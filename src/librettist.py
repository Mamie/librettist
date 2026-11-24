#!/usr/bin/env python3
"""
librettist — 100% local, offline, free AI meeting recorder & summarizer for macOS.

Captures both system audio (Zoom/Meet/Teams) and microphone natively via macOS
ScreenCaptureKit, transcribes locally with whisper.cpp, and summarizes locally
with Ollama into clean Markdown notes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parent.parent
RECORDER_BIN = REPO_DIR / "bin" / "meeting-recorder"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "librettist"
CONFIG_PATH = CONFIG_DIR / "config.json"

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "librettist"
MODELS_DIR = DATA_DIR / "models"
RECORDINGS_DIR = DATA_DIR / "recordings"
STATE_FILE = DATA_DIR / "current_recording.json"
LOG_FILE = DATA_DIR / "librettist.log"

DEFAULT_CONFIG: dict[str, Any] = {
    "notes_dir": "~/Documents/MeetingNotes",
    "whisper_model": str(MODELS_DIR / "ggml-small.en.bin"),
    "whisper_language": "en",
    "ollama_model": "qwen2.5:7b",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_num_ctx": 16384,
    "keep_audio": False,
    "auto_open_note": True,
    "capture_system_audio": True,
    "capture_microphone": True,
}


def ensure_env_path() -> None:
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    current = os.environ.get("PATH", "").split(":")
    merged = []
    for p in extra + current:
        if p and p not in merged:
            merged.append(p)
    os.environ["PATH"] = ":".join(merged)


def log(msg: str, quiet: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if not quiet:
        print(msg, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(title: str, message: str) -> None:
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        pass


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(user_cfg)
        except Exception as exc:
            log(f"Warning: failed to parse {CONFIG_PATH}: {exc}")
    return cfg


def ensure_config_file() -> Path:
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    return CONFIG_PATH


def resolve_path(raw: str) -> Path:
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = DATA_DIR / p
    return p


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "meeting").strip().lower()).strip("-")
    return cleaned[:60] or "meeting"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hrs, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def start_recording_process(title: str, cfg: dict[str, Any], no_mic: bool = False, no_system: bool = False) -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            existing = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if pid_alive(int(existing.get("pid", 0))):
                raise RuntimeError(
                    f"Already recording '{existing.get('title')}' (pid={existing['pid']}). "
                    "Run `librettist stop` first."
                )
        except json.JSONDecodeError:
            pass
        STATE_FILE.unlink(missing_ok=True)

    if not RECORDER_BIN.exists():
        raise RuntimeError(f"Recorder binary missing at {RECORDER_BIN}. Re-run ./install.sh.")

    now = dt.datetime.now()
    rec_id = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{slugify(title)}"
    rec_dir = RECORDINGS_DIR / rec_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    rec_log = rec_dir / "recorder.log"

    cmd = [str(RECORDER_BIN), "--output-dir", str(rec_dir)]
    if no_mic or not cfg.get("capture_microphone", True):
        cmd.append("--no-mic")
    if no_system or not cfg.get("capture_system_audio", True):
        cmd.append("--no-system")

    with rec_log.open("wb") as log_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    time.sleep(1.5)
    if proc.poll() is not None:
        err_text = rec_log.read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(
            "Recorder failed to start. Make sure your terminal has "
            "'Screen & System Audio Recording' and 'Microphone' permissions in "
            f"System Settings -> Privacy & Security.\n\nDetails:\n{err_text}"
        )

    state = {
        "pid": proc.pid,
        "rec_id": rec_id,
        "rec_dir": str(rec_dir),
        "title": title,
        "started_epoch": time.time(),
        "started_iso": now.isoformat(),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def stop_recorder_process(pid: int) -> None:
    if not pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def mix_audio_to_16k_wav(rec_dir: Path) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install via: brew install ffmpeg")

    mic_wav = rec_dir / "mic.wav"
    sys_wav = rec_dir / "system.wav"
    has_mic = mic_wav.exists() and mic_wav.stat().st_size > 1024
    has_sys = sys_wav.exists() and sys_wav.stat().st_size > 1024

    if not has_mic and not has_sys:
        return None

    combined = rec_dir / "combined_16k.wav"
    if has_mic and has_sys:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(mic_wav),
            "-i",
            str(sys_wav),
            "-filter_complex",
            "[0:a]aresample=16000,aformat=channel_layouts=mono,dynaudnorm[a0];"
            "[1:a]aresample=16000,aformat=channel_layouts=mono,dynaudnorm[a1];"
            "[a0][a1]amix=inputs=2:duration=longest:normalize=0[out]",
            "-map",
            "[out]",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(combined),
        ]
    else:
        src = mic_wav if has_mic else sys_wav
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(combined),
        ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr[-500:]}")
    return combined


def convert_any_audio_to_16k_wav(audio_path: Path, out_wav: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install via: brew install ffmpeg")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(audio_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr[-500:]}")
    return out_wav


def transcribe_wav(wav_path: Path, cfg: dict[str, Any]) -> str:
    whisper_cli = shutil.which("whisper-cli")
    if not whisper_cli:
        raise RuntimeError("whisper-cli not found. Install via: brew install whisper-cpp")

    model_path = resolve_path(cfg["whisper_model"])
    if not model_path.exists():
        raise RuntimeError(f"Whisper model not found at {model_path}. Re-run ./install.sh.")

    lang = cfg.get("whisper_language", "en")
    cmd = [whisper_cli, "-m", str(model_path), "-f", str(wav_path), "-l", str(lang)]
    log(f"Transcribing audio locally with Whisper ({model_path.name})...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        log("GPU transcription unavailable; retrying on CPU...", quiet=True)
        res = subprocess.run(cmd + ["--no-gpu"], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"whisper-cli failed: {res.stderr[-500:]}")

    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    return "\n".join(lines)


def ollama_is_up(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_ollama_running(base_url: str) -> None:
    if ollama_is_up(base_url):
        return
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        raise RuntimeError("ollama not found. Install via: brew install ollama")

    log("Starting local Ollama server...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "ollama.log").open("ab") as fh:
        subprocess.Popen(
            [ollama_bin, "serve"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    deadline = time.time() + 20.0
    while time.time() < deadline:
        if ollama_is_up(base_url):
            return
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for local Ollama server to start.")


def call_ollama(prompt: str, system_prompt: str, cfg: dict[str, Any]) -> str:
    base_url = cfg.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
    ensure_ollama_running(base_url)

    payload = {
        "model": cfg["ollama_model"],
        "system": system_prompt,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": int(cfg.get("ollama_num_ctx", 16384)),
            "temperature": 0.2,
        },
    }
    req = urllib.request.Request(
        f"{base_url}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return str(body.get("response", "")).strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc


def summarize_transcript(transcript: str, title: str, manual_notes: str, cfg: dict[str, Any]) -> str:
    system_prompt = (
        "You are an expert executive assistant that turns raw meeting transcripts into crisp, "
        "accurate, actionable Markdown meeting notes. Never invent facts, owners, or decisions "
        "not grounded in the transcript or user notes. If a section has nothing relevant, write 'None noted.'"
    )

    max_chars = 32000
    working_transcript = transcript
    if len(transcript) > max_chars:
        log("Long transcript detected; summarizing in chunks...")
        chunks: list[str] = []
        buf: list[str] = []
        buf_len = 0
        for ln in transcript.splitlines():
            if buf_len + len(ln) + 1 > max_chars and buf:
                chunks.append("\n".join(buf))
                buf = [ln]
                buf_len = len(ln)
            else:
                buf.append(ln)
                buf_len += len(ln) + 1
        if buf:
            chunks.append("\n".join(buf))

        partial = []
        for idx, chunk in enumerate(chunks, start=1):
            chunk_prompt = (
                f"Meeting title: {title}\n"
                f"Part {idx} of {len(chunks)}. Extract key discussion points, decisions, "
                "action items (with owners/deadlines if stated), and open questions as concise bullets.\n\n"
                f"{chunk}"
            )
            partial.append(f"### Part {idx}\n" + call_ollama(chunk_prompt, system_prompt, cfg))
        working_transcript = "\n\n".join(partial)

    notes_section = f"\nAdditional manual notes:\n{manual_notes}\n" if manual_notes else ""
    final_prompt = f"""Meeting title: {title}
{notes_section}
Transcript:
{working_transcript}

