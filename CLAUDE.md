# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state: approved design, no code yet

Scriba v2 is a ground-up rewrite of a Windows voice-dictation tool. The
architecture is fully specified — **read `docs/DESIGN.md` before writing any
code**, and follow the milestone order in `docs/PLAN.md` (M0 → M4; each has an
acceptance checklist). The design doc is the contract: if implementation
reality forces a deviation, update DESIGN.md in the same commit.

The old AWS-based implementation lives on the `legacy` branch (with its own
CLAUDE.md describing it). It is reference material only — its post-processing
ideas (spoken commands, casing) and tray UX carry forward; its code does not.

## What is being built

One Windows-native Python process: microphone(s) → Silero VAD + mic arbiter →
faster-whisper on the local GPU → text post-processing (vocabulary correction,
spoken commands, casing) → Unicode `SendInput` into the focused window.
PySide6 tray + settings UI. Modes: push-to-talk, toggle, wake word (car mode).

## Locked decisions — do not re-litigate without asking the user

- Native **Windows**, single process; WSL is *not* part of the runtime
- **Python 3.12 + uv**; threads + queues, **no asyncio**
- **faster-whisper**, model `large-v3-turbo`, `int8_float16`, CUDA; fallback
  ladder to `small`/CPU (DESIGN §9). Distil models are English-only — the user
  needs German, so they're opt-in only
- **Silero VAD**, **openWakeWord**, **PySide6**, **rapidfuzz**
- CUDA runtime deps via pip wheels (`nvidia-cudnn-cu12`, `nvidia-cublas-cu12`),
  never system installs
- Injection via `SendInput` + `KEYEVENTF_UNICODE` (layout-independent), not
  `VkKeyScan` (that was a v1 bug)
- No cloud STT in v1; `SttBackend` protocol keeps AWS as a future backend

## Target machine & user profile

- Windows 11; NVIDIA RTX 3050 Laptop, **4 GB VRAM, 25 W** — assume ~2 GB VRAM
  available (GPU otherwise idle); driver CUDA 12.2
- Multiple microphones may be enabled at once — all are monitored, an arbiter
  picks the winner per utterance (DESIGN §7.2)
- The user dictates **English and German**, speaks English with a German
  accent, sometimes mixes languages mid-sentence; heavy IT vocabulary; primary
  targets are terminals and Claude Code; also dictates hands-free while
  driving (wake-word mode)
- Models download at runtime (first run) with progress shown in the tray —
  never bundle model weights
- For training jobs (future accent LoRA fine-tune, custom wake-word model)
  the user has CLI access to a local NVIDIA DGX H200 cluster with Run:ai —
  inference always stays on the laptop (DESIGN §14)

## Conventions

- Proper package with relative imports (v1's bare sibling imports broke
  package/test imports — don't repeat that)
- The `scriba/text/` pipeline is **pure functions**, and every behavior change
  lands with golden unit tests in the same commit (`tests/test_commands.py`
  etc.); this is where most product behavior lives
- Platform-specific code stays isolated in `scriba/inject/` and the edges of
  `scriba/ui/` — everything else must remain OS-neutral (future Linux/macOS)
- GPU-dependent tests are marked `@pytest.mark.gpu` and excluded by default;
  plain `pytest` must pass with no hardware
- Run `scriba --diagnose` after touching capture/VAD/STT code — latency
  regressions against the DESIGN §6 budget are bugs

## Commands

No build yet (design phase). Once M0 lands, this section should say:
`uv sync` · `uv run scriba` · `uv run pytest` · `uv run ruff check` — keep it
updated as the milestones progress.
