"""Storage provider interface and factory for transcribe_drive.

Each provider implements the same interface so the main script stays
cloud-agnostic.  Currently supported: Google Drive, Dropbox.
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable, Any
from pathlib import Path


@runtime_checkable
class StorageProvider(Protocol):
    """Interface that every cloud-storage provider must implement."""

    def connect(self) -> Any:
        """Return an authenticated client/service object."""
        ...

    def extract_folder_ref(self, url_or_id: str) -> str:
        """Parse a URL or bare identifier into the canonical folder reference."""
        ...

    def get_file_metadata(self, service: Any, file_id: str) -> dict:
        """Return metadata dict for a single file.

        Must include at least: id, name, size.
        May include: mimeType, createdTime, modifiedTime, webViewLink.
        """
        ...

    def list_video_files(self, service: Any, folder_ref: str,
                         _path: str = "") -> list[dict]:
        """List video files under *folder_ref*, recursing into subfolders.

        Returns a list of dicts with at least:
            id, name, size, mimeType, createdTime, modifiedTime,
            webViewLink (optional), _folder_path
        """
        ...

    def stream_audio(self, service: Any, file_id: str,
                     audio_path: Path) -> None:
        """Stream-extract audio from *file_id* directly into *audio_path*."""
        ...

    def download_file(self, service: Any, file_id: str,
                      dest_path: Path) -> None:
        """Full-download fallback."""
        ...

    def upload_file(self, service: Any, folder_id: str, local_path: Path,
                    mime_type: str, file_id: str | None = None) -> str:
        """Upload *local_path* into *folder_id*.  Returns the new file ID."""
        ...

    def load_manifest(self, service: Any,
                      folder_id: str) -> tuple[dict, str | None]:
        """Download manifest.json.  Returns (manifest_dict, file_id | None)."""
        ...

    def save_manifest(self, service: Any, folder_id: str, manifest: dict,
                      manifest_file_id: str | None) -> str:
        """Write manifest.json.  Returns the file ID on the remote."""
        ...

    def ensure_subfolder(self, service: Any, parent_id: str,
                         name: str) -> str:
        """Find or create a subfolder.  Returns the subfolder ID/path."""
        ...

    def list_existing_transcripts(self, service: Any,
                                  folder_id: str) -> set[str]:
        """Return base-names that already have transcripts."""
        ...


def detect_provider(url_or_ref: str) -> str:
    """Auto-detect provider name from a URL or reference string.

    Returns ``"drive"`` or ``"dropbox"``.  Falls back to ``"drive"``
    for bare IDs (backward-compatible).
    """
    if re.search(r"drive\.google\.com|docs\.google\.com", url_or_ref):
        return "drive"
    if re.search(r"dropbox\.com", url_or_ref):
        return "dropbox"
    # A Dropbox path always starts with "/"
    if url_or_ref.startswith("/"):
        return "dropbox"
    # Bare alphanumeric ID → assume Drive for backward compat
    return "drive"


def get_provider(name: str, script_dir: Path) -> StorageProvider:
    """Factory: return a provider instance by name."""
    if name == "drive":
        from providers.drive import DriveProvider
        return DriveProvider(script_dir)
    if name == "dropbox":
        from providers.dropbox_provider import DropboxProvider
        return DropboxProvider(script_dir)
    raise ValueError(f"Unknown provider: {name!r} (expected 'drive' or 'dropbox')")
