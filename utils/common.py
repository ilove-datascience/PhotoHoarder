from pathlib import Path
import os

from telegram import Update, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize
import mysql.connector
from mysql.connector import Error
import logging
from dotenv import load_dotenv
from telegram.ext import ContextTypes

from utils.google_utils import OAuthTimeoutError, build_oauth_authorization_url, create_album


BASE_DIR = Path(__file__).resolve().parent.parent
# Load .env locally; skip if not found (e.g., Railway uses env vars directly)
#env_file = BASE_DIR / ".env"
#if env_file.exists():
#	load_dotenv(env_file)

DEFAULT_DB_CONFIG = {
	"host": "localhost",
	"user": "root",
	"password": "",
	"database": "photohoarderlalalal"
}


def _load_db_config():
	"""Load database credentials from environment variables."""
	return {
		"host": os.getenv("MYSQLHOST", DEFAULT_DB_CONFIG["host"]),
		"user": os.getenv("MYSQLUSER", DEFAULT_DB_CONFIG["user"]),
		"password": os.getenv("MYSQL_ROOT_PASSWORD", DEFAULT_DB_CONFIG["password"]),
		"database": os.getenv("MYSQL_DATABASE", DEFAULT_DB_CONFIG["database"]),
	}


def _get_db_connection():
	"""Get a MySQL connection using cached credentials."""
	config = _load_db_config()
	return mysql.connector.connect(
		host=config.get("host"),
		user=config.get("user"),
		password=config.get("password"),
		database=config.get("database")
	)


async def debug_send(context, chat_id, text, debug):
	if debug:
		await context.bot.send_message(chat_id=chat_id, text=text)


async def link_sqlite(update: Update, file_id: str, drive_url: str) -> None:
	"""Link file information to MySQL database"""
	try:
		conn = _get_db_connection()
		cursor = conn.cursor()
		
		chat_id = update.effective_chat.id
		chat_name = update.effective_chat.title or update.effective_user.first_name
		
		# Update or insert chat record with album link
		cursor.execute("""
			INSERT INTO chats (chat_id, chat_name, album_link)
			VALUES (%s, %s, %s)
			ON DUPLICATE KEY UPDATE album_link = %s
		""", (chat_id, chat_name, drive_url, drive_url))
		
		conn.commit()
		cursor.close()
		conn.close()
		print(f"Linked file_id {file_id} to database for chat {chat_id}")
	except Error as e:
		print(f"Error linking to MySQL: {e}")


async def store_admin_creds(user_id: int, group_id: int, creds_filename: str) -> None:
	"""Store exactly one admin credential mapping per group in the admins table."""
	try:
		conn = _get_db_connection()
		cursor = conn.cursor()

		# Keep a single admin record per group.
		cursor.execute(
			"DELETE FROM admins WHERE group_id = %s",
			(group_id,),
		)

		# Upsert protects reruns for the same admin/group pair.
		cursor.execute(
			"""
			INSERT INTO admins (user_id, group_id, google_creds_file)
			VALUES (%s, %s, %s)
			ON DUPLICATE KEY UPDATE google_creds_file = VALUES(google_creds_file)
			""",
			(user_id, group_id, creds_filename),
		)
		
		conn.commit()
		cursor.close()
		conn.close()
		print(f"Stored credential filename for admin user {user_id} in group {group_id}")
	except Error as e:
		print(f"Error storing admin credentials: {e}")


async def get_admin_creds_filename(user_id: int, group_id: int) -> str:
	"""Retrieve the credential filename for an admin user"""
	try:
		conn = _get_db_connection()
		cursor = conn.cursor()
		
		cursor.execute(
			"SELECT google_creds_file FROM admins WHERE user_id = %s AND group_id = %s",
			(user_id, group_id)
		)
		result = cursor.fetchone()
		cursor.close()
		conn.close()
		
		if result and result[0]:
			return result[0]
		return None
	except Error as e:
		print(f"Error retrieving admin credentials filename: {e}")
		return None


async def get_group_admin_creds_filename(group_id: int) -> str:
	"""Retrieve the credential filename for the admin configured for a group."""
	try:
		conn = _get_db_connection()
		cursor = conn.cursor()

		cursor.execute(
			"SELECT google_creds_file FROM admins WHERE group_id = %s LIMIT 1",
			(group_id,),
		)
		result = cursor.fetchone()
		cursor.close()
		conn.close()

		if result and result[0]:
			return result[0]
		return None
	except Error as e:
		print(f"Error retrieving group admin credentials filename: {e}")
		return None




async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
	user = update.effective_user.id 
	if user == 919013273:
		groupchat = update.effective_chat.type in ['group', 'supergroup']
		if groupchat:
			group_id = update.effective_chat.id
			chat_id = update.effective_chat.id
			print(f"Bot started in group chat with id: {group_id}")
			member_count = await context.bot.get_chat_member_count(group_id)
			print(f"Group member count: {member_count}")

			auth_url = build_oauth_authorization_url()
			
			await context.bot.send_message(
				chat_id=chat_id,
				text=f"🔐 Google authorization is required.\n\nOpen this link and sign in:\n{auth_url}\n\nAfter you approve access, the browser may show 'localhost refused to connect' or a blank localhost page. That is expected in this flow; you can close that tab.\n\nIf setup does not complete automatically, run /start again."
			)

			await context.bot.send_message(
				chat_id=chat_id,
				text="⏳ Attempting album setup now (OAuth timeout is 1 minute)..."
			)
			
			try:
				album = await create_album(update)
				if album:
					album_id, album_url, creds_filename = album
					print(f"Created album with ID: {album_id}")
					await store_admin_creds(update.effective_user.id, update.effective_chat.id, creds_filename)
					await link_sqlite(update, file_id="", drive_url=album_id)
					
					await context.bot.send_message(
						chat_id=chat_id,
						text=f"✅ Album is ready!\n\nAlbum link: {album_url}\nAlbum ID: {album_id}\n\nYou can now start sending photos and videos, and they'll be automatically uploaded to this album."
					)
				else:
					await context.bot.send_message(
						chat_id=chat_id,
						text="❌ Failed to create album. Please try /start again."
					)
			except OAuthTimeoutError as e:
				print(f"OAuth timeout: {e}")
				await context.bot.send_message(
					chat_id=chat_id,
					text=f"⏱️ Authorization timed out. Please complete the Google sign-in within 1 minute and try /start again."
				)
			except Exception as e:
				print(f"Error creating album: {e}")
				await context.bot.send_message(
					chat_id=chat_id,
					text=f"❌ Error: {str(e)}\n\nPlease try /start again."
				)
	else:
		await context.bot.send_message(chat_id=update.effective_chat.id, text="Unauthorized user.")
