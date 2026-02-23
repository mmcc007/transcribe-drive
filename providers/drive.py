"""Google Drive storage provider for transcribe_drive."""
from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build as build_service
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload


SCOPES = [
    "https://www.googleapis.com/auth/drive",
]

MOV_MIME_TYPES = [
    "video/quicktime",
    "video/mp4",
    "video/x-msvideo",
    "video/x-matroska",
]


class DriveProvider:
    """Google Drive implementation of the StorageProvider interface."""

    def __init__(self, script_dir: Path) -> None:
        self.script_dir = script_dir

    # -- auth ----------------------------------------------------------------

    def connect(self):
        """Build an authenticated Drive API service using OAuth client credentials."""
        client_secret = self.script_dir / "transcribe_client_secret.json"
        token_path = self.script_dir / ".transcribe_drive_token.json"
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(AuthRequest())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(client_secret), SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())
            os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        self._creds = creds
        return build_service("drive", "v3", credentials=creds)

    # -- folder / file helpers -----------------------------------------------

    def extract_folder_ref(self, url_or_id: str) -> str:
        """Extract a Google Drive folder ID from a URL or bare ID."""
        m = re.search(r"folders/([a-zA-Z0-9_-]+)", url_or_id)
        if m:
            return m.group(1)
        m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url_or_id)
        if m:
            return m.group(1)
        return url_or_id.strip()

    def get_file_metadata(self, service, file_id: str) -> dict:
        """Return metadata dict for a single Drive file."""
        return service.files().get(
            fileId=file_id,
            fields=(
                "id, name, size, mimeType, createdTime,"
                " modifiedTime, webViewLink"
            ),
        ).execute()

    def list_video_files(self, service, folder_id: str,
                         _path: str = "") -> list[dict]:
        """List video files in a Drive folder, recursing into subfolders."""
        mime_query = " or ".join(f"mimeType='{m}'" for m in MOV_MIME_TYPES)
        query = f"'{folder_id}' in parents and ({mime_query}) and trashed=false"
        files: list[dict] = []
        page_token = None
        while True:
            resp = service.files().list(
                q=query,
                fields=(
                    "nextPageToken, files(id, name, size, mimeType,"
                    " createdTime, modifiedTime, webViewLink)"
                ),
                pageSize=100,
                orderBy="name",
                pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                if f["name"].startswith("._"):
                    continue
                f["_folder_path"] = _path
                files.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        # Recurse into subfolders
        sub_query = (
            f"'{folder_id}' in parents"
            " and mimeType='application/vnd.google-apps.folder'"
            " and trashed=false"
        )
        page_token = None
        while True:
            resp = service.files().list(
                q=sub_query,
                fields="nextPageToken, files(id, name)",
                pageSize=100,
                orderBy="name",
                pageToken=page_token,
            ).execute()
            for sf in resp.get("files", []):
                sub_path = f"{_path}{sf['name']}/" if _path else f"{sf['name']}/"
                files.extend(self.list_video_files(service, sf["id"], sub_path))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return files

    def get_folder_name(self, service, folder_id: str) -> str:
        """Return the Drive folder's display name."""
        meta = service.files().get(fileId=folder_id, fields="name").execute()
        return meta["name"]

    def list_existing_transcripts(self, service, folder_id: str) -> set[str]:
        """Return set of base names that already have transcripts."""
        query = (
            f"'{folder_id}' in parents and mimeType='text/plain' and trashed=false"
        )
        names: set[str] = set()
        page_token = None
        while True:
            resp = service.files().list(
                q=query,
                fields="nextPageToken, files(name)",
                pageSize=100,
                pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                names.add(Path(f["name"]).stem)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return names

    # -- download / stream ---------------------------------------------------

    def stream_audio(self, service, file_id: str, audio_path: Path) -> None:
        """Stream audio extraction directly from Drive via ffmpeg."""
        self._creds.refresh(AuthRequest())

        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        print(f"  Streaming audio extraction → {audio_path.name}...")
        result = subprocess.run(
            [
                "ffmpeg",
                "-headers", f"Authorization: Bearer {self._creds.token}\r\n",
                "-i", url,
                "-vn",
                "-acodec", "libmp3lame",
                "-q:a", "2",
                "-y",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if "403" in result.stderr or "Forbidden" in result.stderr:
                raise PermissionError(
                    "Drive download quota exceeded (will retry with full download)"
                )
            print(f"  ffmpeg stream error:\n{result.stderr[-500:]}")
            raise RuntimeError("ffmpeg streaming audio extraction failed")
        if not audio_path.exists() or audio_path.stat().st_size < 1000:
            raise RuntimeError("Streaming produced empty/corrupt audio")
        size_mb = audio_path.stat().st_size / 1e6
        print(f"  Audio extracted: {size_mb:.1f} MB (streamed)")

    def download_file(self, service, file_id: str, dest_path: Path) -> None:
        """Download a Drive file to local disk with progress."""
        request = service.files().get_media(fileId=file_id)
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request, chunksize=50 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    print(f"  Downloading... {pct}%", end="\r")
        print(
            f"  Downloaded: {dest_path.name}"
            f" ({dest_path.stat().st_size / 1e9:.1f} GB)"
        )

    # -- upload --------------------------------------------------------------

    def upload_file(self, service, folder_id: str, local_path: Path,
                    mime_type: str, file_id: str | None = None) -> str:
        """Upload a local file to a Drive folder.  Returns file ID."""
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
        if file_id:
            f = service.files().update(
                fileId=file_id, media_body=media, fields="id"
            ).execute()
        else:
            meta = {"name": local_path.name, "parents": [folder_id]}
            f = service.files().create(
                body=meta, media_body=media, fields="id"
            ).execute()
        return f["id"]

    # -- manifest ------------------------------------------------------------

    def load_manifest(self, service,
                      output_folder_id: str) -> tuple[dict, str | None]:
        """Download manifest.json from Drive output folder."""
        query = (
            f"'{output_folder_id}' in parents"
            " and name='manifest.json' and trashed=false"
        )
        resp = service.files().list(q=query, fields="files(id)").execute()
        existing = resp.get("files", [])
        if existing:
            manifest_file_id = existing[0]["id"]
            request = service.files().get_media(fileId=manifest_file_id)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buf.seek(0)
            manifest = json.loads(buf.read().decode("utf-8"))
            return manifest, manifest_file_id
        return {
            "source_folder_id": output_folder_id,
            "generated_by": "transcribe_drive",
            "files": [],
        }, None

    def save_manifest(self, service, output_folder_id: str, manifest: dict,
                      manifest_file_id: str | None) -> str:
        """Write manifest.json to Drive."""
        tmp_dir = Path("/tmp/transcribe_drive")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / "manifest.json"
        tmp_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        file_id = self.upload_file(
            service, output_folder_id, tmp_path, "application/json",
            file_id=manifest_file_id,
        )
        tmp_path.unlink(missing_ok=True)
        return file_id

    # -- subfolders ----------------------------------------------------------

    def ensure_subfolder(self, service, parent_id: str, name: str) -> str:
        """Find or create a subfolder under parent_id."""
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents"
            " and mimeType='application/vnd.google-apps.folder'"
            f" and name='{safe_name}' and trashed=false"
        )
        resp = service.files().list(q=query, fields="files(id)").execute()
        existing = resp.get("files", [])
        if existing:
            return existing[0]["id"]
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(body=meta, fields="id").execute()
        return folder["id"]
