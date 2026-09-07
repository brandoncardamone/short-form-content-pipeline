#!/usr/bin/env bash
# One-shot bootstrap for a fresh Ubuntu VM (built/tested against Oracle
# Cloud's Always Free Ampere A1 shape — ARM64, 4 OCPU / 24GB RAM).
#
# Run this ONCE, as the normal (non-root) user, right after SSH-ing into a
# brand-new instance. It clones the repo, sets up the Python env, and installs
# Playwright's browser + system deps. It does NOT copy secrets, the database,
# or voice/background assets — those aren't in git (see .gitignore) and must
# be transferred separately; see the "Migrating to a cloud VM" section in
# HANDOFF.md for the exact scp/rsync commands.
#
# Known risk, not fully verified: this VM is ARM64, not the x86_64 this
# pipeline has been developed/tested on. torch and chatterbox-tts's
# dependency chain are expected to have aarch64 Linux wheels available on
# PyPI, but if `pip install -r requirements.txt` fails trying to build
# something from source, that's the first thing to suspect — the fallback is
# an x86 VM elsewhere (Oracle's free x86 shapes are far too small at 1GB RAM;
# a small paid x86 VM, or revisiting the GitHub Actions path, would be next).

set -euo pipefail

REPO_URL="https://github.com/brandoncardamone/short-form-content-pipeline"
REPO_DIR="$HOME/short-form-content-pipeline"

echo "== System packages =="
sudo apt-get update
sudo apt-get install -y \
    python3 python3-venv python3-pip \
    ffmpeg git build-essential

echo "== Cloning repo =="
if [ -d "$REPO_DIR/.git" ]; then
    echo "Repo already present at $REPO_DIR — pulling latest instead of cloning."
    git -C "$REPO_DIR" pull
else
    git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"

echo "== Python venv + dependencies =="
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "== Playwright browser + OS deps =="
.venv/bin/playwright install --with-deps chromium

echo "== Directory scaffolding (contents transferred separately, not by this script) =="
mkdir -p data output logs \
    assets/backgrounds/normalized assets/voices assets/music

echo
echo "Bootstrap done. Still needed before this VM can actually run the pipeline:"
echo "  1. Copy .env (secrets) from your old machine — see HANDOFF.md"
echo "  2. Copy data/state.db (video queue/history) — same source, do NOT start fresh"
echo "  3. Copy assets/voices/*.wav (Chatterbox reference clips — irreplaceable)"
echo "  4. Copy assets/backgrounds/normalized/*.mp4 and assets/music/* if present"
echo "  5. Set output.mobile_sync_dir to null in config.yaml (no OneDrive mount here)"
echo "  6. Install the crontab — see scripts/pipeline.cron"
