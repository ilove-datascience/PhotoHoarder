import pickle
import threading
from pathlib import Path
from typing import Optional

from telegram import Update
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import AuthorizedSession, Request

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]
CREDS_DIR = Path("config")
OAUTH_TIMEOUT = 60  # 1 minute timeout for OAuth


def _get_creds_filename(user_id: int, group_id: int) -> str:
	"""Generate a credential filename for a specific user and group."""
	return f"google_photos_creds_{user_id}_{group_id}.pickle"


class OAuthTimeoutError(Exception):
	"""Raised when OAuth flow times out."""
	pass


class CredentialRefreshError(Exception):
	"""Raised when credential refresh fails and user must re-authorize via /start."""
	pass


def build_oauth_authorization_url() -> str:
	"""Build a Google OAuth authorization link that can be shared via Telegram."""
	flow = InstalledAppFlow.from_client_secrets_file("config/credentials.json", SCOPES)
	flow.redirect_uri = "http://localhost"
	auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
	return auth_url


def _load_or_create_creds(user_id: int, group_id: int):
	"""Load cached credentials or run full OAuth flow if cache doesn't exist."""
	creds_filename = _get_creds_filename(user_id, group_id)
	creds_path = CREDS_DIR / creds_filename
	
	if creds_path.exists():
		with open(creds_path, "rb") as token:
			creds = pickle.load(token)
		if hasattr(creds, "has_scopes") and not creds.has_scopes(SCOPES):
			# Scope set changed (for example, sharing was added); re-run OAuth.
			creds_path.unlink(missing_ok=True)
			return _load_or_create_creds(user_id, group_id)
		# Refresh if expired
		if creds.expired and creds.refresh_token:
			try:
				creds.refresh(Request())
				with open(creds_path, "wb") as token:
					pickle.dump(creds, token)
			except Exception as e:
				print(f"Failed to refresh credentials for user {user_id}, group {group_id}: {e}")
				raise CredentialRefreshError(
					f"Credentials have expired and could not be refreshed. Please run /start again to re-authorize."
				)
		return creds
	else:
		# Run full OAuth flow with timeout for first-time auth
		flow = InstalledAppFlow.from_client_secrets_file("config/credentials.json", SCOPES)
		creds_holder = {"creds": None, "error": None}
		
		def run_auth():
			try:
				creds_holder["creds"] = flow.run_local_server(port=0, open_browser=True)
			except Exception as e:
				creds_holder["error"] = e
		
		auth_thread = threading.Thread(target=run_auth, daemon=True)
		auth_thread.start()
		auth_thread.join(timeout=OAUTH_TIMEOUT)
		
		if auth_thread.is_alive():
			raise OAuthTimeoutError(f"OAuth flow timed out after {OAUTH_TIMEOUT} seconds. Please authorize the app and try again.")
		
		if creds_holder["error"]:
			raise creds_holder["error"]
		
		if not creds_holder["creds"]:
			raise RuntimeError("OAuth flow failed to produce credentials.")
		
		creds = creds_holder["creds"]
		# Save credentials for future use
		with open(creds_path, "wb") as token:
			pickle.dump(creds, token)
		return creds


def _build_photos_service(user_id: int, group_id: int):
	creds = _load_or_create_creds(user_id, group_id)
	service = build("photoslibrary", "v1", credentials=creds, static_discovery=False)
	return service, creds


def _load_creds_by_filename(creds_filename: str):
	"""Load cached credentials by filename from the config credential directory."""
	if not creds_filename:
		raise ValueError("creds_filename is required for upload")

	creds_path = CREDS_DIR / creds_filename
	if not creds_path.exists():
		raise FileNotFoundError(f"Credential file not found: {creds_path}")

	with open(creds_path, "rb") as token:
		creds = pickle.load(token)

	if creds.expired and creds.refresh_token:
		try:
			creds.refresh(Request())
			with open(creds_path, "wb") as token:
				pickle.dump(creds, token)
		except Exception as e:
			print(f"Failed to refresh credentials from file {creds_filename}: {e}")
			raise CredentialRefreshError(
				f"Credentials have expired and could not be refreshed. Please run /start again to re-authorize."
			)

	return creds


def _build_photos_service_from_filename(creds_filename: str):
	creds = _load_creds_by_filename(creds_filename)
	service = build("photoslibrary", "v1", credentials=creds, static_discovery=False)
	return service, creds


