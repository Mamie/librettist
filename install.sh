#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/librettist"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/librettist"
MODELS_DIR="$DATA_DIR/models"
WHISPER_MODEL_NAME="ggml-small.en.bin"
WHISPER_MODEL_PATH="$MODELS_DIR/$WHISPER_MODEL_NAME"
WHISPER_MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_MODEL_NAME"
OLLAMA_MODEL="${LIBRETTIST_OLLAMA_MODEL:-qwen2.5:7b}"

echo "=== Installing librettist (100% local macOS meeting notes) ==="

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Error: librettist requires macOS (15.0+ for native mic + system audio capture)."
  exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
  echo "Error: Homebrew is required. Install it from https://brew.sh and re-run ./install.sh."
  exit 1
fi

if ! command -v swiftc >/dev/null 2>&1; then
  echo "Error: Xcode Command Line Tools are required. Run: xcode-select --install"
  exit 1
fi

echo ""
echo "[1/5] Checking Homebrew dependencies (ffmpeg, whisper-cpp, ollama)..."
MISSING_PKGS=()
command -v ffmpeg >/dev/null 2>&1 || MISSING_PKGS+=(ffmpeg)
command -v whisper-cli >/dev/null 2>&1 || MISSING_PKGS+=(whisper-cpp)
command -v ollama >/dev/null 2>&1 || MISSING_PKGS+=(ollama)

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  echo "Installing: ${MISSING_PKGS[*]}"
  brew install "${MISSING_PKGS[@]}"
else
  echo "All Homebrew dependencies already installed."
fi

echo ""
echo "[2/5] Compiling native macOS ScreenCaptureKit audio recorder..."
mkdir -p "$REPO_DIR/bin"
swiftc -O -parse-as-library \
  -framework ScreenCaptureKit \
  -framework AVFoundation \
  -framework CoreMedia \
  -framework Foundation \
  "$REPO_DIR/recorder/main.swift" \
  -o "$REPO_DIR/bin/meeting-recorder"

chmod +x "$REPO_DIR/src/librettist.py"

echo ""
echo "[3/5] Downloading local Whisper model ($WHISPER_MODEL_NAME, ~465 MB)..."
mkdir -p "$MODELS_DIR"
if [[ -f "$WHISPER_MODEL_PATH" ]]; then
  echo "Whisper model already present at $WHISPER_MODEL_PATH."
else
  curl -L --fail --progress-bar -o "$WHISPER_MODEL_PATH" "$WHISPER_MODEL_URL"
fi

echo ""
echo "[4/5] Starting Ollama and pulling local summary model ($OLLAMA_MODEL)..."
brew services start ollama >/dev/null 2>&1 || true
for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ollama list 2>/dev/null | awk '{print $1}' | grep -Fxq "$OLLAMA_MODEL"; then
  echo "Ollama model $OLLAMA_MODEL already installed."
else
  ollama pull "$OLLAMA_MODEL"
fi

echo ""
echo "[5/5] Installing 'librettist' command & default config..."
BIN_DIR="$(brew --prefix)/bin"
if [[ ! -w "$BIN_DIR" ]]; then
  BIN_DIR="$HOME/.local/bin"
  mkdir -p "$BIN_DIR"
fi

ln -sf "$REPO_DIR/src/librettist.py" "$BIN_DIR/librettist"

mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  "$REPO_DIR/src/librettist.py" config >/dev/null
fi

echo ""
echo "=== Installation complete! ==="
echo "Installed CLI: $BIN_DIR/librettist"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "Note: Add '$BIN_DIR' to your PATH (e.g. in ~/.zshrc)."
fi
echo ""
echo "One-time permission check (grants your terminal system audio + mic access):"
echo "  $REPO_DIR/bin/meeting-recorder --check-permissions"
echo ""
echo "Quick start:"
echo "  librettist \"Weekly Sync\"          # Start & press Enter/Ctrl+C to finish"
echo "  librettist start \"Weekly Sync\"    # Record in background"
echo "  librettist stop                   # Stop, transcribe, and summarize"
