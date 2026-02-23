# transcribe_drive

Batch transcription system for video files on Google Drive or Dropbox using Google Gemini for speaker-diarized transcription.

## Overview

Takes a cloud folder (Google Drive or Dropbox) of video files (.mov, .mp4, .avi, .mkv), extracts audio, sends it to Gemini for speaker-diarized transcription, and uploads the results (transcripts + mp3 audio) back to an output folder. Tracks progress via a manifest so batches can be interrupted and resumed.

## Architecture

```
Cloud Storage (source)         Local / VM                      Cloud Storage (output)
┌──────────────┐       ┌─────────────────────────┐       ┌──────────────────┐
│ Drive or     │──────>│ ffmpeg stream extraction │──────>│ Output folder    │
│ Dropbox      │ HTTP  │ (video→mp3 over HTTP)   │       │  transcripts/    │
│ video files  │       │                         │       │  audio/          │
└──────────────┘       │ Gemini 2.5 Pro API      │       │  manifest.json   │
                       │ (audio→transcript)      │──────>│                  │
                       └─────────────────────────┘       └──────────────────┘
```

## Prerequisites

- Python 3.10+
- ffmpeg
- A Gemini API key (with billing for Gemini 2.5 Pro)
- **For Google Drive**: OAuth 2.0 client credentials (Desktop app type) for Drive access
- **For Dropbox**: A Dropbox app with `files.metadata.read`, `files.content.read`, `files.content.write` permissions

## Setup

### Common setup

1. Clone this repo and put `transcribe_drive` somewhere on your `$PATH` (e.g. `~/bin/`)
2. Copy `transcribe.env.example` to `transcribe.env` in the same directory as the script
3. Fill in your `GEMINI_API_KEY`

### Google Drive setup

4. Place your OAuth client credentials JSON as `transcribe_client_secret.json` next to the script
5. On first run, a browser window opens for Google OAuth login

### Dropbox setup

