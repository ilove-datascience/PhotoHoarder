from telegram import Update
from telegram.ext import CallbackContext, ContextTypes
from mysql.connector import Error
from io import BytesIO
from pathlib import Path

import torch
import torchvision.transforms as transforms
import timm
from PIL import Image

from .common import debug_send, _get_db_connection, get_group_admin_creds_filename
from .google_utils import upload_to_album, CredentialRefreshError


MODEL_PATH = Path("model.pth")
DOWNLOADS_DIR = Path("downloads")
IMG_SIZE = 224
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
CLASS_NAMES = ["discard", "keep"]

INFER_TRANSFORMS = transforms.Compose([
	transforms.Resize((IMG_SIZE, IMG_SIZE)),
	transforms.ToTensor(),
])

_model = None


def _get_sort_model():
	global _model
	if _model is not None:
		return _model

	if not MODEL_PATH.exists():
		raise FileNotFoundError(f"Model file not found: {MODEL_PATH}")

	model = timm.create_model(
		"mobilenetv3_small_100",
		pretrained=False,
		num_classes=2,
	)
	state = torch.load(MODEL_PATH, map_location=DEVICE)
	model.load_state_dict(state)
	model.to(DEVICE)
	model.eval()
	_model = model
	return _model


def _predict_photo(photo_bytes: bytes):
	model = _get_sort_model()
	image = Image.open(BytesIO(photo_bytes)).convert("RGB")
	tensor = INFER_TRANSFORMS(image).unsqueeze(0).to(DEVICE)

	with torch.no_grad():
		logits = model(tensor)
		probs = torch.softmax(logits, dim=1)[0]
		pred_idx = int(torch.argmax(probs).item())

	pred_label = CLASS_NAMES[pred_idx]
	confidence = float(probs[pred_idx].item())
	return pred_label, confidence


def _save_discarded_photo(photo_bytes: bytes, file_id: str):
	DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
	file_path = DOWNLOADS_DIR / f"discard_{file_id}.jpg"
	file_path.write_bytes(photo_bytes)
	return str(file_path)


async def get_album_id_from_db(chat_id: int) -> str:
	"""Fetch the album ID for a given chat from the database"""
	try:
		conn = _get_db_connection()
		cursor = conn.cursor()
		cursor.execute(
			"SELECT album_link FROM chats WHERE chat_id = %s",
			(chat_id,)
		)
		result = cursor.fetchone()
		cursor.close()
		conn.close()
		if result and result[0]:
			return result[0]
		return None
	except Error as e:
		print(f"Error fetching album ID from database: {e}")
		return None