async def upload_to_album(
	file_path: Optional[str],
	album_id: str,
	creds_filename: str,
	*,
	media_bytes: bytes = None,
	file_name: str = None,
) -> str:
	if not album_id:
		raise ValueError("album_id is required to upload media into a designated album")

	if media_bytes is None:
		if not file_path:
			raise ValueError("Either file_path or media_bytes must be provided")
		media_path = Path(file_path)
		if not media_path.exists():
			raise FileNotFoundError(f"Media file not found: {file_path}")
		payload = media_path.read_bytes()
		upload_name = file_name or media_path.name
	else:
		payload = media_bytes
		upload_name = file_name or "upload.bin"

	service, creds = _build_photos_service_from_filename(creds_filename)

	upload_session = AuthorizedSession(creds)
	upload_response = upload_session.post(
		"https://photoslibrary.googleapis.com/v1/uploads",
		data=payload,
		headers={
			"Content-type": "application/octet-stream",
			"X-Goog-Upload-Protocol": "raw",
			"X-Goog-Upload-File-Name": upload_name,
		},
	)
	upload_response.raise_for_status()
	upload_token = upload_response.text

	batch_response = service.mediaItems().batchCreate(
		body={
			"albumId": album_id,
			"newMediaItems": [
				{
					"description": upload_name,
					"simpleMediaItem": {"uploadToken": upload_token},
				}
			],
		}
	).execute()

	created_items = batch_response.get("newMediaItemResults", [])
	if not created_items:
		raise RuntimeError("Google Photos did not return a created media item")

	media_item = created_items[0].get("mediaItem", {})
	return media_item.get("productUrl") or media_item.get("id", "")

async def check_existing_album(update: Update) -> bool:
	chat = update.effective_chat
	if not chat:
		return False

	chat_id = chat.id
	album_id = None

	# Read the configured album id for this chat from SQL.
	try:
		from .common import _get_db_connection, get_group_admin_creds_filename

		conn = _get_db_connection()
		cursor = conn.cursor()
		cursor.execute(
			"SELECT album_link FROM chats WHERE chat_id = %s",
			(chat_id,),
		)
		result = cursor.fetchone()
		cursor.close()
		conn.close()

		if not result or not result[0]:
			return False

		album_id = result[0]
		creds_filename = await get_group_admin_creds_filename(chat_id)
		if not creds_filename:
			return False
	except Exception as e:
		print(f"Error loading album id from database for chat {chat_id}: {e}")
		return False

	# Confirm the album still exists in Google Photos.
	try:
		service, _ = _build_photos_service_from_filename(creds_filename)
		album = service.albums().get(albumId=album_id).execute()
		return bool(album and album.get("id") == album_id)
	except Exception as e:
		print(f"Album verification failed for chat {chat_id}, album {album_id}: {e}")
		return False

async def create_album(update: Update) -> tuple:
	"""Create album and return (album_id, album_url, creds_filename)."""
	user_id = update.effective_user.id
	group_id = update.effective_chat.id
	group_name = update.effective_chat.title if update.effective_chat.title else "Default Group"
	album_title = f"{group_name} Album"
	creds_filename = _get_creds_filename(user_id, group_id)

	# Reuse an existing group album from SQL without creating a duplicate.
	try:
		from .common import _get_db_connection

		conn = _get_db_connection()
		cursor = conn.cursor()
		cursor.execute(
			"SELECT album_link FROM chats WHERE chat_id = %s",
			(group_id,),
		)
		result = cursor.fetchone()
		cursor.close()
		conn.close()

		existing_album_id = result[0] if result and result[0] else None
		print(f"Existing album ID from database for group {group_id}: {existing_album_id}")
		if existing_album_id:
			# Ensure credentials are present/valid for this user+group.
			# This may trigger OAuth when needed, even if album already exists.
			_build_photos_service(user_id, group_id)
			existing_album_url = f"https://photos.google.com/lr/album/{existing_album_id}"
			print(f"Album already linked for group {group_id}. Reusing album {existing_album_id}")
			return existing_album_id, existing_album_url, creds_filename
	except Exception as e:
		print(f"Existing album lookup failed for group {group_id}, creating a new one: {e}")
	
	# Use the cached service builder
	service, creds = _build_photos_service(user_id, group_id)

	# Step 2: Create album
	album_body = {
		"album": {
			"title": album_title
		}
	}

	response = service.albums().create(body=album_body).execute()

	# Step 3: Output result
	album_id = response.get("id")
	album_url = response.get("productUrl") or f"https://photos.google.com/album/{album_id}"

	print("Album created!")
	print("Title:", response.get("title"))
	print("Album ID:", album_id)
	return album_id, album_url, creds_filename


upload_to_drive = upload_to_album

async def update_credentials_for_group(user_id: int, group_id: int, creds_filename: str):
	"""Update credentials for a specific user and group."""
	creds_path = CREDS_DIR / creds_filename
	if not creds_path.exists():
		raise FileNotFoundError(f"Credential file not found: {creds_path}")

	with open(creds_path, "rb") as token:
		creds = pickle.load(token)

	if creds.expired and creds.refresh_token:
		creds.refresh(Request())
		with open(creds_path, "wb") as token:
			pickle.dump(creds, token)