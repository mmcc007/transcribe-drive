#!/bin/bash
# Auto-retry wrapper for transcribe_drive batch
# Runs the batch, checks results, notifies via ntfy
# Smart backoff: if all files fail with quota errors, skips next run (12h wait)

set -euo pipefail

LOGDIR="/tmp/transcribe_retry"
mkdir -p "$LOGDIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOGFILE="$LOGDIR/batch_${TIMESTAMP}.log"
LOCKFILE="$LOGDIR/batch.lock"
BACKOFF_FILE="$LOGDIR/backoff_until"

SOURCE_FOLDER="${SOURCE_FOLDER:?Set SOURCE_FOLDER env var to Drive folder ID}"
OUTPUT_FOLDER="${OUTPUT_FOLDER:?Set OUTPUT_FOLDER env var to Drive output folder ID}"
BUDGET="${BUDGET:-40}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
NTFY_EMAIL="${NTFY_EMAIL:-}"

notify() {
    [ -z "$NTFY_TOPIC" ] && return 0
    local title="$1"
    local msg="$2"
    local headers=(-H "Title: $title")
    [ -n "$NTFY_EMAIL" ] && headers+=(-H "Email: $NTFY_EMAIL")
    curl -s -o /dev/null "${headers[@]}" \
        -d "$msg" \
        "https://ntfy.sh/$NTFY_TOPIC" || true
}

# Check backoff — skip if we're in a cooldown period
if [ -f "$BACKOFF_FILE" ]; then
    backoff_until=$(cat "$BACKOFF_FILE")
    now=$(date +%s)
    if [ "$now" -lt "$backoff_until" ]; then
        remaining=$(( (backoff_until - now) / 3600 ))
        echo "$(date): Backing off — ${remaining}h remaining until next attempt"
        exit 0
    fi
    rm -f "$BACKOFF_FILE"
    echo "$(date): Backoff period ended, resuming"
fi

# Prevent concurrent runs
if [ -f "$LOCKFILE" ]; then
    pid=$(cat "$LOCKFILE")
    if kill -0 "$pid" 2>/dev/null; then
        echo "Batch already running (PID $pid), skipping"
        exit 0
    fi
    rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

echo "=== Retry attempt at $(date) ===" | tee "$LOGFILE"

# Run the batch
cd ~/bin
source .transcribe_drive_venv/bin/activate
source transcribe.env

python3 -u transcribe_drive batch "$SOURCE_FOLDER" \
    --output-folder "$OUTPUT_FOLDER" \
    --budget "$BUDGET" \
    2>&1 | tee -a "$LOGFILE"

# Parse results
successes=$(grep -c "Transcript uploaded to Drive" "$LOGFILE" 2>/dev/null || echo 0)
failures=$(grep -c "^  ERROR processing" "$LOGFILE" 2>/dev/null || echo 0)
quota_errors=$(grep -c "downloadQuotaExceeded\|userRateLimitExceeded" "$LOGFILE" 2>/dev/null || echo 0)

echo ""
echo "=== Retry summary ==="
echo "  Successes: $successes"
echo "  Failures: $failures"
echo "  Quota errors: $quota_errors"

if [ "$successes" -gt 0 ]; then
    # Progress made — clear any backoff and notify
    rm -f "$BACKOFF_FILE"
    notify "Transcribe: $successes files done!" \
        "Processed $successes new files. $failures failures remaining."
fi

if [ "$failures" -eq 0 ] && [ "$successes" -eq 0 ]; then
    notify "Transcribe: all files complete" \
        "No pending files remaining. Batch is done!"
    # Disable cron since we're done
    crontab -l 2>/dev/null | grep -v transcribe_retry | crontab -
    echo "All files complete — cron removed."
fi

if [ "$failures" -gt 0 ] && [ "$successes" -eq 0 ]; then
    # No progress at all — back off 12 hours
    backoff_until=$(date -d "+12 hours" +%s)
    echo "$backoff_until" > "$BACKOFF_FILE"
    echo "No files succeeded. Backing off 12 hours (until $(date -d "+12 hours"))."
    notify "Transcribe: all files blocked" \
        "0/$failures files succeeded (quota errors: $quota_errors). Backing off 12h, next attempt ~$(date -d '+12 hours' '+%H:%M %Z')."
fi

# Cleanup old logs (keep last 10)
ls -t "$LOGDIR"/batch_*.log 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