async def getphotos(update: Update, context: ContextTypes.DEFAULT_TYPE, debug: bool, sort: bool = False) -> None:
    
	# normalize message and sender information safely (updates may lack `message`)
	message = update.effective_message

	sender = "unknown"
	if message and getattr(message, "from_user", None):
		sender = getattr(message.from_user, "username", None) or getattr(message.from_user, "first_name", "unknown")
	elif getattr(update, "callback_query", None) and getattr(update.callback_query, "from_user", None):
		sender = getattr(update.callback_query.from_user, "username", None) or getattr(update.callback_query.from_user, "first_name", "unknown")
	elif getattr(update, "effective_user", None):
		sender = getattr(update.effective_user, "username", None) or getattr(update.effective_user, "first_name", "unknown")

	print(f"Received a message from {sender}")

	# ensure we have a chat to operate on
	if not getattr(update, "effective_chat", None):
		print("Update has no effective chat; ignoring.")
		return

	# get album ID for this chat
	chat_id = update.effective_chat.id
	group_id = update.effective_chat.id
	album_id = await get_album_id_from_db(chat_id)
	if not album_id:
		await debug_send(context, chat_id, "No album found for this chat. Please use /start first.", debug)
		return

	admin_creds_filename = await get_group_admin_creds_filename(group_id)
	if not admin_creds_filename:
		await debug_send(context, chat_id, "No admin credentials found for this group. Admin must run /start first.", debug)
		return

	# check if message contains a photo, if so save and upload it
	if message and message.photo:
		# get photo obj
		photo = message.photo[-1]
		file_id = photo.file_id  # get file obj id
		file = await photo.get_file()
		photo_bytes = await file.download_as_bytearray()

		first_name = getattr(message.from_user, "first_name", "unknown") if message and getattr(message, "from_user", None) else "unknown"
		print((f'{first_name} sent a photo with file_id: {file_id}'))

		if sort:
			try:
				pred_label, confidence = _predict_photo(bytes(photo_bytes))
				print(f"Model decision for {file_id}: {pred_label} ({confidence:.4f})")
			except Exception as e:
				await debug_send(context, chat_id, f"Sorting failed for photo {file_id}: {str(e)}", debug)
				print(f"Error sorting photo {file_id}: {e}")
				return

			if pred_label == "discard":
				local_path = _save_discarded_photo(bytes(photo_bytes), file_id)
				await debug_send(
					context,
					chat_id,
					f"Photo discarded by model ({confidence:.4f}) and saved locally to {local_path}",
					debug,
				)
				print(f"Discarded photo saved locally: {local_path}")
				return
		
		try:
			# Upload to Google Photos album
			photo_url = await upload_to_album(
				None,
				album_id,
				admin_creds_filename,
				media_bytes=bytes(photo_bytes),
				file_name=f"{file_id}.jpg",
			)
			await debug_send(context, chat_id, f"Photo uploaded to album! URL: {photo_url}", debug)
			print(f"Photo uploaded to album {album_id}: {photo_url}")
		except CredentialRefreshError as e:
			await context.bot.send_message(
				chat_id=chat_id,
				text=f"🔐 {str(e)}"
			)
			print(f"Credential refresh failed for photo upload: {e}")
		except FileNotFoundError as e:
			if "google_photos_creds" in str(e):
				await context.bot.send_message(
					chat_id=chat_id,
					text="🔐 Credentials not found. Please run /start to authorize. Please send image again after credentials are updated."
				)
				print(f"Credential file not found for photo upload: {e}")
			else:
				await debug_send(context, chat_id, f"Failed to upload photo: {str(e)}", debug)
				print(f"Error uploading photo to album: {e}")
		except Exception as e:
			await debug_send(context, chat_id, f"Failed to upload photo: {str(e)}", debug)
			print(f"Error uploading photo to album: {e}")

	if message and message.video:
		video = message.video
		file_id = video.file_id
		file = await video.get_file()
		video_bytes = await file.download_as_bytearray()
		first_name = getattr(message.from_user, "first_name", "unknown") if message and getattr(message, "from_user", None) else "unknown"
		print((f'{first_name} sent a video with file_id: {file_id}'))
		
		try:
			# Upload to Google Photos album
			video_url = await upload_to_album(
				None,
				album_id,
				admin_creds_filename,
				media_bytes=bytes(video_bytes),
				file_name=f"{file_id}.mp4",
			)
			await debug_send(context, chat_id, f"Video uploaded to album! URL: {video_url}", debug)
			print(f"Video uploaded to album {album_id}: {video_url}")
		except CredentialRefreshError as e:
			await context.bot.send_message(
				chat_id=chat_id,
				text=f"🔐 {str(e)}"
			)
			print(f"Credential refresh failed for video upload: {e}")
		except FileNotFoundError as e:
			if "google_photos_creds" in str(e):
				await context.bot.send_message(
					chat_id=chat_id,
					text="🔐 Credentials not found. Please run /start to authorize. Please send video again after credentials are updated."
				)
				print(f"Credential file not found for video upload: {e}")
			else:
				await debug_send(context, chat_id, f"Failed to upload video: {str(e)}", debug)
				print(f"Error uploading video to album: {e}")
		except Exception as e:
			await debug_send(context, chat_id, f"Failed to upload video: {str(e)}", debug)
			print(f"Error uploading video to album: {e}")
		
	# mock reply for now
	elif message and getattr(message, "text", None):
		original_text = message.text
		print(f"Received text message: {original_text}")
		await debug_send(context, chat_id, f"SYBAUUUUU {sender.upper()}", debug)

