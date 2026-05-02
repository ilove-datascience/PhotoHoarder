from telegram import Update, ForceReply,InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import logging 
from dotenv import load_dotenv
import os
import asyncio  
load_dotenv()
logger = logging.getLogger(__name__)
TOKEN = os.getenv("tg_token")
from utils.functions import getphotos
from utils.google_utils import upload_to_drive, check_existing_album, create_album, OAuthTimeoutError, build_oauth_authorization_url
from utils import debug_send, link_sqlite, store_admin_creds
from utils.common import start

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


def main():
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
