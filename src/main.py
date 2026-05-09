from pathlib import Path
import asyncio
import logging
import os
import pickle
import threading

import uvicorn
from logging.handlers import RotatingFileHandler
import traceback
import datetime
import sys
from fastapi import FastAPI, HTTPException, Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from telegram import Bot, Update, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, BaseFilter

from src.utils.functions import getphotos, health_check, genderswap
from src.utils import google_utils
from src.utils.common import start, _get_db_connection, store_admin_creds, ADMIN_USER, is_group_started

logger = logging.getLogger(__name__)
IS_RAILWAY = os.getenv("RAILWAY_ENVIRONMENT") is not None
JASON_USER = int(os.getenv("jason_user", "0")) if os.getenv("jason_user") else None
def setup_logging():
	"""Configure logging: always stream to stdout and optionally write to a rotating file."""
	log_dir = Path("/data/logs") if IS_RAILWAY else Path(".") / "logs"
	try:
		log_dir.mkdir(parents=True, exist_ok=True)
	except Exception:
		pass

	log_file = log_dir / "app.log"
	fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
	formatter = logging.Formatter(fmt)

	# Stream handler (stdout) — always present so PaaS captures logs
	stream_h = logging.StreamHandler(sys.stdout)
	stream_h.setFormatter(formatter)

	# File handler (optional)
	handlers = [stream_h]
	try:
		file_h = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
		file_h.setFormatter(formatter)
		handlers.append(file_h)
	except Exception:
		# If file handler can't be created, continue with stream only
		pass

	# Attach handlers if not already present
	if not logger.handlers:
		for h in handlers:
			logger.addHandler(h)
	else:
		# ensure we have a stream handler
		if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
			logger.addHandler(stream_h)

	level = os.getenv("LOG_LEVEL", "INFO").upper()
	try:
		logger.setLevel(getattr(logging, level))
	except Exception:
		logger.setLevel(logging.INFO)


setup_logging()

# In-memory last error store (UTC ISO timestamp and traceback)
LAST_ERROR = None
LAST_ERROR_TS = None
TOKEN = os.getenv("tg_token")
BOT = Bot(token=TOKEN) if TOKEN else None
WEB_APP = FastAPI()
WEB_SERVER_STARTED = False
import random
DEBUG = False # degug flag to control debug messages, set to False in production
SORT = True # sort photos to keep vs discard, set to False to keep all photos without sorting


class GroupStartedFilter(BaseFilter):
	"""Filter that checks if a group has been started (exists in chats table)."""
	async def filter(self, update: Update) -> bool:
		if not update.effective_chat:
			return False
		group_id = update.effective_chat.id
		result = await is_group_started(group_id)
		if not result:
			try:
				await update.effective_chat.send_message("This group has not been started yet. Please use /start command first.")
			except Exception as e:
				print(f"Failed to send group started reminder: {e}")
		return result


async def respond_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
	# generic response handler for any text message that doesn't match other handlers, can be used for debugging or future features
	if update.message and update.message.text and update.message.text.strip() and update.message.text.strip().split()[0].lower() == "computer":
		choices = ["daddy", "master", "sir", "boss", "chief", "captain", "commander",  "sgt", "SARGANT", "sir, i'm just a stupid clanker"]
		choice = random.choice(choices)
		msg= f"yes {choice}.."
		await update.message.reply_text(msg)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # handles all media messages (photos, videos, text) and routes to appropriate functions, uploads to google photos
	await getphotos(update, context, debug=DEBUG, sort=SORT)

async def debug_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
	global DEBUG
	DEBUG = not DEBUG
	status = "ON" if DEBUG else "OFF"
	await update.message.reply_text(f"Debug mode is now {status}.")
 
