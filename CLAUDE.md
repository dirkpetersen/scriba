# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scriba is a **Windows-only** voice-dictation utility: it streams microphone audio to AWS Transcribe over a
websocket, and types the transcribed text into whatever window currently has focus (via the Win32
`SendInput` API). It runs as a system tray icon (pystray) whose color reflects state (yellow=ready,
green=actively transcribing, red=recording disabled, orange=cloud connection timed out). Distributed both
as a standalone `scriba.exe` (PyInstaller) and as a PyPI package (`pyscriba`, console script `scriba`).

There is no cross-platform fallback: `pywin32`, the ctypes `user32` calls in `windows.py`, and the
mutex-based single-instance check all require Windows. It cannot run or be meaningfully tested on
Linux/macOS — `pywin32` doesn't even install there.

## Commands

```
pip install -r requirements.txt          # dev/runtime deps (pyaudio, pywin32, pystray, PySimpleGUI, etc.)
python scriba/scriba.py                  # run from source (see import gotcha below — must be run this way)
python scriba/scriba.py --language=de-DE # override default language (en-US)
python -m pytest tests/                  # run tests (single test: pytest tests/test_scriba.py::test_scriba_audio_settings)
pyinstaller --onefile --windowed --icon=scriba.ico scriba/scriba.py   # build scriba.exe (Windows only)
python -m build && twine upload dist/*   # build/publish the pyscriba package to PyPI
```

CI (`.github/workflows/build-and-publish.yml`, `windows-latest` runners) runs `pytest tests/` across
Python 3.9–3.13 on every push/PR to `main`/`deploy`. Pushing to `deploy` or pushing a `v*` tag additionally
builds `scriba.exe` and attaches it to a GitHub Release, and publishes to PyPI. As of this writing GitHub
Actions shows **0 total runs** for this repo (`gh api repos/.../actions/runs` → `total_count: 0`), so
nothing in this workflow — tests, the exe build, or the PyPI publish step — has actually been verified to
pass in CI.

## Architecture

- **`scriba/scriba.py`** — the `Scriba` class and `main()`. Owns the asyncio event loop; runs two
  concurrent tasks per websocket connection: `record_and_stream` (reads PyAudio chunks, gates them on
  voice-activity/silence detection, sends AWS Transcribe `AudioEvent`s only while "in a billable minute")
  and `receive_transcription` (reads transcript events back, calls `process_transcript`).
  `process_transcript` handles the actual text-shaping logic: strips filler words (um, uh, hm, oh, ah, er,
  well) via regex, turns the spoken commands "period"/"and period" (German: "Punkt"/"und Punkt") into a
  literal `.` and "comma"/"and comma" (German: "Komma"/"und Komma") into `,`, manages sentence-start
  lower/upper-casing around those stops, and ASCII-folds German umlauts (ä→ae, ß→ss, ...) when in `de-DE`
  mode. Connection handling has its own reconnect/backoff state machine (exponential backoff on timeouts,
  capped retries, mutex-guarded single-instance startup via `win32event`/`winerror`).
- **`scriba/presigned_url.py`** — hand-rolled AWS SigV4 presigned-URL signer for the
  `wss://transcribestreaming.<region>.amazonaws.com:8443/stream-transcription-websocket` endpoint. No
  boto3 dependency; builds the canonical request/signature manually.
- **`scriba/eventstream.py`** — manual encode/decode of the AWS event-stream binary framing (prelude +
  CRC32-checked headers/payload) that the Transcribe streaming websocket protocol uses for both outbound
  audio events and inbound transcript events.
- **`scriba/windows.py`** — ctypes wrapper around `user32.SendInput` that types text into the foreground
  window character-by-character (`VkKeyScan` + shift-state handling), independent of `keyboard`/pyautogui.
- **`scriba/gui.py`** — the pystray tray icon: colored-circle icon generation, right-click menu
  (toggle recording / switch English↔German / exit), Windows notifications.
- **`scriba/aws_credentials_form.py`** — PySimpleGUI popup shown only when no AWS credentials are found
  (checked in order: `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`/`AWS_REGION`(or
  `AWS_DEFAULT_REGION`) env vars, then `~/.aws/credentials` `[default]` profile); credentials entered here
  are written back to `~/.aws/credentials` and `~/.aws/config`.

### Import-layout gotcha

`scriba/scriba.py` imports its sibling modules with bare, non-package names:
`from presigned_url import ...`, `from eventstream import ...`, `from gui import GUI`,
`from windows import send_keystrokes_win32` — **not** `from .presigned_url import ...` or
`from scriba.presigned_url import ...`. This only resolves when the `scriba/` directory itself is on
`sys.path`, which happens automatically when the script is launched directly
(`python scriba/scriba.py` — Python puts the launched script's own directory at `sys.path[0]`).

It breaks with `ModuleNotFoundError: No module named 'presigned_url'` whenever `scriba/scriba.py` is
imported as part of the `scriba` package instead — e.g. `python -m scriba.scriba`,
`from scriba.scriba import Scriba` (exactly what `tests/test_scriba.py` does), or the installed
console-script entry point `scriba=scriba.scriba:main` declared in `setup.py`. This was confirmed directly
in this environment by stubbing out the Windows-only modules and importing `scriba.scriba` with only the
repo root on `sys.path`. There is also no `__init__.py` in `scriba/` (it's an implicit namespace package).
Given CI has never actually run (see above), this inconsistency has never been caught. Keep this in mind
before "cleaning up" imports in one of these files — check both the direct-script and package-import call
paths still work.
