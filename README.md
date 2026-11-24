# librettist

*The quiet scribe for your meetings — 100% local, offline, free AI meeting notes for macOS.*

Record live meetings (Zoom, Google Meet, Microsoft Teams, or in-person) directly from your terminal—**no meeting bots, no subscriptions, no BlackHole virtual audio driver, and no cloud uploads**.

- **Botless system + mic audio capture:** Native macOS `ScreenCaptureKit` Swift binary captures both your voice and remote participants.
- **Local transcription:** Apple Silicon–accelerated [`whisper.cpp`](https://github.com/ggml-org/whisper.cpp) (`ggml-small.en.bin`).
- **Local AI summaries:** [`Ollama`](https://ollama.com) (`qwen2.5:7b` by default) turns transcripts into structured Markdown notes with TL;DR, Key Discussion Points, Decisions, Action Items (`- [ ]`), and a collapsible timestamped transcript.
- **Plain Markdown output:** Saved to `~/Documents/MeetingNotes` (works great with Obsidian or any Markdown editor).

---

## Requirements
- **macOS 15.0+** (Apple Silicon recommended; 16 GB RAM recommended for `qwen2.5:7b`, or use a smaller Ollama model like `llama3.2:3b` on 8 GB Macs)
- [Homebrew](https://brew.sh)
- Xcode Command Line Tools (`xcode-select --install`)

---

## One-Command Install

```bash
git clone https://github.com/Mamie/librettist.git
cd librettist
./install.sh
```

The installer automatically:
1. Installs `ffmpeg`, `whisper-cpp`, and `ollama` via Homebrew (skipping anything already installed)
2. Compiles the native macOS audio recorder (`bin/meeting-recorder`)
3. Downloads the local Whisper model (`ggml-small.en.bin`, ~465 MB)
4. Starts Ollama and pulls `qwen2.5:7b` (~4.7 GB; override with `LIBRETTIST_OLLAMA_MODEL=llama3.2:3b ./install.sh` on 8 GB Macs)
5. Adds `librettist` to your `PATH`

### One-Time macOS Permissions
Run this once in your terminal (Terminal.app or iTerm2) to trigger the macOS permission prompts:

```bash
./bin/meeting-recorder --check-permissions
```

Grant your terminal app access under **System Settings → Privacy & Security**:
- **Screen & System Audio Recording** *(captures remote speakers; macOS may ask you to restart your terminal once)*
- **Microphone** *(captures your voice)*

---

## Usage

### Option 1: Single-Command Mode (Easiest)
Start recording with a live timer, then press **Enter** or **Ctrl+C** when the meeting ends:

```bash
librettist "Weekly Sync"
# Or just run `librettist` and type a title when prompted
librettist
```

### Option 2: Background Start / Stop
Keep your terminal free while recording:

```bash
librettist start "Design Review"
# ... have your meeting ...
librettist stop
```

You can also attach quick manual notes when stopping so the AI weaves them into the summary:
```bash
librettist stop --notes "Follow up with Alex on launch timeline"
```

### Other Handy Commands
```bash
# Check setup and whether a recording is currently active
librettist status

# Transcribe & summarize any existing audio file (.m4a, .mp3, .wav) or transcript (.txt)
librettist summarize ~/Desktop/call.m4a --title "Customer Interview"

# View your config file path & current settings
librettist config
```

---

## Configuration
Settings live in `~/.config/librettist/config.json` (run `librettist config` to inspect):

| Setting | Default | Description |
| --- | --- | --- |
| `notes_dir` | `~/Documents/MeetingNotes` | Output folder (point to your Obsidian vault if you use Obsidian) |
| `ollama_model` | `qwen2.5:7b` | Any local Ollama model (e.g. `llama3.2:3b`, `gemma3:4b`) |
| `whisper_model` | `~/.local/share/librettist/models/ggml-small.en.bin` | Local GGML Whisper model |
| `whisper_language` | `en` | Language code (or `auto` with a multilingual model) |
| `keep_audio` | `false` | Save a compressed `.m4a` recording next to the Markdown note |
| `auto_open_note` | `true` | Automatically open the finished Markdown file |

---

## Privacy & Etiquette
Everything runs locally on your Mac—audio is processed on-device and temporary WAV files are deleted automatically once your summary is written. Always make sure meeting participants know when a call is being recorded or transcribed.