async def last_error_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
	# Only allow admin to fetch last error
	try:
		user_id = getattr(update.effective_user, "id", None)
	except Exception:
		user_id = None
	if ADMIN_USER and user_id != ADMIN_USER:
		await update.message.reply_text("Unauthorized.")
		return
	if LAST_ERROR:
		text = f"Last error at {LAST_ERROR_TS}:\n{LAST_ERROR[:3800]}"
	else:
		text = "No recorded errors."
	await update.message.reply_text(text)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
	# Application-level error handler to catch exceptions from handlers
	try:
		print("Exception in handler:", context.error)
		# capture traceback text
		try:
			tb_text = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
		except Exception:
			tb_text = str(context.error)
		# store last error (UTC)
		global LAST_ERROR, LAST_ERROR_TS
		LAST_ERROR = tb_text
		LAST_ERROR_TS = datetime.datetime.utcnow().isoformat()
		logger.error("Unhandled exception in handler: %s", tb_text)
		# try to notify the chat if available (short message)
		if getattr(update, "effective_chat", None):
			try:
				await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ An internal error occurred. The maintainers have been notified.")
				# send full traceback to admin only
				if ADMIN_USER:
					try:
						await context.bot.send_message(chat_id=ADMIN_USER, text=f"Internal error in chat {getattr(update.effective_chat,'id',None)}: {context.error}\n\nTraceback (truncated):\n{tb_text[:3800]}")
					except Exception as ex:
						print("Failed to send traceback to admin via Telegram:", ex)
			except Exception as ex:
				print("Failed to send error notification to chat:", ex)
	except Exception as e:
		print("Error in error_handler:", e)


def _upsert_group_album_link(group_id: int, chat_name: str, album_id: str) -> None:
	conn = _get_db_connection()
	cursor = conn.cursor()
	cursor.execute(
		"""
		INSERT INTO chats (chat_id, chat_name, album_link)
		VALUES (%s, %s, %s)
		ON DUPLICATE KEY UPDATE chat_name = VALUES(chat_name), album_link = VALUES(album_link)
		""",
		(group_id, chat_name, album_id),
	)
	conn.commit()
	cursor.close()
	conn.close()


@WEB_APP.get("/")
async def healthcheck():
	return {"status": "ok"}


@WEB_APP.get("/oauth2callback")
async def oauth2callback(request: Request):
	if not TOKEN or BOT is None:
		raise HTTPException(status_code=500, detail="Telegram bot token is not configured.")

	if request.query_params.get("error"):
		raise HTTPException(status_code=400, detail=request.query_params.get("error"))

	state = request.query_params.get("state")
	print(f"OAuth callback received with state: {state}")
	if not state or ":" not in state:
		print(f"Invalid state format: {state}")
		raise HTTPException(status_code=400, detail="Missing or invalid OAuth state.")

	user_id_str, group_id_str = state.split(":", 1)
	try:
		user_id = int(user_id_str)
		group_id = int(group_id_str)
	except ValueError as exc:
		print(f"Failed to parse state {state}: {exc}")
		raise HTTPException(status_code=400, detail="Invalid OAuth state payload.") from exc

	# Retrieve the flow from cache (which has the code_verifier)
	print(f"Looking for cached flow with state: {state}")
	flow = google_utils._retrieve_oauth_flow(state)
	if flow is None:
		print(f"Flow not found in cache for state: {state}")
		raise HTTPException(status_code=400, detail="OAuth flow state not found or expired. Try /start again.")

	print(f"Found cached flow for state: {state}")
	# Reconstruct authorization response using the same redirect_uri from the flow
	# This ensures we use HTTPS on Railway (request.url may be HTTP internally)
	redirect_uri = google_utils.OAUTH_REDIRECT_URI
	authorization_response = f"{redirect_uri}?{request.url.query}"
	print(f"Authorization response URL: {authorization_response}")

	try:
		flow.fetch_token(authorization_response=authorization_response)
	except Exception as exc:
		print(f"Token exchange failed: {exc}")
		raise HTTPException(status_code=400, detail=f"OAuth token exchange failed: {exc}") from exc

	creds = flow.credentials
	creds_filename = google_utils._get_creds_filename(user_id, group_id)
	creds_path = google_utils.CREDS_DIR / creds_filename
	creds_path.parent.mkdir(parents=True, exist_ok=True)
	with open(creds_path, "wb") as token_file:
		pickle.dump(creds, token_file)

	try:
		chat = await BOT.get_chat(group_id)
		chat_name = chat.title or chat.first_name or "Default Group"
	except Exception:
		chat_name = "Default Group"

	conn = _get_db_connection()
	cursor = conn.cursor()
	cursor.execute(
		"SELECT album_link FROM chats WHERE chat_id = %s",
		(group_id,),
	)
	result = cursor.fetchone()
	existing_album_id = result[0] if result and result[0] else None
	cursor.close()
	conn.close()

	await store_admin_creds(user_id, group_id, creds_filename)

	if existing_album_id:
		_upsert_group_album_link(group_id, chat_name, existing_album_id)
		album_url = f"https://photos.google.com/lr/album/{existing_album_id}"
		await BOT.send_message(
			chat_id=group_id,
			text=f"✅ Google authorization complete. Reusing the existing album.\n\nAlbum link: {album_url}\nAlbum ID: {existing_album_id}",
		)
		return {"message": "Authorization successful", "album_id": existing_album_id}

	service = build("photoslibrary", "v1", credentials=creds, static_discovery=False)
	album_title = f"{chat_name} Album"
	response = service.albums().create(body={"album": {"title": album_title}}).execute()
	album_id = response.get("id")
	album_url = response.get("productUrl") or f"https://photos.google.com/album/{album_id}"

	_upsert_group_album_link(group_id, chat_name, album_id)
	await BOT.send_message(
		chat_id=group_id,
		text=f"✅ Album is ready!\n\nAlbum link: {album_url}\nAlbum ID: {album_id}\n\nYou can now start sending photos and videos, and they'll be automatically uploaded to this album.",
	)
	return {"message": "Authorization successful", "album_id": album_id}


