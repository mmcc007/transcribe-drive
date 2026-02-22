"""Dropbox storage provider for transcribe_drive."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import WriteMode


# Video extensions to look for (Dropbox doesn't have MIME-based queries)
VIDEO_EXTENSIONS = {".mov", ".mp4", ".avi", ".mkv"}

# Dropbox chunked upload threshold (150 MB)
CHUNK_SIZE = 150 * 1024 * 1024


class DropboxProvider:
    """Dropbox implementation of the StorageProvider interface."""

    def __init__(self, script_dir: Path) -> None:
        self.script_dir = script_dir
        self.token_path = script_dir / ".transcribe_dropbox_token.json"

    # -- auth ----------------------------------------------------------------

    def connect(self):
        """Return an authenticated Dropbox client.

        Uses OAuth2 with offline refresh tokens.  On first run, prints an
        auth URL for the user to visit and paste the authorization code.
        """
        import os

        app_key = os.getenv("DROPBOX_APP_KEY")
        app_secret = os.getenv("DROPBOX_APP_SECRET")
        if not app_key or not app_secret:
            raise SystemExit(
                "ERROR: DROPBOX_APP_KEY and DROPBOX_APP_SECRET must be set "
                "in transcribe.env"
            )

        # Try loading saved refresh token
        if self.token_path.exists():
            token_data = json.loads(self.token_path.read_text())
            refresh_token = token_data.get("refresh_token")
            if refresh_token:
                dbx = dropbox.Dropbox(
                    oauth2_refresh_token=refresh_token,
                    app_key=app_key,
                    app_secret=app_secret,
                )
                # Validate the connection
                try:
                    dbx.users_get_current_account()
                    return dbx
                except AuthError:
                    print("  Saved Dropbox token is invalid, re-authenticating...")

        # OAuth2 flow (no redirect)
        flow = dropbox.DropboxOAuth2FlowNoRedirect(
            app_key, app_secret, token_access_type="offline"
        )
        authorize_url = flow.start()
        print(f"\n1. Go to: {authorize_url}")
        print("2. Click 'Allow' (you may have to log in first)")
        print("3. Copy the authorization code.\n")
        auth_code = input("Enter the authorization code: ").strip()

        result = flow.finish(auth_code)
        # Save refresh token for future runs (restricted permissions)
        self.token_path.write_text(json.dumps({
            "refresh_token": result.refresh_token,
        }))
        os.chmod(self.token_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        print("  Dropbox token saved for future use.")

        return dropbox.Dropbox(
            oauth2_refresh_token=result.refresh_token,
            app_key=app_key,
            app_secret=app_secret,
        )

    # -- folder / file helpers -----------------------------------------------

    def extract_folder_ref(self, url_or_path: str) -> str:
        """Parse a Dropbox URL or bare path into a canonical folder path.

        Accepts:
        - Bare paths: /Videos, /My Folder/SubFolder
        - Dropbox home URLs: https://www.dropbox.com/home/Videos
        - Dropbox legacy shared URLs: https://www.dropbox.com/sh/...
        - Dropbox shared folder links: https://www.dropbox.com/scl/fo/...
          (returned as-is; list_video_files resolves via SharedLink API)
        """
        import re
        import urllib.parse

        # New-style shared folder links (/scl/fo/) — return URL unchanged;
        # list_video_files will pass it as a SharedLink to the API.
        if re.search(r"dropbox\.com/scl/fo/", url_or_path):
            return url_or_path.strip()

        # Dropbox home URLs
        m = re.search(r"dropbox\.com/home(/[^?#]*)", url_or_path)
        if m:
            return urllib.parse.unquote(m.group(1))

        # Legacy shared URLs (/sh/ or /s/)
        m = re.search(r"dropbox\.com/sh?/[^/]+/[^/]+(/[^?#]*)?", url_or_path)
        if m and m.group(1):
            return urllib.parse.unquote(m.group(1))

        # Assume it's already a bare path
        path = url_or_path.strip()
        if path == "/":
            return ""
        return path

    def get_file_metadata(self, service, file_path: str) -> dict:
        """Return metadata dict for a single Dropbox file."""
        meta = service.files_get_metadata(file_path)
        return {
            "id": meta.path_display,
            "name": meta.name,
            "size": getattr(meta, "size", 0),
            "mimeType": "",
            "createdTime": "",
            "modifiedTime": (
                meta.client_modified.isoformat() + "Z"
                if hasattr(meta, "client_modified") and meta.client_modified
                else ""
            ),
            "webViewLink": "",
            "_folder_path": str(Path(meta.path_display).parent) + "/",
        }

    def list_video_files(self, service, folder_path: str,
                         _path: str = "") -> list[dict]:
        """List video files under *folder_path*, recursing into subfolders.

        For bare Dropbox paths, uses recursive=True for efficiency.
        For shared folder links (https://), recurses manually because
        the Dropbox API does not support recursive=True with shared links.
        """
        is_shared_link = folder_path.startswith("https://")

        if is_shared_link:
            return self._list_shared_link_recursive(service, folder_path)

        files: list[dict] = []
        try:
            result = service.files_list_folder(folder_path, recursive=True)
        except ApiError as e:
            print(f"  Dropbox API error listing folder: {e}")
            return files

        while True:
            for entry in result.entries:
                if not isinstance(entry, dropbox.files.FileMetadata):
                    continue
                ext = Path(entry.name).suffix.lower()
                if ext not in VIDEO_EXTENSIONS:
                    continue
                if entry.name.startswith("._"):
                    continue

                rel_path = entry.path_display
                if folder_path:
                    rel_path = rel_path[len(folder_path):]
                rel_dir = str(Path(rel_path).parent).lstrip("/")
                if rel_dir and not rel_dir.endswith("/"):
                    rel_dir += "/"
                elif rel_dir == ".":
                    rel_dir = ""

                files.append({
                    "id": entry.path_display,
                    "name": entry.name,
                    "size": entry.size,
                    "mimeType": "",
                    "createdTime": "",
                    "modifiedTime": (
                        entry.client_modified.isoformat() + "Z"
                        if entry.client_modified else ""
                    ),
                    "webViewLink": "",
                    "_folder_path": rel_dir,
                })

            if not result.has_more:
                break
            result = service.files_list_folder_continue(result.cursor)

        files.sort(key=lambda f: f["name"])
        return files

    def _list_shared_link_recursive(self, service, shared_url: str,
                                    subfolder: str = "",
                                    _rel_prefix: str = "") -> list[dict]:
        """Recursively list a shared folder link by manually walking subfolders."""
        shared_link = dropbox.files.SharedLink(url=shared_url)
        files: list[dict] = []
        try:
            result = service.files_list_folder(
                path=subfolder, shared_link=shared_link
            )
        except ApiError as e:
            print(f"  Dropbox API error listing shared folder '{subfolder}': {e}")
            return files

        subfolders = []
        while True:
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FolderMetadata):
                    # path_display may be None for shared link listings;
                    # construct path from parent subfolder + entry name
                    sf_path = (
                        entry.path_display
                        if entry.path_display
                        else f"{subfolder}/{entry.name}"
                    )
                    subfolders.append((sf_path, entry.name))
                elif isinstance(entry, dropbox.files.FileMetadata):
                    ext = Path(entry.name).suffix.lower()
                    if ext not in VIDEO_EXTENSIONS:
                        continue
                    if entry.name.startswith("._"):
                        continue
                    # Construct a stable path-based ID for use in subsequent API calls
                    file_path = (
                        entry.path_display
                        if entry.path_display
                        else f"{subfolder}/{entry.name}"
                    )
                    files.append({
                        "id": file_path,
                        "name": entry.name,
                        "size": entry.size,
                        "mimeType": "",
                        "createdTime": "",
                        "modifiedTime": (
                            entry.client_modified.isoformat() + "Z"
                            if entry.client_modified else ""
                        ),
                        "webViewLink": shared_url,
                        "_folder_path": _rel_prefix,
                    })
            if not result.has_more:
                break
            result = service.files_list_folder_continue(result.cursor)

        # Recurse into subfolders
        for sf_path, sf_name in subfolders:
            rel = f"{_rel_prefix}{sf_name}/"
            files.extend(
                self._list_shared_link_recursive(service, shared_url, sf_path, rel)
            )

        files.sort(key=lambda f: f["name"])
        return files

    # -- download / stream ---------------------------------------------------

    def stream_audio(self, service, file_path: str, audio_path: Path) -> None:
        """Stream audio extraction from Dropbox via a temporary link.

        Dropbox temporary links are public URLs valid for 4 hours — no auth
        headers needed for ffmpeg.
        """
        link_result = service.files_get_temporary_link(file_path)
        url = link_result.link

        print(f"  Streaming audio extraction → {audio_path.name}...")
        result = subprocess.run(
            [
                "ffmpeg",
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
            print(f"  ffmpeg stream error:\n{result.stderr[-500:]}")
            raise RuntimeError("ffmpeg streaming audio extraction failed")
        if not audio_path.exists() or audio_path.stat().st_size < 1000:
            raise RuntimeError("Streaming produced empty/corrupt audio")
        size_mb = audio_path.stat().st_size / 1e6
        print(f"  Audio extracted: {size_mb:.1f} MB (streamed)")

    def download_file(self, service, file_path: str, dest_path: Path) -> None:
        """Download a Dropbox file to local disk."""
        print(f"  Downloading {Path(file_path).name}...")
        service.files_download_to_file(str(dest_path), file_path)
        print(
            f"  Downloaded: {dest_path.name}"
            f" ({dest_path.stat().st_size / 1e9:.1f} GB)"
        )

    # -- upload --------------------------------------------------------------

    def upload_file(self, service, folder_path: str, local_path: Path,
                    mime_type: str, file_id: str | None = None) -> str:
        """Upload a local file to a Dropbox folder.  Returns the path."""
        dest_path = f"{folder_path}/{local_path.name}"
        file_size = local_path.stat().st_size

        if file_size <= CHUNK_SIZE:
            with open(local_path, "rb") as f:
                service.files_upload(
                    f.read(), dest_path, mode=WriteMode.overwrite
                )
        else:
            # Chunked upload for large files
            with open(local_path, "rb") as f:
                chunk = f.read(CHUNK_SIZE)
                session = service.files_upload_session_start(chunk)
                cursor = dropbox.files.UploadSessionCursor(
                    session_id=session.session_id, offset=len(chunk)
                )
                while cursor.offset < file_size:
                    chunk = f.read(CHUNK_SIZE)
                    if cursor.offset + len(chunk) < file_size:
                        service.files_upload_session_append_v2(chunk, cursor)
                        cursor.offset += len(chunk)
                    else:
                        commit = dropbox.files.CommitInfo(
                            path=dest_path, mode=WriteMode.overwrite
                        )
                        service.files_upload_session_finish(
                            chunk, cursor, commit
                        )
                        return dest_path

                # Exact multiple of CHUNK_SIZE — finish with empty data
                commit = dropbox.files.CommitInfo(
                    path=dest_path, mode=WriteMode.overwrite
                )
                service.files_upload_session_finish(b"", cursor, commit)

        return dest_path

    # -- manifest ------------------------------------------------------------

    def load_manifest(self, service,
                      folder_path: str) -> tuple[dict, str | None]:
        """Download manifest.json from Dropbox folder."""
        manifest_path = f"{folder_path}/manifest.json"
        try:
            _, response = service.files_download(manifest_path)
            manifest = json.loads(response.content)
            return manifest, manifest_path
        except ApiError:
            return {
                "source_folder_id": folder_path,
                "generated_by": "transcribe_drive",
                "files": [],
            }, None

    def save_manifest(self, service, folder_path: str, manifest: dict,
                      manifest_file_id: str | None) -> str:
        """Write manifest.json to Dropbox."""
        manifest_path = f"{folder_path}/manifest.json"
        data = json.dumps(manifest, indent=2).encode("utf-8")
        service.files_upload(data, manifest_path, mode=WriteMode.overwrite)
        return manifest_path

    # -- subfolders ----------------------------------------------------------

    def ensure_subfolder(self, service, parent_path: str, name: str) -> str:
        """Find or create a subfolder under parent_path.  Returns the path."""
        folder_path = f"{parent_path}/{name}"
        try:
            service.files_get_metadata(folder_path)
        except ApiError:
            try:
                service.files_create_folder_v2(folder_path)
            except ApiError:
                pass  # May already exist due to race
        return folder_path

    def get_folder_name(self, service, folder_ref: str) -> str:
        """Return the display name of the folder.

        For shared links, resolves via the sharing API.
        For bare paths, returns the last path component.
        """
        if folder_ref.startswith("https://"):
            meta = service.sharing_get_shared_link_metadata(folder_ref)
            return meta.name
        return Path(folder_ref.rstrip("/")).name or folder_ref

    def list_existing_transcripts(self, service, folder_path: str) -> set[str]:
        """Return base-names that already have transcripts."""
        names: set[str] = set()
        try:
            result = service.files_list_folder(folder_path)
            while True:
                for entry in result.entries:
                    if isinstance(entry, dropbox.files.FileMetadata):
                        if entry.name.endswith(".txt"):
                            names.add(Path(entry.name).stem)
                if not result.has_more:
                    break
                result = service.files_list_folder_continue(result.cursor)
        except ApiError:
            pass
        return names
