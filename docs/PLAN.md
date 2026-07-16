# Scriba v2 — Implementation Plan

Companion to [DESIGN.md](DESIGN.md) — read that first; this file only sequences
the work. Milestones are strictly ordered; each ends in something the user can
actually run. Don't start a milestone before the previous one's acceptance
checklist passes on the target machine (Windows 11, RTX 3050 4 GB).

---

## M0 — Skeleton that runs

Scaffolding only; no audio, no model.

- [ ] `pyproject.toml` (uv-managed, Python 3.12), package layout per DESIGN §8,
      console entry point `scriba = scriba.app:main`
- [ ] `config.py`: load/create `%APPDATA%\Scriba\config.toml` with the DESIGN
      §7.9 schema and defaults; validation with clear errors
- [ ] Logging setup (rotating file + console), `%LOCALAPPDATA%\Scriba\logs\`
- [ ] Single-instance named mutex (carry over the v1 trick)
- [ ] Qt app + tray icon with the full state-color set (states driven by a
      dummy timer for now), context menu skeleton, Quit works cleanly
- [ ] `pytest` + `ruff` wired up; config round-trip test

**Accept:** `uv run scriba` shows a tray icon on Windows, menu works, second
launch refuses to start, log file appears.

## M1 — Push-to-talk dictation, end to end

The core value: hold a key, speak, text appears in the focused window.

- [ ] Audio capture: enumerate devices, one 16 kHz/int16/512-sample stream per
      enabled device, pre-roll ring buffers, frame queue (DESIGN §7.1)
- [ ] Silero VAD + segmentation state machine (§7.2); PTT mode wires hotkey
      down/up to LISTENING/endpoint
- [ ] Mic arbiter: per-device VAD, winner sticky per utterance (§7.2)
- [ ] Global hotkeys incl. key-up for PTT (§7.8)
- [ ] `SttBackend` protocol + `whisper_local.py`: large-v3-turbo,
      int8_float16, CUDA; warmup at load; runtime model download with tray
      PROVISIONING progress (§7.4)
- [ ] Minimal postproc: hallucination filter + casing/spacing state only
- [ ] Injector: SendInput `KEYEVENTF_UNICODE` incl. surrogate pairs and Enter;
      foreground-window query; clipboard consolation on failure (§7.7)
- [ ] Model fallback ladder (§9) — at minimum rungs 1, 3, 4 with DEGRADED state
- [ ] Toggle mode (VAD-armed continuous dictation)
- [ ] `--diagnose` flag (§7.10 → devices, CUDA, model cache, timing benchmark)

**Accept (manual, on target machine):** first run downloads the model with
visible tray progress; dictating a 2–3 sentence paragraph into Notepad,
Windows Terminal, and Claude Code lands correctly (incl. an umlaut word typed
natively); end-of-speech → text < 1.5 s measured by `--diagnose` figures; 10
minutes of toggle-mode use shows no memory growth, no stray text while silent;
unplugging/replugging a USB mic recovers with a toast.

## M2 — Vocabulary, commands, languages

Accuracy work — this milestone is why the rewrite exists.

- [ ] `vocabulary.txt` parsing + file watching; hotwords rendering (§7.6)
- [ ] Fuzzy post-correction with rapidfuzz incl. sounds-like aliases and
      multi-word windows; correction logging
- [ ] Spoken-command table EN+DE (full §7.5 table), config-extensible
- [ ] Filler removal per language
- [ ] Language policy (§7.10): fixed en/de, `mixed` (restricted per-utterance
      detection + confidence fallback), `auto`; tray + hotkey switching
- [ ] Full unit-test suite for `text/` (golden tests: commands EN+DE,
      corrections incl. accent-artifact cases, casing sequences, blocklist)
- [ ] `initial_prompt` config support

**Accept:** "cube control apply the manifest" (spoken) yields
`kubectl apply the manifest`; period/comma/new-line commands work in both
languages; in `mixed` mode alternating EN and DE sentences transcribe in the
right language ≥ 9/10 utterances; `pytest` green.

## M3 — Wake word & car mode

- [ ] openWakeWord integration (pre-trained phrase), ARMED→LISTENING flow,
      VAD-agreement gate, refractory period (§7.3)
- [ ] Activation/deactivation sounds; sleep phrase + auto-sleep timeout
- [ ] Settings window v1 (PySide6): audio devices w/ level meters, model,
      hotkeys, wake word, vocabulary editor tabs (§7.8)
- [ ] First-run wizard (language → mics → model download → try-it page)
- [ ] Adaptation flywheel: "flag last utterance" hotkey/menu → save WAV +
      correction dialog (§7.10d, opt-in)

**Accept:** laptop on a desk across the room: wake phrase → beep → dictated
sentence lands in the focused window → auto-sleep re-arms; false-activation
rate over an hour of ambient noise/music ≈ 0; settings edits persist and apply
without restart where feasible.

**Stretch:** train custom "hey Scriba" wake-word model.

## M4 — Robustness & packaging

- [ ] Worker crash-restart policy + ERROR states end-to-end (§9)
- [ ] Per-app injection overrides + paste mode (§7.7)
- [ ] Install story: wheel + `uv tool install`, `--autostart` Run-key flag;
      evaluate PyInstaller one-dir vs. wheel-only (decision recorded in §12)
- [ ] Write user-facing README (install, hotkeys, vocabulary how-to, car mode,
      UIPI/elevated-window limitation)
- [ ] Optional: GitHub Actions for ruff + CPU unit tests

**Accept:** clean machine (or fresh user account) → install → first-run wizard
→ dictation working, without touching a terminal other than the install
command itself.

## M5+ — Future (designed, not scheduled)

In rough order of user value:

1. **Readout mode (TTS)** — DESIGN §14.1: Kokoro British female voice, MCP
   `speak` tool for Claude Code, intent keywords ("more details", "stop"),
   half-duplex mic suppression.
2. **Personal accent LoRA fine-tune** from the adaptation dataset (§14).
3. **AWS Transcribe backend** with custom vocabulary + hard cost guards (§14).
4. **Client/server split**; Linux/macOS edge clients (§14).
5. **Eager flush** for long dictation (§14).

---

## Standing instructions for implementation sessions

- The `text/` pipeline stays pure-functional and unit-tested — new behavior
  lands with its golden tests in the same commit.
- Any deviation from DESIGN.md gets a short note *in* DESIGN.md (edit the
  section, don't let the doc rot). The design doc is the contract.
- Test on the real target: dictation into Windows Terminal + Claude Code is
  the primary use case, Notepad is the smoke test.
- Latency regressions are bugs: keep `--diagnose` honest and run it after
  touching capture, VAD, or STT code.