def _start_web_server() -> None:
	global WEB_SERVER_STARTED
	if WEB_SERVER_STARTED:
		return

	port = int(os.getenv("PORT", "8000"))
	config = uvicorn.Config(WEB_APP, host="0.0.0.0", port=port, log_level="info")
	server = uvicorn.Server(config)
	thread = threading.Thread(target=server.run, daemon=True)
	thread.start()
	WEB_SERVER_STARTED = True
	print(f"Web server started on port {port}; OAuth callback endpoint: {google_utils.OAUTH_REDIRECT_URI}")


def main():
	# Ensure required directories exist before starting the bot
	def ensure_required_dirs():
		base = Path(".")
		required = [
			base / "downloads",
			base / "config",
			base / "ml",
			base / "ml" / "data",
			base / "ml" / "data" / "train",
			base / "ml" / "data" / "train" / "keep",
			base / "ml" / "data" / "train" / "discard",
			base / "ml" / "data" / "test",
			base / "ml" / "data" / "test" / "keep",
			base / "ml" / "data" / "test" / "discard",
		]

		for d in required:
			try:
				d.mkdir(parents=True, exist_ok=True)
			except Exception as e:
				print(f"Failed to create or verify directory {d}: {e}")

	ensure_required_dirs()
	_start_web_server()
	group_started_filter = GroupStartedFilter()
	app = Application.builder().token(TOKEN).build()
	app.add_handler(CommandHandler("start", start))
	app.add_handler(CommandHandler("health", health_check, filters=group_started_filter))
	app.add_handler(CommandHandler("lasterror", last_error_cmd, filters=(filters.User(ADMIN_USER) if ADMIN_USER else filters.ALL) & group_started_filter))
	app.add_handler(CommandHandler("debug", debug_switch, filters=(filters.User(ADMIN_USER) if ADMIN_USER else filters.ALL) & group_started_filter))
	app.add_handler(CommandHandler("genderswap", genderswap, filters=group_started_filter))

	# register a global error handler so uncaught exceptions are surfaced and handled
	app.add_error_handler(error_handler)
	app.add_handler(
		MessageHandler(
			(filters.User(JASON_USER) & (filters.TEXT & ~filters.COMMAND)) if JASON_USER else filters.ALL,
			respond_msg,
		)
	)
	app.add_handler(
		MessageHandler(
			(filters.PHOTO | filters.VIDEO) & group_started_filter,
			handle_media,
		)
	)
	app.run_polling()


if __name__ == "__main__":
    main()
