import os
import pickle
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import AuthorizedSession, Request

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _resolve_oauth_redirect_uri() -> str:
    override = os.getenv("OAUTH_REDIRECT_URI")
    if override:
        return override

    if os.getenv("RAILWAY_ENVIRONMENT"):
        railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_STATIC_URL")
        if railway_domain:
            railway_domain = railway_domain.replace("https://", "").replace("http://", "").rstrip("/")
            return f"https://{railway_domain}/oauth2callback"

    return "http://localhost:8000/oauth2callback"


OAUTH_REDIRECT_URI = _resolve_oauth_redirect_uri()
print(f"Resolved OAuth redirect URI: {OAUTH_REDIRECT_URI}")


def get_path(env_name: str, default_path: str | Path) -> Path:
    """Read a path from env and resolve relative values from the repo root."""
    value = os.getenv(env_name)
    path = Path(value) if value else Path(default_path)

    if not path.is_absolute():
        if IS_RAILWAY:
            normalized = path.as_posix()
            if normalized.startswith("./"):
                normalized = normalized[2:]
            if normalized.startswith("data/"):
                normalized = normalized[5:]
            elif normalized == "data":
                normalized = ""
            path = Path("/data") if not normalized else Path("/data") / normalized
        else:
            path = BASE_DIR / path
    print(f"Resolved path for {env_name}: {path}")

    return path


def get_oauth_redirect_uri() -> str:
    """Return the configured OAuth redirect URI."""
    return OAUTH_REDIRECT_URI


def uses_remote_oauth() -> bool:
    """True when the OAuth redirect URI points to a non-localhost host."""
    host = urlparse(get_oauth_redirect_uri()).hostname
    return host not in {"localhost", "127.0.0.1", "::1"}


IS_RAILWAY = os.getenv("RAILWAY_ENVIRONMENT") is not None
CREDS_DIR = get_path("CREDS_DIR", "/data" if IS_RAILWAY else BASE_DIR / "config")
print(f"Using credential directory: {CREDS_DIR}")
CREDS_DIR.mkdir(parents=True, exist_ok=True)
GOOGLE_CLIENT_SECRET_PATH = get_path("GOOGLE_CLIENT_SECRET_PATH", CREDS_DIR / "client_secret.json")
GOOGLE_TOKEN_PATH = get_path("GOOGLE_TOKEN_PATH", CREDS_DIR / "token.json")
OAUTH_TIMEOUT = int(os.getenv("OAUTH_TIMEOUT", "60"))

# If client secret JSON provided via env, write it to the configured path (compat with run.sh)
client_secret_env = os.getenv("GOOGLE_CLIENT_SECRET_JSON")
if client_secret_env:
    try:
        if not GOOGLE_CLIENT_SECRET_PATH.exists() or GOOGLE_CLIENT_SECRET_PATH.stat().st_size == 0:
            GOOGLE_CLIENT_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(GOOGLE_CLIENT_SECRET_PATH, "w", encoding="utf-8") as f:
                f.write(client_secret_env)
            try:
                os.chmod(GOOGLE_CLIENT_SECRET_PATH, 0o600)
            except Exception:
                # chmod may not be available on Windows or some containers; ignore failures
                pass
            print(f"Wrote GOOGLE_CLIENT_SECRET_JSON to {GOOGLE_CLIENT_SECRET_PATH}")
        else:
            print(f"Client secret file already exists at {GOOGLE_CLIENT_SECRET_PATH}; not overwriting")
    except Exception as e:
        print(f"Failed to write GOOGLE_CLIENT_SECRET_JSON to {GOOGLE_CLIENT_SECRET_PATH}: {e}")


def _get_creds_filename(user_id: int, group_id: int) -> str:
    """Generate a credential filename for a specific user and group."""
    return f"google_photos_creds_{user_id}_{group_id}.pickle"


def _clear_expired_creds(user_id: int, group_id: int) -> None:
    """Delete expired credential file to force fresh OAuth flow."""
    creds_filename = _get_creds_filename(user_id, group_id)
    creds_path = CREDS_DIR / creds_filename
    if creds_path.exists():
        creds_path.unlink(missing_ok=True)
        print(f"Cleared expired credentials for user {user_id}, group {group_id}")


class OAuthTimeoutError(Exception):
    """Raised when OAuth flow times out."""
    pass


class CredentialRefreshError(Exception):
    """Raised when credential refresh fails and user must re-authorize via /start."""
    pass


def _resolve_client_secret_path() -> Path:
    """Choose the configured OAuth client secret file, with a compatibility fallback."""
    if GOOGLE_CLIENT_SECRET_PATH.exists():
        return GOOGLE_CLIENT_SECRET_PATH

    compatibility_path = CREDS_DIR / "credentials.json"
    if compatibility_path.exists():
        return compatibility_path

    return GOOGLE_CLIENT_SECRET_PATH


def build_oauth_authorization_url(user_id: int, group_id: int) -> str:
    """Build a Google OAuth authorization link that can be shared via Telegram."""
    flow = Flow.from_client_secrets_file(
        str(_resolve_client_secret_path()),
        scopes=SCOPES,
        redirect_uri=get_oauth_redirect_uri(),
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=f"{user_id}:{group_id}",
    )
    return auth_url


def _load_or_create_creds(user_id: int, group_id: int):
    """Load cached credentials or run full OAuth flow if cache doesn't exist."""
    creds_filename = _get_creds_filename(user_id, group_id)
    creds_path = CREDS_DIR / creds_filename
    print(f"Looking for credentials at: {creds_path}")
    
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

    raise RuntimeError(
        "Interactive OAuth setup now uses the /oauth2callback flow. Run /start and complete authorization through the bot link."
    )


def _build_photos_service(user_id: int, group_id: int):
    print(f"Building Google Photos service for user {user_id}, group {group_id}")
    creds = _load_or_create_creds(user_id, group_id)
    service = build("photoslibrary", "v1", credentials=creds, static_discovery=False)
    print(f"Built Google Photos service for user {user_id}, group {group_id}")
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
    print(f"Creating album for group {group_id} with title '{album_title}' and credentials file '{creds_filename}'")
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
        print(f"Database query for existing album returned: {result}")

        existing_album_id = result[0] if result and result[0] else None
        print(f"Existing album ID from database for group {group_id}: {existing_album_id}")
        if existing_album_id:
            # Clear any expired credentials to force fresh OAuth flow.
            # If the user runs /start, they want a fresh login, not to reuse a broken token.
            _clear_expired_creds(user_id, group_id)
            print(f"Cleared expired credentials for user {user_id}, group {group_id} to reuse existing album {existing_album_id}")
            # Now load or create fresh credentials.
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