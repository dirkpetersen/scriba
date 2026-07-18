# Scriba v2 — Implementation Plan

Companion to [DESIGN.md](DESIGN.md) — read that first; this file only sequences
the work. Milestones are strictly ordered; each ends in something the user can
actually run. Don't start a milestone before the previous one's acceptance
checklist passes on the target machine (Windows 11, RTX 3050 4 GB).

---

## M0 — Skeleton that runs

Scaffolding only; no audio, no model.

- [x] `pyproject.toml` (uv-managed, Python 3.12), package layout per DESIGN §8,
      console entry point `scriba = scriba.app:main`
- [x] `config.py`: load/create `%APPDATA%\Scriba\config.toml` with the DESIGN
      §7.9 schema and defaults; validation with clear errors
- [x] Logging setup (rotating file + console), `%LOCALAPPDATA%\Scriba\logs\`
- [x] Single-instance named mutex (carry over the v1 trick)
- [x] Qt app + tray icon with the full state-color set (states driven by the
      real pipeline now, not a dummy timer — M1 landed alongside M0 in this
      pass), context menu skeleton, Quit works cleanly
- [x] `pytest` + `ruff` wired up; config round-trip test

**Accept:** verified on the target machine — `uv run scriba` constructs the
full app (config load, logging, mutex, tray, all pipeline modules) without
error and shows the tray; a second `SingleInstance` correctly reports
already-running; the log file appears at
`%LOCALAPPDATA%\Scriba\logs\scriba.log`. `uv run pytest` (129 passed) and
`uv run ruff check` are clean.

## M1 — Push-to-talk dictation, end to end

The core value: hold a key, speak, text appears in the focused window.

- [x] Audio capture: enumerate devices, one 16 kHz/int16/512-sample stream per
      enabled device, pre-roll ring buffers, frame queue (DESIGN §7.1)
- [x] Silero VAD + segmentation state machine (§7.2); toggle mode wires the
      `enabled` gate to whether new utterances may start
- [x] Mic arbiter: per-device VAD, winner sticky per utterance (§7.2)
- [x] Global hotkeys incl. key-up for PTT (§7.8)
- [x] `SttBackend` protocol + `whisper_local.py`: large-v3-turbo,
      int8_float16, CUDA; warmup at load; runtime model download with tray
      PROVISIONING progress (§7.4)
- [x] Minimal postproc: hallucination filter + casing/spacing state only
- [x] Injector: SendInput `KEYEVENTF_UNICODE` incl. surrogate pairs and Enter;
      foreground-window query; clipboard consolation on failure (§7.7)
- [x] Streaming partials (§7.4a): re-decode loop with LocalAgreement-2,
      eager/stable policies, injector revision protocol (backspace + retype),
      abandon-on-focus-change, auto-off on CPU fallback
- [x] Model fallback ladder (§9) — all 4 rungs, with DEGRADED state
- [x] Toggle mode (VAD-armed continuous dictation)
- [x] `--diagnose` flag (§7.10 → devices, CUDA, model cache; timing benchmark
      still prints a "not yet wired" placeholder rather than fake numbers —
      see the known-limitations note below)

**Known M1 integration limitations** (acceptable for this pass, noted rather
than silently glossed over):
- Push-to-talk's key-*up* only stops new utterances from starting; an
  utterance already in flight when the key is released finishes via VAD's own
  `endpoint_silence_ms`, not an instant cut. True instant-on-release PTT needs
  a `Detector.force_endpoint()` hook that doesn't exist yet.
- `wake_word` mode is selectable in the tray menu but does nothing (no wake
  detector is wired — openWakeWord integration is M3); manually flipping
  "Enabled" while in this mode behaves like toggle mode.
- `--diagnose`'s STT benchmark line is still a placeholder (needs a loaded
  backend, which `--diagnose` deliberately doesn't provision).

**Accept (manual, on target machine) — core flow verified live:** first-run
model download showed tray progress and loaded on CUDA rung 1 (after fixing a
DLL-path issue, see DESIGN §8 deviations); the user dictated multi-sentence
mixed EN/DE text into Claude Code with streaming partials working. A
one-line detector bug (`dict.setdefault` eagerly constructing a ~190 ms ONNX
session per 16 ms audio frame) initially made dictation appear dead — found
via live probing plus four parallel code-review agents, fixed, and verified.
Still pending from the checklist: umlaut smoke test in Notepad/Terminal,
`--diagnose` latency figures (benchmark still unwired), 10-minute toggle
soak / memory-growth check, USB mic unplug/replug toast. Known accuracy
gaps are M2 scope: technical vocabulary ("Claude Code" -> "clot code") needs
the vocabulary system; `mixed` mode sometimes translates German utterances
to English when per-utterance detection picks EN — tune
`language_confidence_min` / prefer fixed `de` via the language hotkey.

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
2. **Rewrite mode** — DESIGN §14.2: fix/rewrite selected text in place via a
   pluggable LLM provider (Anthropic / Bedrock / Azure AI Foundry / local).
3. **Personal accent LoRA fine-tune** from the adaptation dataset (§14),
   trained on the Run:ai DGX H200 cluster.
4. **AWS Transcribe backend** with custom vocabulary + hard cost guards (§14).
5. **Client/server split**; Linux/macOS edge clients (§14).

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