4. Create a Dropbox app at https://www.dropbox.com/developers/apps (Full Dropbox access)
5. Add `DROPBOX_APP_KEY` and `DROPBOX_APP_SECRET` to `transcribe.env`
6. On first run, you'll be prompted to visit a URL and paste an authorization code
7. Refresh tokens are saved to `.transcribe_dropbox_token.json` (they don't expire)

The script auto-creates a Python venv and installs dependencies (`google-genai`, `google-auth-oauthlib`, `google-api-python-client`, `dropbox`).

## Usage

```bash
# --- Google Drive (default) ---

# List video files in a Drive folder
transcribe_drive list <folder_url_or_id>

# Transcribe a single file
transcribe_drive transcribe <file_id> [--output-folder DRIVE_FOLDER_ID]

# Batch transcribe all files in a folder
transcribe_drive batch <folder_url_or_id> --output-folder DRIVE_FOLDER_ID [--budget DOLLARS]

# Filter by filename (process only files matching a substring, case-insensitive)
transcribe_drive batch <folder_url_or_id> --output-folder DRIVE_FOLDER_ID --filter "episode_01"

# --- Dropbox ---

# List video files in a Dropbox folder (bare path or shared folder URL)
transcribe_drive list /Videos --source dropbox
transcribe_drive list "https://www.dropbox.com/scl/fo/..." --source dropbox

# Transcribe a single file
transcribe_drive transcribe /Videos/meeting.mov --source dropbox --output-folder /Output

# Batch transcribe
transcribe_drive batch /Videos --source dropbox --output-folder /Output --budget 5

# Cross-provider: Dropbox source → Drive output
transcribe_drive batch "https://www.dropbox.com/scl/fo/..." \
  --source dropbox --output-folder DRIVE_FOLDER_ID --budget 20
```

The `--source` flag accepts `drive` or `dropbox`. If omitted, the source is auto-detected from the URL/path (Drive URLs → drive, Dropbox URLs or `/`-prefixed paths → dropbox, bare IDs → drive).

**Output subfolders**: when `--output-folder` is set, the batch automatically creates a subfolder named after the source folder (e.g. `Season_6/`) inside the output root, with `transcripts/` and `audio/` subdirectories inside it. This keeps multiple batch runs to the same output root from colliding.

### Running a batch on a VM

```bash
# Start batch in tmux (survives SSH disconnect)
tmux new-session -d -s batch
tmux send-keys -t batch 'cd ~/bin && python3 -u transcribe_drive batch <SOURCE_FOLDER_ID> \
  --output-folder <OUTPUT_FOLDER_ID> --budget 40 2>&1 | tee /tmp/batch.log' Enter

# Monitor progress
tail -20 /tmp/batch.log
```

### Auto-retry for quota-blocked batches

When Drive download quotas block files, use `transcribe_retry.sh` to automatically retry on a schedule:

```bash
# Configure env vars
export SOURCE_FOLDER="your-source-folder-id"
export OUTPUT_FOLDER="your-output-folder-id"
export BUDGET=40
export NTFY_TOPIC="my-transcribe-topic"  # optional
export NTFY_EMAIL="you@example.com"       # optional

# Add to cron (every 6 hours)
echo '30 */6 * * * SOURCE_FOLDER=xxx OUTPUT_FOLDER=yyy /path/to/transcribe_retry.sh >> /tmp/transcribe_retry/cron.log 2>&1' | crontab -
```

The retry script:
- Uses a lockfile to prevent overlapping runs
- Sends ntfy notifications when files succeed
- Auto-removes itself from cron when all files are complete
- Keeps the last 10 log files

## How it works

### Audio extraction (two methods)

1. **Streaming** (preferred, ~7x faster): ffmpeg reads the video directly over HTTP, extracting only the audio track. No full video download needed.
   - **Drive**: uses an OAuth bearer token in the request header
   - **Dropbox (owned files)**: uses a short-lived temporary link (no auth header needed)
   - **Dropbox (shared folder files)**: uses the `sharing/get_shared_link_file` content API endpoint with a bearer token — this endpoint supports `Accept-Ranges: bytes`, allowing ffmpeg to seek to the moov atom even for `.mov` files with metadata at the end

2. **Full download** (fallback): Downloads the entire video file to disk, then runs ffmpeg locally. Used when streaming fails (e.g. Drive download quota exceeded).

The script tries streaming first and automatically falls back to full download on failure.

### Transcription

Audio is uploaded to the Gemini Files API, then Gemini 2.5 Pro generates a speaker-diarized transcript with timestamps. The prompt instructs Gemini to:
- Label speakers consistently (Speaker 1, Speaker 2, etc.)
- Identify speakers by name when possible from context
- Include `[MM:SS]` timestamps at each speaker turn
- Transcribe all speech without omission

### Manifest & resume

A `manifest.json` in the output folder tracks every completed file by source file ID. On restart, the batch skips any file already in the manifest. This means you can kill and restart freely.

### Budget control

- `--budget N` sets a hard dollar cap; the batch stops before exceeding it
- If no budget is set, auto-budget is calculated as 2x the extrapolated cost after the first file
- Cost is estimated before each file and checked against the remaining budget

### Notifications

Set `NTFY_TOPIC` in your env to receive push notifications via [ntfy.sh](https://ntfy.sh) when a batch completes. Optionally set `NTFY_EMAIL` for email notifications too.

## Costs

**Gemini 2.5 Pro pricing** (as of Feb 2026):
- Input: $1.25 / 1M tokens
- Output: $10.00 / 1M tokens
- Rough estimate: ~32 tokens per second of audio

**Observed costs**:
- Average: ~$0.40/file, ~$0.31/hour of audio
- 1-hour file: ~$0.30-0.50
- Full 59-file batch: ~$25-40

## Output format

Each transcript is a `.txt` file with a metadata header:

```
# Source: My Video.mov
# Source ID: 1abc...
# Source URL: https://drive.google.com/file/d/1abc.../view
# Source Created: 2024-03-15T...
# Source Modified: 2024-03-15T...
# Audio Duration: 1:02:34
# Transcribed: 2026-02-07T...
# ---

[00:00] Speaker 1 (Host): Welcome to the show...
[00:15] Speaker 2: Thanks for having me...
```

## OAuth setup

The script uses OAuth (not a service account) for Drive access. First run triggers a browser-based login flow.

**Scopes**: `drive.readonly` (read source files) + `drive.file` (write to output folder)

**To switch accounts**: delete `.transcribe_drive_token.json` (Drive) or `.transcribe_dropbox_token.json` (Dropbox) and re-run.

## Dropbox vs Drive comparison

| Aspect | Google Drive | Dropbox |
|--------|-------------|---------|
| File identifiers | Opaque IDs | Filesystem paths |
| Streaming URL | Needs `Authorization: Bearer` header | Temp link, no auth needed |
| Download quota | Per-file global quota (24h reset) | None |
| Folder listing | Query-based | Path-based, recursive |
| Created time | Available | Not available |
| Upload large files | MediaFileUpload | Chunked sessions (>150MB) |

## Known issues & limitations

### Drive download quota (per-file, global)

Google Drive enforces per-file download quotas across ALL users. Heavy download activity (including repeated streaming attempts) can exhaust a file's quota for ~24 hours. When this happens, both streaming and full-download methods return 403. The only fix is to wait for the quota to reset.

### Gemini API timeout

Long files (2+ hours) can take 5-10 minutes for Gemini to process. The client is configured with a 10-minute HTTP timeout (`http_options={"timeout": 600_000}`). Without this, requests hang indefinitely.

### Drive API transfer speed

Drive API caps at ~10-11 MB/s per file regardless of network bandwidth. Streaming extraction helps because ffmpeg only needs to read enough of the video to extract the audio track.

### Model quality notes

- **Gemini 2.5 Flash**: Fast and cheap but produced garbled output on files longer than ~90 minutes. Speaker diarization degraded significantly.
- **Gemini 2.5 Pro**: ~4x more expensive but vastly superior transcript quality. Occasionally loses speaker labels briefly but recovers. Recommended for production use.

## License

MIT
