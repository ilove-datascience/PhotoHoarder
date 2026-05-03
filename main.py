from pathlib import Path
import asyncio
import logging
import os
import pickle
import threading

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from telegram import Bot, Update, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from utils.functions import getphotos
from utils import google_utils
from utils.common import start, _get_db_connection, store_admin_creds

logger = logging.getLogger(__name__)
TOKEN = os.getenv("tg_token")
BOT = Bot(token=TOKEN) if TOKEN else None
WEB_APP = FastAPI()
WEB_SERVER_STARTED = False

DEBUG = False # degug flag to control debug messages, set to False in production
SORT = True # sort photos to keep vs discard, set to False to keep all photos without sorting


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # handles all media messages (photos, videos, text) and routes to appropriate functions, uploads to google photos
	await getphotos(update, context, debug=DEBUG, sort=SORT)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
	# Application-level error handler to catch exceptions from handlers
	try:
		print("Exception in handler:", context.error)
		# try to notify the chat if available
		if getattr(update, "effective_chat", None):
			try:
				await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ An internal error occurred. The maintainers have been notified.")
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
	app = Application.builder().token(TOKEN).build()
	app.add_handler(CommandHandler("start", start))

	# register a global error handler so uncaught exceptions are surfaced and handled
	app.add_error_handler(error_handler)
	
	app.add_handler(
		MessageHandler(
			filters.PHOTO | filters.VIDEO | (filters.TEXT & ~filters.COMMAND),
			handle_media,
		)
	)
	app.run_polling()


if __name__ == "__main__":
    main()