Write structured Markdown notes using these exact headings:
## TL;DR
(2-3 concise sentences summarizing the purpose and main outcome)

## Key Discussion Points
- Bullet points grouped logically by topic

## Decisions Made
- Clear list of concrete decisions

## Action Items
- [ ] Task description (Owner, if mentioned — Deadline, if mentioned)

## Open Questions & Follow-ups
- Unresolved questions or topics for next time
"""
    log(f"Summarizing locally with Ollama ({cfg['ollama_model']})...")
    return call_ollama(final_prompt, system_prompt, cfg)


def write_markdown_note(
    *,
    notes_dir: Path,
    title: str,
    start_dt: dt.datetime,
    duration_min: float | None,
    summary_md: str,
    manual_notes: str,
    transcript: str,
    audio_saved_path: Path | None,
) -> Path:
    notes_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{start_dt.strftime('%Y-%m-%d_%H-%M')}_{slugify(title)}.md"
    out_path = notes_dir / filename

    safe_title = title.replace('"', "'")
    dur_str = f"{duration_min:.1f}" if duration_min is not None else "unknown"
    header = [
        "---",
        f'title: "{safe_title}"',
        f"date: {start_dt.strftime('%Y-%m-%d %H:%M')}",
        f"duration_minutes: {dur_str}",
        "tags:",
        "  - meeting-notes",
        "---",
        "",
        f"# {title}",
        "",
        f"**Date:** {start_dt.strftime('%B %d, %Y at %I:%M %p')}  ",
        f"**Duration:** {dur_str} min",
    ]
    if audio_saved_path:
        header.append(f"**Audio:** `{audio_saved_path.name}`")

    parts = ["\n".join(header), "", summary_md.strip()]

    if manual_notes:
        parts.extend(["", "---", "", "## Manual Notes", "", manual_notes.strip()])

    parts.extend(
        [
            "",
            "---",
            "",
            "<details>",
            "<summary>Full Transcript</summary>",
            "",
            "```text",
            transcript.strip() or "(No speech detected)",
            "```",
            "",
            "</details>",
            "",
        ]
    )

    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path


def finalize_recording(state: dict[str, Any], manual_notes: str, cfg: dict[str, Any], output_dir_override: str | None = None) -> int:
    rec_dir = Path(state["rec_dir"])
    title = state.get("title", "Untitled Meeting")
    started_epoch = state.get("started_epoch")
    ended_epoch = time.time()
    duration_min = max(0.0, (ended_epoch - float(started_epoch)) / 60.0) if started_epoch else None
    start_dt = dt.datetime.fromtimestamp(float(started_epoch)) if started_epoch else dt.datetime.now()

    combined_wav = mix_audio_to_16k_wav(rec_dir)
    if not combined_wav:
        log("No audio was captured. Check microphone / Screen & System Audio Recording permissions.")
        return 1

    transcript = transcribe_wav(combined_wav, cfg)
    if not transcript.strip():
        summary_md = "## TL;DR\nNo speech was detected in the recording."
    else:
        summary_md = summarize_transcript(transcript, title, manual_notes, cfg)

    notes_dir = resolve_path(output_dir_override) if output_dir_override else resolve_path(cfg["notes_dir"])
    audio_saved_path = None
    if cfg.get("keep_audio", False):
        audio_saved_path = notes_dir / f"{start_dt.strftime('%Y-%m-%d_%H-%M')}_{slugify(title)}.m4a"
        notes_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(combined_wav), "-c:a", "aac", "-b:a", "96k", str(audio_saved_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    out_path = write_markdown_note(
        notes_dir=notes_dir,
        title=title,
        start_dt=start_dt,
        duration_min=duration_min,
        summary_md=summary_md,
        manual_notes=manual_notes,
        transcript=transcript,
        audio_saved_path=audio_saved_path,
    )

    shutil.rmtree(rec_dir, ignore_errors=True)
    log(f"Saved meeting notes: {out_path}")
    notify("Meeting Notes Ready", out_path.name)

    if cfg.get("auto_open_note", True) and not output_dir_override:
        subprocess.run(["/usr/bin/open", str(out_path)], check=False)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    cfg = load_config()
    title = " ".join(args.title).strip() if args.title else ""
    if not title and sys.stdin.isatty():
        try:
            title = input("Meeting title (Enter for 'Untitled Meeting'): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
    title = title or "Untitled Meeting"

    try:
        state = start_recording_process(title, cfg, no_mic=args.no_mic, no_system=args.no_system)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f'\nRecording "{title}" (mic + system audio).')
    print("Press Enter or Ctrl+C when the meeting ends to transcribe & summarize.\n")

    start_ts = float(state["started_epoch"])
    try:
        while True:
            if not pid_alive(int(state["pid"])):
                print("\nRecorder stopped unexpectedly.")
                break
            elapsed = format_elapsed(time.time() - start_ts)
            print(f"\r  [REC {elapsed}] Press Enter or Ctrl+C to stop... ", end="", flush=True)
            if sys.stdin.isatty():
                ready, _, _ = select.select([sys.stdin], [], [], 0.5)
                if ready:
                    sys.stdin.readline()
                    break
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    print("\n\nStopping recording...")
    STATE_FILE.unlink(missing_ok=True)
    stop_recorder_process(int(state["pid"]))

    manual_notes = args.notes or ""
    if not manual_notes and sys.stdin.isatty():
        try:
            extra = input("Optional manual notes to include (or press Enter to skip): ").strip()
            manual_notes = extra
        except (EOFError, KeyboardInterrupt):
            print()

    try:
        return finalize_recording(state, manual_notes, cfg, output_dir_override=args.output_dir)
    except Exception as exc:
        log(f"Error: {exc}")
        return 1


def cmd_start(args: argparse.Namespace) -> int:
    cfg = load_config()
    title = " ".join(args.title).strip() if args.title else "Untitled Meeting"
    try:
        state = start_recording_process(title, cfg, no_mic=args.no_mic, no_system=args.no_system)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    log(f'Recording started: "{state["title"]}" (pid={state["pid"]})')
    print("Run `librettist stop` when your meeting ends.")
    notify("librettist", f'Recording: {state["title"]}')
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not STATE_FILE.exists():
        print("No active recording found.")
        return 0

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to read recording state: {exc}", file=sys.stderr)
        STATE_FILE.unlink(missing_ok=True)
        return 1

    STATE_FILE.unlink(missing_ok=True)
    if args.title:
        state["title"] = " ".join(args.title).strip() or state["title"]

    log(f'Stopping recording "{state["title"]}"...')
    stop_recorder_process(int(state.get("pid", 0)))

    try:
        return finalize_recording(state, args.notes or "", cfg, output_dir_override=args.output_dir)
    except Exception as exc:
        log(f"Error: {exc}")
        notify("librettist — Error", str(exc)[:120])
        return 1


def cmd_summarize(args: argparse.Namespace) -> int:
    cfg = load_config()
    src = Path(args.input_file).expanduser().resolve()
    if not src.exists():
        print(f"Input file not found: {src}", file=sys.stderr)
        return 1

    title = args.title or src.stem.replace("_", " ").replace("-", " ").title()
    notes_dir = resolve_path(args.output_dir) if args.output_dir else resolve_path(cfg["notes_dir"])

    if src.suffix.lower() in {".txt", ".md"}:
        transcript = src.read_text(encoding="utf-8")
    else:
        tmp_dir = DATA_DIR / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        wav = convert_any_audio_to_16k_wav(src, tmp_dir / "input_16k.wav")
        transcript = transcribe_wav(wav, cfg)
        wav.unlink(missing_ok=True)

    summary_md = summarize_transcript(transcript, title, args.notes or "", cfg)
    out_path = write_markdown_note(
        notes_dir=notes_dir,
        title=title,
        start_dt=dt.datetime.now(),
        duration_min=None,
        summary_md=summary_md,
        manual_notes=args.notes or "",
        transcript=transcript,
        audio_saved_path=None,
    )
    print(f"Saved summary to: {out_path}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    model_path = resolve_path(cfg["whisper_model"])
    notes_dir = resolve_path(cfg["notes_dir"])
    ollama_url = cfg.get("ollama_url", "http://127.0.0.1:11434")

    print("=== librettist status ===")
    print(f"Config file:     {CONFIG_PATH} ({'exists' if CONFIG_PATH.exists() else 'using defaults'})")
    print(f"Notes output:    {notes_dir}")
    print(f"Recorder binary: {'OK' if RECORDER_BIN.exists() else 'MISSING'} ({RECORDER_BIN})")
    print(f"ffmpeg:          {shutil.which('ffmpeg') or 'MISSING'}")
    print(f"whisper-cli:     {shutil.which('whisper-cli') or 'MISSING'}")
    print(f"Whisper model:   {'OK' if model_path.exists() else 'MISSING'} ({model_path})")
    print(f"ollama binary:   {shutil.which('ollama') or 'MISSING'}")
    print(f"Ollama server:   {'RUNNING' if ollama_is_up(ollama_url) else 'STOPPED (auto-starts when needed)'}")
    print(f"Ollama model:    {cfg['ollama_model']}")

    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        alive = pid_alive(int(state.get("pid", 0)))
        elapsed = format_elapsed(time.time() - float(state.get("started_epoch", time.time())))
        print(f"Active session:  {'RECORDING' if alive else 'STALE'} — \"{state.get('title')}\" ({elapsed}, pid={state.get('pid')})")
    else:
        print("Active session:  None")
    return 0


def cmd_config(_args: argparse.Namespace) -> int:
    path = ensure_config_file()
    print(f"Config path: {path}\n")
    print(path.read_text(encoding="utf-8"))
    return 0


def main() -> int:
    ensure_env_path()
    parser = argparse.ArgumentParser(
        prog="librettist",
        description="100% local, offline AI meeting recorder & summarizer for macOS.",
    )
    sub = parser.add_subparsers(dest="command")

    p_rec = sub.add_parser("record", aliases=["rec"], help="Record interactively until you press Enter or Ctrl+C")
    p_rec.add_argument("title", nargs="*", help="Meeting title (optional)")
    p_rec.add_argument("--notes", default=None, help="Optional manual notes to merge into summary")
    p_rec.add_argument("--output-dir", default=None, help="Override output directory")
    p_rec.add_argument("--no-mic", action="store_true", help="Capture system audio only")
    p_rec.add_argument("--no-system", action="store_true", help="Capture microphone only")
    p_rec.set_defaults(func=cmd_record)

    p_start = sub.add_parser("start", help="Start recording in the background")
    p_start.add_argument("title", nargs="*", help="Meeting title (optional)")
    p_start.add_argument("--no-mic", action="store_true", help="Capture system audio only")
    p_start.add_argument("--no-system", action="store_true", help="Capture microphone only")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="Stop background recording, transcribe, and summarize")
    p_stop.add_argument("--title", nargs="*", default=None, help="Override meeting title")
    p_stop.add_argument("--notes", default=None, help="Optional manual notes to merge into summary")
    p_stop.add_argument("--output-dir", default=None, help="Override output directory")
    p_stop.set_defaults(func=cmd_stop)

    p_sum = sub.add_parser("summarize", help="Transcribe & summarize an existing audio or text file")
    p_sum.add_argument("input_file", help="Path to audio (.m4a/.wav/.mp3) or transcript (.txt/.md)")
    p_sum.add_argument("--title", default=None, help="Meeting title")
    p_sum.add_argument("--notes", default=None, help="Optional manual notes")
    p_sum.add_argument("--output-dir", default=None, help="Override output directory")
    p_sum.set_defaults(func=cmd_summarize)

    p_stat = sub.add_parser("status", aliases=["doctor"], help="Check setup and active recording status")
    p_stat.set_defaults(func=cmd_status)

    p_cfg = sub.add_parser("config", help="Show config path and current settings")
    p_cfg.set_defaults(func=cmd_config)

    # Default to interactive `record` if invoked with no subcommand or a bare title.
    known_cmds = {"record", "rec", "start", "stop", "summarize", "status", "doctor", "config"}
    argv = sys.argv[1:]
    if not argv:
        argv = ["record"]
    elif argv[0] not in known_cmds and not argv[0].startswith("-"):
        argv = ["record"] + argv
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
