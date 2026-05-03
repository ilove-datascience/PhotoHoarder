from telegram import Update
from telegram.ext import CallbackContext, ContextTypes
from mysql.connector import Error
from io import BytesIO
from pathlib import Path
import gc
import time

from PIL import Image

from .common import debug_send, _get_db_connection, get_group_admin_creds_filename
from .google_utils import upload_to_album, CredentialRefreshError, check_existing_album, _load_creds_by_filename

try:
	import torch
	import torchvision.transforms as transforms
	import timm
	TORCH_AVAILABLE = True
except ModuleNotFoundError:
	torch = None
	transforms = None
	timm = None
	TORCH_AVAILABLE = False


MODEL_PATH = Path("model.pth")
DOWNLOADS_DIR = Path("downloads")
IMG_SIZE = 224
DEVICE = torch.device("cuda" if TORCH_AVAILABLE and torch.cuda.is_available() else "cpu") if TORCH_AVAILABLE else "cpu"
CLASS_NAMES = ["discard", "keep"]

INFER_TRANSFORMS = None
if TORCH_AVAILABLE:
	INFER_TRANSFORMS = transforms.Compose([
		transforms.Resize((IMG_SIZE, IMG_SIZE)),
		transforms.ToTensor(),
	])

_model = None
_model_last_used_ts = 0.0
_MODEL_IDLE_TIMEOUT_SECONDS = 60 * 60


def _unload_sort_model_if_idle() -> bool:
	"""Unload the cached sort model when it has been idle too long."""
	global _model, _model_last_used_ts
	if _model is None:
		return False

	now = time.time()
	if now - _model_last_used_ts < _MODEL_IDLE_TIMEOUT_SECONDS:
		return False

	_model = None
	_model_last_used_ts = 0.0
	if TORCH_AVAILABLE and torch.cuda.is_available():
		torch.cuda.empty_cache()
	gc.collect()
	print("Unloaded sort model after idle timeout")
	return True


def _get_sort_model_health() -> tuple[str, str]:
	"""Return the current cached model state and a short detail string."""
	_unload_sort_model_if_idle()
	if not TORCH_AVAILABLE:
		return "unavailable", "torch/timm are not installed"

	if _model is None:
		return "unloaded", "no cached model in memory"

	if _model_last_used_ts:
		idle_seconds = max(0, int(time.time() - _model_last_used_ts))
		return "loaded", f"last used {idle_seconds // 60}m {idle_seconds % 60}s ago"

	return "loaded", "cached model is in memory"


def _get_creds_health(creds_filename: str) -> tuple[str, str]:
	"""Load and validate the configured Google Photos credentials file."""
	if not creds_filename:
		return "missing", "no admin credential file is linked to this chat"

	try:
		creds = _load_creds_by_filename(creds_filename)
	except FileNotFoundError:
		return "missing", f"credential file not found: {creds_filename}"
	except CredentialRefreshError as exc:
		return "invalid", str(exc)
	except Exception as exc:
		return "error", f"failed to load credentials: {exc}"

	if getattr(creds, "valid", False):
		return "valid", f"loaded from {creds_filename}"

	return "invalid", f"token loaded but not valid: {creds_filename}"


def _get_sort_model():
	if not TORCH_AVAILABLE:
		raise RuntimeError("Photo sorting is unavailable because torch/timm are not installed.")

	global _model, _model_last_used_ts
	_unload_sort_model_if_idle()
	if _model is not None:
		_model_last_used_ts = time.time()
		return _model

	if not MODEL_PATH.exists():
		raise FileNotFoundError(f"Model file not found: {MODEL_PATH}")

	model = timm.create_model(
		"mobilenetv3_small_100",
		pretrained=False,
		num_classes=2
	)
	state = torch.load(MODEL_PATH, map_location=DEVICE, weights_only=True)
	model.load_state_dict(state)
	model.to(DEVICE)
	model.eval()
	_model = model
	_model_last_used_ts = time.time()
	return _model


def _predict_photo(photo_bytes: bytes):
	if not TORCH_AVAILABLE:
		raise RuntimeError("Photo sorting is unavailable because torch/timm are not installed.")

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
	# Opportunistically free model memory even if this update is not a sortable photo.
	_unload_sort_model_if_idle()
    
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

	if sort and not TORCH_AVAILABLE:
		await debug_send(
			context,
			chat_id,
			"Photo sorting is disabled because torch/timm are not installed in this environment.",
			debug,
		)
		sort = False

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
				#local_path = _save_discarded_photo(bytes(photo_bytes), file_id)
				await debug_send(
					context,
					chat_id,
					f"Photo discarded by model ({confidence:.4f})",
					debug,
				)
				print(f"Discarded photo saved locally")
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
		
	
async def health_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	chat = update.effective_chat
	if not chat:
		return

	chat_id = chat.id
	model_status, model_detail = _get_sort_model_health()
	creds_filename = await get_group_admin_creds_filename(chat_id)
	creds_status, creds_detail = _get_creds_health(creds_filename)
	album_id = await get_album_id_from_db(chat_id)

	if creds_status == "valid":
		try:
			album_exists = await check_existing_album(update)
			if album_exists:
				album_status = "available"
				album_detail = f"album_link={album_id}"
			else:
				album_status = "missing or inaccessible"
				album_detail = f"album_link={album_id or 'not set'}"
		except Exception as exc:
			album_status = "error"
			album_detail = str(exc)
	else:
		album_status = "skipped"
		album_detail = "credential check failed"

	lines = [
		"Health check:",
		f"Model: {model_status} ({model_detail})",
		f"Credentials: {creds_status} ({creds_detail})",
		f"Album: {album_status} ({album_detail})",
	]
	await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))