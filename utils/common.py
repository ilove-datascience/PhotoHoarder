from pathlib import Path
import os

from telegram import Update, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize
import mysql.connector
from mysql.connector import Error
import logging
from dotenv import load_dotenv
from telegram.ext import ContextTypes


from utils.google_utils import build_oauth_authorization_url


BASE_DIR = Path(__file__).resolve().parent.parent
global ADMIN_USER
ADMIN_USER = int(os.getenv("ADMIN_USER_ID", "0")) if os.getenv("ADMIN_USER_ID") else None


DEFAULT_DB_CONFIG = {
	"host": "localhost",
	"user": "root",
	"password": "",
	"database": "photohoarder"
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
	if user == ADMIN_USER:
		groupchat = update.effective_chat.type in ['group', 'supergroup']
		if groupchat:
			group_id = update.effective_chat.id
			chat_id = update.effective_chat.id
			print(f"Bot started in group chat with id: {group_id}")
			member_count = await context.bot.get_chat_member_count(group_id)
			print(f"Group member count: {member_count}")

			auth_url = build_oauth_authorization_url(update.effective_user.id, group_id)
			
			await context.bot.send_message(
				chat_id=chat_id,
				text=f"🔐 Google authorization is required.\n\nOpen this link and sign in:\n{auth_url}\n\nAfter you approve access, I will finish the setup automatically through the callback URL."
			)

			return
	else:
		await context.bot.send_message(chat_id=update.effective_chat.id, text="Unauthorized user.")
