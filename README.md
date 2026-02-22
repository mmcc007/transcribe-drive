# transcribe_drive

Batch transcription system for video files on Google Drive using Google Gemini for speaker-diarized transcription.

## Overview

Takes a Google Drive folder of video files (.mov, .mp4, .avi, .mkv), extracts audio, sends it to Gemini for speaker-diarized transcription, and uploads the results (transcripts + mp3 audio) back to a Drive output folder. Tracks progress via a manifest so batches can be interrupted and resumed.

## Architecture

```
Google Drive (source)          Local / VM                      Google Drive (output)
┌──────────────┐       ┌─────────────────────────┐       ┌──────────────────┐
│ Source folder │──────>│ ffmpeg stream extraction │──────>│ Output folder    │
│  .mov/.mp4   │ Drive │ (video→mp3 over HTTP)   │       │  transcripts/    │
│  files       │  API  │                         │       │  audio/          │
└──────────────┘       │ Gemini 2.5 Pro API      │       │  manifest.json   │
                       │ (audio→transcript)      │──────>│                  │
                       └─────────────────────────┘       └──────────────────┘
```

## Prerequisites

- Python 3.10+
- ffmpeg
- A Google Cloud project with:
  - Gemini API enabled and an API key (with billing for Gemini 2.5 Pro)
  - OAuth 2.0 client credentials (Desktop app type) for Drive access

## Setup

1. Clone this repo and put `transcribe_drive` somewhere on your `$PATH` (e.g. `~/bin/`)
2. Copy `transcribe.env.example` to `transcribe.env` in the same directory as the script
3. Fill in your `GEMINI_API_KEY`
4. Place your OAuth client credentials JSON as `transcribe_client_secret.json` next to the script
5. On first run, a browser window opens for Google OAuth login

The script auto-creates a Python venv and installs dependencies (`google-genai`, `google-auth-oauthlib`, `google-api-python-client`).

## Usage

```bash
# List video files in a Drive folder
transcribe_drive list <folder_url_or_id>

# Transcribe a single file
transcribe_drive transcribe <file_id> [--output-folder DRIVE_FOLDER_ID]

# Batch transcribe all files in a folder
transcribe_drive batch <folder_url_or_id> --output-folder DRIVE_FOLDER_ID [--budget DOLLARS]
```

### Running a batch on a VM

```bash
# Start batch in tmux (survives SSH disconnect)
tmux new-session -d -s batch
tmux send-keys -t batch 'cd ~/bin && python3 -u transcribe_drive batch <SOURCE_FOLDER_ID> \
  --output-folder <OUTPUT_FOLDER_ID> --budget 40 2>&1 | tee /tmp/batch.log' Enter

# Monitor progress
tail -20 /tmp/batch.log
```

## How it works

### Audio extraction (two methods)

1. **Streaming** (preferred, ~7x faster): ffmpeg reads video directly from Drive via HTTP using an OAuth bearer token, extracts only the audio track. No full video download needed.

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

**To switch accounts**: delete `.transcribe_drive_token.json` and re-run.

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
