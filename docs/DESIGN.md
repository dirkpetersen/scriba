# Scriba v2 — Architecture & Design

Status: **approved design, pre-implementation**. This document is the authoritative
specification for the rewrite. The legacy AWS-based implementation lives on the
`legacy` branch and is referenced here only for lessons learned.

---

## 1. Purpose

Scriba is a voice-dictation utility: the user speaks, and the transcribed text is
typed into whatever window currently has keyboard focus (editors, terminals,
Claude Code, browsers). It replaces both the legacy AWS-based Scriba and the
built-in Windows dictation (Win+H), which the user abandoned for cost and
accuracy reasons respectively.

### Goals

- **Accuracy on technical vocabulary.** The user dictates IT terms (kubectl,
  systemd, Terraform, WSL2, ...) that generic dictation consistently misspells.
  Scriba must support a user-maintained vocabulary that biases recognition and
  corrects output.
- **Zero marginal cost.** Speech-to-text runs locally on the user's GPU. No
  cloud service, no metering, no "session left open" billing surprises (the
  failure mode that killed v1).
- **Low latency.** Text should appear well under ~1.5 s after the user stops
  speaking.
- **Streaming partials, Windows-parity.** Like Win+H dictation, words must
  begin appearing *while the user is still speaking*, and already-typed words
  must self-correct when later context disambiguates them. This is a v1
  requirement — the bar is "at least as good as Windows dictation" (§7.4a).
- **Hands-free operation ("car mode").** The user dictates while driving with
  the laptop on the passenger seat: activation must work without touching the
  keyboard — wake word, plus optional hardware-button support.
- **Bilingual & accent-robust.** English (en) and German (de) dictation with a
  quick way to switch — and good handling of the user's actual speech:
  American English spoken with a German accent, plus mixed-language sentences
  ("Denglish"). See §7.10 for the language/accent strategy.
- **User-friendly.** Real settings UI, tray icon with meaningful states,
  first-run experience that downloads the model with visible progress. No
  editing config files as the primary interface (though the config file exists
  and is documented).

### Non-goals (v1 of the rewrite)

- No cloud STT backend (AWS Transcribe is a *future optional* backend — the
  interface accommodates it, see §14).
- No client/server split across machines (the internal seams allow it later,
  see §14).
- No Linux/macOS support yet (module boundaries keep platform code isolated,
  see §14).
- No meeting transcription / diarization / long-form file transcription.
- No text-to-speech in v1 — "readout mode" (agent output read aloud, British
  female voice, "more details" keyword) is a designed roadmap item, see §14.1.
- No LLM text transformation in v1 — "rewrite mode" (fix grammar/style of
  selected text in any app, in place) is a designed roadmap item, see §14.2.

---

## 2. Lessons from v1 (legacy branch)

| v1 problem | v2 answer |
|---|---|
| AWS streaming cost, opaque "billable minute" logic keeping sessions warm | Local inference; no meter exists |
| Misrecognized technical terms | Hotword biasing + vocabulary correction pass (§7.6) |
| `VkKeyScan`-based injection was keyboard-layout dependent; forced umlaut folding | `SendInput` with `KEYEVENTF_UNICODE` (§7.7); umlaut folding becomes an optional toggle, default off |
| Complex asyncio task/reconnect machinery for the websocket | Plain threads + queues; no network in the hot path (§5) |
| Bare sibling imports (`from presigned_url import ...`) broke package imports and the test suite | Proper package with relative imports from day one |
| PySimpleGUI credentials popup, pystray-only UI | PySide6 (Qt) tray + settings window (§7.8) |
| Windows-only assumptions baked in everywhere | Platform-specific code isolated to `inject/` and `ui/` seams |

Worth carrying forward from v1: the spoken-command idea ("period"/"comma", EN+DE),
filler-word stripping, sentence-casing state, tray color language, and the
named-mutex single-instance guard.

---

## 3. Decisions (locked)

These were discussed and settled with the user. Do not re-litigate without asking.

| Decision | Choice | Rationale |
|---|---|---|
| Platform for v1 | **Native Windows, single process** | Mic capture and keystroke injection must be on Windows anyway; WSL adds GPU paravirtualization overhead and an "is WSL running" failure mode. WSL remains a dev environment only. |
| Language | **Python 3.12** | Latency lives in the STT model (C++ under faster-whisper), not the runtime. Python has mature libs for every component. Clean seams allow later rewrites of individual pieces. |
| STT engine | **faster-whisper** (CTranslate2) | Best default per ecosystem consensus; int8 quantization fits the 4 GB card; `hotwords` support. |
| Model | **`large-v3-turbo`, `int8_float16`, CUDA** when `general.language != "en"`; **`distil-large-v3`** when `general.language == "en"` | Multilingual is required for German (note: **Distil-Whisper models are English-only**). **Deviation (implemented, user request):** originally `distil-large-v3` was an opt-in choice; it's now *automatically* selected whenever the language is plain English and swapped for the multilingual model on any other language/mixed/auto selection — `scriba.stt.language.model_for_language()` is the single source of truth, enforced in `ScribaApp.__init__` so `stt.model` is never independently configurable (a stale/hand-edited value is silently overwritten). Switching triggers an unload+reload with a tray toast sequence (§9). ~1.5–2 GB VRAM for the large model. Fallback ladder in §9. |
| VAD | **Silero VAD** (ONNX, CPU) | Tiny (~2 MB), accurate, 512-sample/32 ms frames align with capture blocks. Also prevents Whisper silence-hallucinations by construction. |
| Wake word | **openWakeWord** (ONNX, CPU) | Free, offline, runs continuously at negligible CPU. Pre-trained phrase first; custom "Scriba" phrase later (§7.3). |
| UI toolkit | **PySide6 (Qt)** | One framework covers tray icon, native menus, toasts, a real settings dialog, and a first-run/download progress UI. Cross-platform for free later. |
| Concurrency | **Threads + queues, no asyncio** | The pipeline is a simple linear dataflow with blocking C libraries (PortAudio, CTranslate2); threads are the natural fit and remove v1's most bug-prone area. |
| GPU/CUDA deps | **pip wheels** (`nvidia-cudnn-cu12`, `nvidia-cublas-cu12`) | Avoids the #1 faster-whisper install complaint (system cuDNN mismatches). Everything lives in the venv. |
| Cloud | **None in v1** | AWS Transcribe (+custom vocabulary +cost guards) is a future pluggable backend behind the `SttBackend` interface. |

### Target machine (the user's)

- Windows 11, WSL2 present but **not** part of the runtime.
- NVIDIA RTX 3050 Laptop GPU, **4 GB VRAM, 25 W power limit**, driver CUDA 12.2.
- GPU is otherwise idle — Scriba may assume ~2 GB VRAM is available.
- Multiple microphones may be present (laptop array, USB, Bluetooth headset)
  and **all enabled mics must be monitored** — see §7.2.

---

## 4. System overview

One Windows process, five pipeline stages connected by queues, plus the Qt main
thread for UI:

```
                    ┌────────────────────────────────────────────────────────┐
                    │                     Qt main thread                     │
                    │   tray icon · menus · toasts · settings window ·       │
                    │   first-run wizard · download progress                 │
                    └──────────────▲─────────────────────────▲───────────────┘
                                   │ state signals            │ config changes
┌──────────────┐   AudioFrame   ┌──┴───────────────┐  Utterance  ┌──────────────┐
│ capture       │──per device──▶│ detector          │────────────▶│ stt worker   │
│ one PortAudio │               │ mic arbiter       │   (audio,   │ faster-      │
│ InputStream   │               │ wake word (armed) │   metadata) │ whisper on   │
│ per enabled   │               │ Silero VAD        │             │ GPU (owns    │
│ mic           │               │ utterance         │             │ the model)   │
└──────────────┘               │ segmentation      │             └──────┬───────┘
                                └──────────────────┘                    │ Transcript
                                                                        ▼
                                                        ┌──────────────────────────┐
                                                        │ postproc (pure functions) │
                                                        │ hallucination filter ·    │
                                                        │ spoken commands ·         │
                                                        │ filler removal ·          │
                                                        │ vocabulary correction ·   │
                                                        │ casing/spacing state      │
                                                        └────────────┬─────────────┘
                                                                     │ InjectJob
                                                                     ▼
                                                        ┌──────────────────────────┐
                                                        │ injector                  │
                                                        │ SendInput (Unicode) or    │
                                                        │ clipboard-paste mode      │
                                                        └──────────────────────────┘
```

Everything above the injector is platform-neutral by construction; only
`inject/` and small parts of `ui/` and `app` bootstrap are Windows-specific.

---

## 5. Runtime model

### Threads

| Thread | Owns | Blocking on |
|---|---|---|
| Qt main thread | Tray, settings window, toasts, timers | Qt event loop |
| PortAudio callback threads (one per open input stream) | Nothing — copy frames into the arbiter's per-device ring buffers and return immediately | (real-time, never block) |
| `detector` thread | Mic arbiter, wake-word model, VAD model, segmentation state | `frame_queue.get()` |
| `stt` thread | The Whisper model (single owner — serializes GPU access) | `utterance_queue.get()` |
| `inject` thread | SendInput pacing, clipboard mode | `inject_queue.get()` |
| `provision` thread (transient) | Model download on first run / model change | network I/O |

Workers communicate with the UI via Qt signals (thread-safe queued connections).
Every worker thread runs a top-level `try/except` that logs, emits an error
state to the tray, and restarts its loop — a crash in one stage must never take
the app down silently (§9).

### Messages (dataclasses)

**Deviation (implemented):** these dataclasses live in `scriba/messages.py`
(a small shared module not listed in §8's original layout — added there now).
More substantially, the single end-of-utterance `Utterance` message described
here can't carry the streaming re-decode loop (§7.4a): that loop needs to see
audio *before* the VAD endpoint fires, which a single post-endpoint message
can't express. It is replaced by `AudioChunk`, emitted incrementally by the
detector while an utterance is open; non-streaming mode
(`streaming.enabled = false`) is the same shape with exactly one
`is_final=True` chunk carrying the whole utterance, so both modes share one
message type and one STT-worker code path. `utterance_id` (minted by the
detector per utterance) threads through `AudioChunk` -> `Transcript` ->
`InjectJob` so the injector can tell "new utterance" from "revision of the
utterance I'm already typing". `ForegroundWindow` and `PostprocState` were
also added (not shown in the original snippet) to make the §7.5 casing/window
reset behavior an explicit, pure function argument — see §7.5 below.

```python
@dataclass
class AudioFrame:
    device_id: str          # stable device identifier
    pcm: np.ndarray         # int16 mono, 512 samples @ 16 kHz (32 ms)
    t_monotonic: float

@dataclass
class AudioChunk:
    utterance_id: int
    device_id: str
    pcm: np.ndarray | None  # incremental int16 mono samples; None on a pure finalize marker
    t_monotonic: float
    is_final: bool = False  # True on the chunk carrying (or following) the VAD endpoint
    language: str | None = None  # language-policy resolution, set on the utterance's first chunk

@dataclass
class Transcript:
    text: str
    avg_logprob: float
    no_speech_prob: float
    duration_s: float
    language: str
    utterance_id: int = 0
    is_partial: bool = False   # streaming partial vs. final (§7.4a)

@dataclass
class InjectJob:
    text: str               # text to type; may contain '\n' (injected as Enter)
    erase: int = 0          # backspaces to send first (streaming revision, §7.4a)
    utterance_id: int = 0
    is_final: bool = False

@dataclass
class ForegroundWindow:
    hwnd: int
    title: str
    exe_name: str

@dataclass
class PostprocState:
    capitalize_next: bool = True
    space_before_next: bool = False
    last_hwnd: int | None = None
```

### State machine

Modes (user-selected): `push_to_talk` · `toggle` · `wake_word`.

States (tray-visible):

```
DISABLED ──enable──▶ ARMED ──speech/wake/hotkey──▶ LISTENING ──endpoint──▶ TRANSCRIBING ─┐
   ▲                   ▲                                                                 │
   └──disable──────────┴────────────────────────────(text injected)◀────────────────────┘

PROVISIONING (first run / model change: download+load with progress)
DEGRADED     (running, but on a fallback model/CPU — see §9)
ERROR        (no mic, model load failed after all fallbacks, ...)
```

- `toggle` mode: hotkey or tray click flips DISABLED ⇄ ARMED. While ARMED, VAD
  alone starts utterances (this is the "always on at my desk" mode).
- `push_to_talk` mode: LISTENING only while the hotkey is held; endpoint on
  release.
- `wake_word` mode ("car mode"): ARMED runs the wake-word model; on detection,
  play a short confirmation sound (important in the car — eyes stay on the
  road) and go LISTENING until a configurable sleep phrase ("stop listening")
  or N seconds of silence.

Tray colors: gray=DISABLED, yellow=ARMED, green=LISTENING, blue=TRANSCRIBING
(usually a blink), orange=DEGRADED, red=ERROR, animated badge=PROVISIONING.

---

## 6. Latency budget

For a typical 5-second utterance, target end-of-speech → text-on-screen:

| Stage | Budget | Notes |
|---|---|---|
| Endpoint silence wait | 600 ms | Dominant term; configurable (`vad.endpoint_silence_ms`). PTT mode: 0 (key release is the endpoint). |
| STT (turbo int8, RTX 3050 25 W) | 300–600 ms | beam_size=1, no word timestamps |
| Post-processing | < 5 ms | Pure Python string work |
| Injection (100 chars, type mode) | ~200 ms | 2 ms/char pacing; paste mode ~10 ms |
| **Total** | **≈ 0.9–1.4 s** | |

If measured latency blows this budget, the knobs are (in order): endpoint
silence, beam size, model size, paste mode.

Streaming targets (§7.4a): first words on screen ≤ ~1.5 s after speech starts;
partial refresh cadence ≈ `streaming.interval_ms` (800 ms). The table above
then describes only the *final* reconciliation pass at utterance end.

---

## 7. Component specifications

### 7.1 Audio capture (`scriba/audio/`)

- Library: **sounddevice** (PortAudio; WASAPI on Windows).
- One `InputStream` per *enabled* device: 16 kHz, mono, `int16`,
  `blocksize=512` (32 ms — exactly one Silero VAD frame). If a device can't do
  16 kHz natively, open at its native rate and resample in the callback
  (`soxr`/`scipy.signal.resample_poly`; keep it cheap).
- Callbacks only copy into a per-device ring buffer and push an `AudioFrame`
  to the detector queue. Never do model work in a PortAudio callback.
- **Device management:** enumerate input devices; the settings UI shows them
  with checkboxes (default: all enabled) and live level meters. Handle device
  hot-plug/unplug: listen for WM_DEVICECHANGE (or poll enumeration every few
  seconds — simpler and acceptable), re-open streams, toast on change. A
  Bluetooth mic appearing in the car must start being monitored without a
  restart.
- Each device gets a **pre-roll ring buffer** of `vad.pre_roll_ms` (default
  400 ms) so the first syllable isn't clipped when VAD triggers.

- **Deviation (`audio.wasapi_speech_category`, default `true`):** sounddevice/
  PortAudio has no way to request a WASAPI stream category at all (confirmed
  via its `WasapiSettings` source -- only `exclusive`/`auto_convert`/
  `explicit_sample_format` are exposed), so every device it opens gets
  whatever the *default* (uncategorized) audio-engine graph provides.
  Querying the WinRT `AudioCaptureEffectsManager` on the dev machine showed
  that requesting `AudioCategory_Speech` gets the driver to attach
  AcousticEchoCancellation + NoiseSuppression + AutomaticGainControl, while
  the default/`Communications` categories get none of those. `DEEP_NOISE_SUPPRESSION`
  ("Windows Studio Effects"/Voice Focus) is not available on this hardware's
  driver at all -- a real dead end, not just unresolved.
  `scriba/audio/wasapi_speech.py` opens WASAPI devices directly via a raw
  `IAudioClient2`/`IAudioCaptureClient` COM path (built on `pycaw`'s existing
  `IMMDeviceEnumerator`/`IAudioClient` bindings, extended with the two
  interfaces pycaw doesn't wrap) and calls `SetClientProperties` with
  `eCategory = AudioCategory_Speech` before `Initialize`. `AudioCapture._open_device`
  tries this path first per-device when the config flag is on and a WASAPI
  host-API entry exists, falling back to the sounddevice path unchanged on
  any failure -- so worst case for any given device is no different from
  before. Shipped default-on despite mixed lab evidence: a synthetic
  SAPI-speech + pink-noise test via WinRT `MediaCapture` showed +31% clearer
  recovered speech (amplitude-normalized cross-correlation, to control for
  AGC's gain effect), but the same test against the production raw-COM
  implementation showed the opposite (-44%) with also a 10x swing in the
  sounddevice baseline's raw RMS between otherwise-identical runs -- strong
  evidence that speaker-playback-loopback lab testing on this laptop is too
  noisy a proxy for the real problem (background noise while driving) to
  trust either result. Judge this one by real dictation quality in the field,
  not another synthetic benchmark.

### 7.2 Mic arbiter + VAD + segmentation (`scriba/detect/`)

**Why an arbiter:** multiple live mics will all hear the same speech. Exactly
one stream may feed an utterance, or the user gets double text.

- Run Silero VAD **independently per enabled device** (it's tiny; even 4
  devices are negligible CPU).
- When one or more devices cross the speech threshold within an arbitration
  window (~200 ms), pick the winner by highest mean VAD probability (tie-break:
  highest RMS). The winner is **sticky for the whole utterance** — no
  mid-utterance switching.
- All other devices are ignored (not closed) until the utterance ends.
- Optional per-device priority in config (e.g. always prefer the headset when
  present).

**Segmentation state machine** (runs on the winning device's frames):

- Speech start: VAD probability ≥ `vad.threshold` (default 0.5) for ≥ 2
  consecutive frames → emit utterance start; prepend pre-roll buffer.
- Speech end: probability below threshold for `vad.endpoint_silence_ms`
  (default 600) → close utterance, push `Utterance` to STT queue.
- Guards: discard utterances shorter than `vad.min_speech_ms` (default 250);
  force-flush at `vad.max_utterance_s` (default 30) at the last sub-threshold
  frame, then continue a new utterance seamlessly.

### 7.3 Wake word (`scriba/detect/wakeword.py`)

- Library: **openWakeWord** (onnxruntime CPU). It consumes 80 ms/1280-sample
  chunks at 16 kHz; the detector thread re-chunks frames for it.
- Runs **only** in `wake_word` mode while ARMED, on all enabled devices (the
  arbiter applies to the wake event too — the triggering device wins the
  subsequent utterance... or re-arbitrate at first speech; implementer's
  choice, document it).
- v1 ships with a pre-trained phrase (e.g. "hey jarvis") to validate the
  pipeline; a custom **"hey Scriba"** model trained with openWakeWord's
  synthetic-data pipeline is a follow-up task (M3 stretch).
- False-positive controls: detection threshold (default 0.6), require VAD
  agreement within ±300 ms, 2 s refractory period after any detection,
  confirmation beep on activation, sleep phrase and/or
  `wake_word.auto_sleep_s` (default 15 s of no speech) to return to ARMED.

### 7.4 STT engine (`scriba/stt/`)

**Interface** (the seam for future backends):

```python
class SttBackend(Protocol):
    def load(self, progress_cb: Callable[[float, str], None]) -> None: ...
    def transcribe(self, utt: Utterance, hotwords: str | None) -> Transcript: ...
    def unload(self) -> None: ...
    @property
    def descriptor(self) -> str: ...   # e.g. "large-v3-turbo/int8_float16/cuda"
```

**Local backend (`whisper_local.py`)** — the only v1 implementation:

- `faster_whisper.WhisperModel(model_name, device=..., compute_type=...)`.
- Defaults: `model="large-v3-turbo"`, `device="auto"`,
  `compute_type="int8_float16"` on CUDA / `"int8"` on CPU.
- Transcribe parameters:
  - `language`: resolved per utterance by the **language policy** (§7.10) —
    a fixed code, or per-utterance detection in `auto`/`mixed` modes.
  - `beam_size=1`, `temperature=0.0`, `condition_on_previous_text=False`
    (prevents cross-utterance hallucination loops),
    `vad_filter=False` (we already segmented), `without_timestamps=True`.
  - `hotwords`: rendered from the vocabulary (§7.6).
- The model is loaded once and owned by the STT thread. Model changes from the
  settings UI go through `unload()` → PROVISIONING → `load()`.
- First inference is slow (CUDA context + kernel warmup): run a ~1 s silent
  warmup transcription at load time so the first real dictation isn't laggy.

**Deviation (implemented, user request): background-noise suppression.**
The user reported background noise hurting accuracy noticeably more than it
does for Windows' own dictation. `_denoise()` applies `noisereduce`
(spectral gating, `stationary=False` to adapt to fluctuating noise rather
than assuming a fixed floor) to the float32 audio buffer immediately before
every decode call (partial and final), gated by `stt.denoise` (default on).
Benchmarked on this machine: ~20-60ms for a 1.5-10s buffer, negligible
against the §6 latency budget. `deepfilternet` (a real neural denoiser,
likely higher quality) was evaluated first and rejected: its `deepfilterlib`
dependency requires a Rust/cargo toolchain to build from source, with no
prebuilt Windows wheel -- violates this project's no-compilation rule
(`docs/WINDOWS-SETUP.md`). `noisereduce` is pure numpy/scipy, no compiled
extension, proven fast. Applied only to the STT decode path, not to the
VAD/segmentation input -- Silero VAD's own probability estimates are left
on raw audio, since denoising is tuned for decode-time intelligibility, not
speech/silence boundary detection, and touching VAD input was out of scope
for this fix.

**Model provisioning / first run:**

- Models download **at runtime** from Hugging Face (faster-whisper handles
  this) into `%LOCALAPPDATA%\Scriba\models`. `large-v3-turbo` int8 is roughly
  a 1.5 GB download.
- The download runs in the `provision` thread; `progress_cb(fraction, label)`
  drives the tray (PROVISIONING state, percentage in tooltip + a determinate
  progress bar in the first-run window + toast on completion). The app stays
  responsive; dictation is unavailable until load completes.
- Downloads must be resumable/retryable (huggingface_hub does this) and a
  failed download must land in ERROR with a "Retry" menu action, not a crash.

### 7.4a Streaming partials (`scriba/stt/streaming.py`)

Windows-parity dictation: words appear while the user speaks and self-correct
as context clarifies. Whisper is not natively streaming, so this is built from
two cooperating mechanisms — **periodic re-decoding** in the STT worker and a
**revision protocol** in the injector. (This is the established approach; cf.
the whisper_streaming / LocalAgreement literature.)

**Re-decode loop (STT worker).** While an utterance is open (VAD has started
it but not endpointed), re-transcribe the accumulated utterance audio every
`streaming.interval_ms` (default 800). Each pass yields a candidate text for
the whole utterance so far.

**Stability policy — LocalAgreement-2.** Tokens become *committed* once two
consecutive decode passes agree on them (longest common token prefix of the
last two passes). Two policies, config `streaming.policy`:

- `eager` (default — this is the Windows-like behavior the user wants): type
  everything from the latest pass immediately, committed or not; later passes
  revise what changed.
- `stable`: type only committed tokens; the unstable tail is withheld. Fewer
  visible corrections, text trails the voice slightly.

**Revision protocol (injector).** The injector keeps the exact string it has
typed for the current utterance. Each partial update computes the longest
common prefix between the typed string and the new candidate; it then sends
`VK_BACK` × (typed − prefix) followed by the new suffix (`InjectJob.erase` +
`text`). The **final** transcript — after the full post-processing pipeline
(commands, vocabulary, casing, §7.5) — reconciles the same way, then the
utterance buffer resets. Expect one visible "snap" at utterance end when
post-processing lands, same as Windows dictation.

**Window management.** Re-decode cost grows with utterance length. Cap the
decoded audio at `streaming.window_s` (default 15 s): audio older than the
window whose text is already committed is dropped from the decode input and
its committed text is fed as decoder prefix/`initial_prompt` context instead,
so long dictation stays real-time without losing context.

**Constraints and honest limitations:**

- Streaming requires `inject.method = "type"`. Paste mode can't revise —
  apps with a per-app paste override get whole-utterance behavior instead.
- If the foreground window changes mid-utterance, the injector must **never**
  backspace into the new window: revision is abandoned, the utterance is
  finalized, and the buffer resets.
- Manual typing during an active utterance corrupts the revision diff — known
  limitation, identical to Win+H; document it for the user.
- GPU duty cycle rises during speech (continuous re-decode on the 3050,
  idle between utterances) — acceptable for dictation bursts.
  `streaming.enabled = false` restores plain utterance-at-once mode, and the
  DEGRADED CPU fallback rungs (§9) force it off automatically (CPU decode
  isn't fast enough to re-decode on a cadence).
- Partial passes get whitespace normalization only; spoken commands, filler
  removal, vocabulary correction, and casing run **only on the final pass**
  (they are not stable mid-stream).

### 7.5 Text post-processing (`scriba/text/`)

**Pure functions only** —
`(Transcript, PostprocState, config, foreground: ForegroundWindow | None) → (list[InjectJob], PostprocState)`.
No I/O, no globals: this is the most unit-testable and most behavior-defining
part of the app. **Deviation (implemented):** `foreground` is an explicit
argument (the injector's foreground-window query result, threaded in by the
caller) rather than the pipeline querying it itself — needed so the §7.5
point 5 "reset on window change" rule stays a pure comparison instead of I/O
inside `text/`.

The full pipeline runs on **final** transcripts only; streaming partials
(§7.4a) bypass it except for whitespace normalization, and the injector's
revision protocol reconciles the post-processed final against the typed
partials.

Pipeline order:

1. **Hallucination filter.** Drop the transcript entirely if: empty/whitespace;
   `no_speech_prob > 0.6`; `avg_logprob < -1.0`; duration < `vad.min_speech_ms`;
   or text ∈ blocklist. Blocklist ships with Whisper's classic silence
   artifacts ("Thank you.", "Thanks for watching!", "you", subtitle credits)
   per language, extensible in config.
2. **Spoken commands** (`scriba/text/commands.py`, implemented in M1 rather
   than M2 — pulled forward on user request). Token-level command table,
   EN+DE, applied to trailing and standalone commands (v1 legacy parity plus
   new entries):

   | Spoken (EN) | Spoken (DE) | Output |
   |---|---|---|
   | period / full stop / and period | punkt / und punkt | `.` (sets sentence-end state) |
   | comma / and comma | komma / und komma | `,` |
   | question mark | fragezeichen | `?` (sentence-end) |
   | exclamation mark | ausrufezeichen | `!` (sentence-end) |
   | new line | neue zeile | `\n` |
   | new paragraph | neuer absatz | `\n\n` |
   | colon | doppelpunkt | `:` |
   | hit enter | — | `\n` (added; user asked for an explicit "hit enter" -> Enter command) |

   **Deviations (implemented):** "sets sentence-end state" is not tracked as
   separate boolean state — `casing.py`'s existing rule (capitalize after
   text ending in `.?!`/`\n`) already derives it purely from the *output*
   text, so a command only needs to produce the right trailing character.
   "Applied to trailing and standalone" is implemented literally and only —
   i.e. a command phrase is recognized as the *entire* utterance or the
   words at the very *end* of it, never replaced mid-utterance — so a
   literal sentence like "the trial period is 30 days" is never mangled.
   The table does not live in config yet (it's a Python tuple in
   `commands.py`); making it config-editable is deferred, unlike the rest of
   this deviation which was pulled forward.
3. **Filler removal** (`scriba/text/pipeline.py`, implemented in M1 rather
   than M2; configurable via `postproc.filler_removal`, default on): strips
   `um, umm, uh, uhh, hm, hmm, ah, er, erm, äh, ähm, ähh` word-boundary
   matches (case-insensitive), regardless of the utterance's detected
   language (simpler than per-language lists and low false-positive risk,
   since the two lists barely overlap). **Deviation:** runs *before* spoken
   commands, reversing the order listed above — its whitespace-collapsing
   cleanup would otherwise destroy a `new line`/`new paragraph`/`hit enter`
   command's just-produced `\n`/`\n\n`. Fillers never occur inside a command
   phrase, so the swap is equivalent for every other case.
4. **Vocabulary correction** — see §7.6.
5. **Casing & spacing state machine.** Carries `PostprocState` across
   utterances: if the previous injected text ended a sentence (`.?!` or `\n`),
   capitalize the next utterance's first letter and don't prepend a space
   after `\n`; otherwise prepend one space and lowercase the first word
   (unless it's a vocabulary term with fixed casing). **Reset the state when
   the foreground window changes** between utterances (the injector reports
   the hwnd it last typed into) — continuing mid-sentence into a different app
   makes no sense.
6. **Optional umlaut folding** (`postproc.umlaut_fold`, default **off** —
   Unicode injection types ä/ö/ü/ß natively; the toggle exists for pure-ASCII
   contexts).

### 7.6 Vocabulary system (`scriba/text/vocabulary.py`)

One user-maintained file drives **two** mechanisms:

`%APPDATA%\Scriba\vocabulary.txt`, one entry per line:

```
# canonical | sounds-like alias, alias2, ...
kubectl | cube control, cube cuttle, cube c t l
systemd | system d
WSL2 | w s l two, wsl two
Terraform
CLAUDE.md | claude m d
```

1. **Recognition biasing:** all canonical terms are joined into the
   faster-whisper `hotwords` string (and, later, become the AWS custom
   vocabulary). This raises the model's prior for those tokens.
2. **Post-correction:** after transcription, scan the text for fuzzy matches
   (rapidfuzz, token-level and bigram/trigram window for multi-word aliases)
   against the canonical terms **and** their sounds-like aliases; replace at
   similarity ≥ `postproc.correction_threshold` (default 87, tune with tests).
   Canonical casing always wins (`WSL2` never becomes `wsl2`).

The settings window includes a vocabulary editor (add/remove/edit lines) and
the file is watched for external edits (reload on change). Corrections applied
are logged at INFO ("corrected 'cube control' → 'kubectl'") so the user can
debug surprises.

### 7.7 Text injection (`scriba/inject/`)

- **Primary method:** Win32 `SendInput` with `KEYEVENTF_UNICODE` — per
  character, `wVk=0, wScan=<code unit>`, down+up; **surrogate pairs** sent as
  two events each (emoji-safe); `\n` sent as `VK_RETURN` down/up. This is
  keyboard-layout independent and types umlauts directly (fixes the v1
  layout bug).
- Pacing: `inject.per_char_delay_ms` (default 2). Terminals (Windows Terminal,
  VS Code terminal) are the primary consumers and handle this fine.
- **Paste mode** (config global default + per-app override by exe name): set
  clipboard → send Ctrl+V → restore previous clipboard after a short delay.
  Use for apps that mishandle synthetic Unicode input or for long texts.
- Before injecting, read the foreground window (hwnd, title, exe name) — used
  for per-app overrides, the postproc state reset (§7.5), and logging. If no
  foreground window, drop the job with a warning toast.
- **Known limitation to document for the user:** UIPI blocks injection into
  elevated windows unless Scriba itself runs elevated. Detect the failure
  (SendInput returns 0 / foreground exe is elevated) and toast a clear message
  rather than failing silently.

### 7.8 UI (`scriba/ui/`)

PySide6 throughout; the Qt event loop is the main thread.

- **Tray icon** (`QSystemTrayIcon`): state colors per §5; left-click = toggle
  enable/disable; tooltip shows state + active model + language. Context menu:
  - Enable/Disable
  - Mode: Push-to-talk / Toggle / Wake word
  - Language: English / Deutsch / Mixed (EN+DE) / Auto
  - Flag last utterance… (visible when `[adaptation]` is enabled)
  - Settings…
  - Open log / Open vocabulary
  - Quit
  - **Deviation (implemented):** no separate Model menu item -- model choice
    is derived from Language, not independent, see §9's idle-unload note and
    `scriba.stt.language.model_for_language`.
  - **Deviation (implemented, user request):** `scriba/ui/tray_pin.py`
    best-effort pins the tray icon to Windows' always-visible area (like
    OneDrive/VPN clients) instead of defaulting into the hidden-icons
    overflow chevron, via the undocumented
    `HKCU\Control Panel\NotifyIconSettings\<uid>\IsPromoted` registry value
    (verified empirically on Windows 11 against another already-pinned
    app's own entry). Not a public API; wrapped so any failure is silent and
    only ever affects icon *position*, never functionality. Called ~3s after
    `tray.show()` since Windows registers the entry lazily; persists across
    future launches once it succeeds once.
- **Settings window** (opened on demand, closable without quitting):
  - *Audio*: device list with enable checkboxes + live level meters, per-device priority
  - *Model*: read-only display of the active model (derived from Language, see above),
    device (auto/cuda/cpu), current VRAM note, "re-download model"
  - *Vocabulary*: editor for vocabulary.txt
  - *Commands & text*: spoken-command table, filler removal toggle, umlaut fold
  - *Hotkeys*: PTT key, toggle key, language-switch key (with capture widget)
  - *Wake word*: phrase, threshold, sleep phrase, auto-sleep, sounds on/off
- **First-run wizard**: pick language(s) → pick mics → model downloads with a
  determinate progress bar (also mirrored in the tray, per the PROVISIONING
  state) → "try it: focus Notepad and hold <hotkey>".
- **Toasts** via `QSystemTrayIcon.showMessage` for: mode/language changes,
  device hot-plug, degraded fallbacks, injection-blocked warnings.
- **Hotkeys**: global hotkeys must work when Scriba is not focused, including
  key-up detection for push-to-talk. Implementation options: `RegisterHotKey`
  (no key-up events — fine for toggle) plus a low-level keyboard hook
  (`WH_KEYBOARD_LL` via pywin32/ctypes, or the `keyboard` package as in v1)
  for PTT hold semantics. Requirement, not implementation, is locked: *hold to
  talk must work globally*.
- **Sounds**: short activation/deactivation cues (Qt multimedia or winsound),
  essential for car mode; toggleable.

### 7.9 Configuration (`scriba/config.py`)

- Location: `%APPDATA%\Scriba\config.toml` (created with defaults on first
  run). Vocabulary sits next to it. Models and logs under
  `%LOCALAPPDATA%\Scriba\`.
- Read with `tomllib`; written by the settings UI (use `tomlkit` to preserve
  comments, or accept regenerated files — implementer's choice).
- Schema (defaults shown):

```toml
[general]
mode = "toggle"                # push_to_talk | toggle | wake_word
language = "en"                # en | de | auto | mixed  (see §7.10)

[audio]
enabled_devices = []           # empty = all input devices
device_priority = []           # optional ordered exe of preferred device names

[vad]
threshold = 0.5
endpoint_silence_ms = 600
pre_roll_ms = 400
min_speech_ms = 250
max_utterance_s = 30

[wake_word]
model = "hey_jarvis"           # until custom "hey scriba" is trained
threshold = 0.6
sleep_phrase = "stop listening"
auto_sleep_s = 15
sounds = true

[stt]
backend = "local"              # local | aws (future)
model = "large-v3-turbo"
device = "auto"                # auto | cuda | cpu
compute_type = "int8_float16"
beam_size = 1
languages = ["en", "de"]       # candidate set for language = "mixed"
language_confidence_min = 0.6  # below this, fall back to languages[0]
initial_prompt = ""            # optional decoder priming, see §7.10(a)

[adaptation]
enabled = false                # "flag last utterance" accent flywheel, §7.10(d)

[streaming]
enabled = true                 # Windows-parity partials, §7.4a
policy = "eager"               # eager | stable
interval_ms = 800
window_s = 15

[postproc]
filler_removal = true
umlaut_fold = false
correction_threshold = 87
blocklist_extra = []

[inject]
method = "type"                # type | paste
per_char_delay_ms = 2

[inject.per_app]               # exe name -> method override
# "someapp.exe" = "paste"

[hotkeys]
toggle = "ctrl+alt+d"
push_to_talk = "ctrl+alt+space"
language_switch = "ctrl+alt+l"
```

(Default hotkeys deliberately avoid Win+H, which remains bound to Windows
dictation.)

### 7.10 Language policy: accents & mixed-language speech (`scriba/stt/language.py`)

The user's real speech is American English with a German accent, sometimes
mixing German words into English sentences (and vice versa). Three distinct
problems, three distinct mechanisms:

**(a) Accented English.** This is mostly a model-quality question, and it's the
reason the default model is `large-v3-turbo` rather than a small one: Whisper's
large variants were trained on heavily accented English and handle it far
better than Windows dictation (whose weakness with accents is what drove the
user away). Where the accent still causes *systematic* mishearings of specific
terms, that is exactly what the vocabulary's sounds-like aliases are for —
"cube cuttle → kubectl" is an accent artifact, and the alias column should be
written against how the user actually pronounces things. Additionally,
`stt.initial_prompt` (config, default empty) lets the user prime the decoder
with a sentence or two of representative text ("Transcript of a software
engineering dictation about Kubernetes, WSL2, and Terraform."), which measurably
nudges style and domain vocabulary. True per-voice adaptation (fine-tuning on
the user's own corrections) is future work — but v1 lays its groundwork, see
**(d)** below.

**(b) Switching languages between utterances.** Because VAD hands the STT
worker one utterance at a time, per-utterance language detection gives natural
sentence-level code-switching. Language modes (config `general.language`,
also in the tray menu):

- `"en"` / `"de"` — fixed language, fastest and most accurate. Default `"en"`,
  with the `language_switch` hotkey and tray toggle as in v1.
- `"auto"` — Whisper's built-in detection over **all** languages. Unreliable
  on short utterances; kept as an option, not recommended.
- `"mixed"` — detection **restricted to `stt.languages`** (default
  `["en", "de"]`): run faster-whisper's language-detection pass, but choose the
  argmax only among the configured set. Restricting the choice to two known
  candidates makes short-utterance detection dramatically more reliable than
  open-set auto. If the winning probability is below
  `stt.language_confidence_min` (default 0.6 — i.e. genuinely ambiguous,
  usually *because* the utterance is mixed), fall back to the list's first
  entry as the primary language. This is the recommended mode for Denglish
  speakers.

**(c) Mixing languages within one utterance** ("Wir müssen das deployment
rebooten"). Honest engineering position: Whisper cannot code-switch word-level
within a single decode — it transcribes in one language and will usually
render embedded foreign words phonetically or translate them. Mitigations, in
order of practical value:

1. Whisper handles embedded **proper nouns and technical terms** (the dominant
   Denglish case for this user: English tech terms inside German sentences)
   surprisingly well even in `de` mode, because those terms are frequent in
   its training data. Expect this to mostly just work.
2. The **vocabulary system is language-agnostic**: canonical terms + aliases
   are applied to the post-correction pass regardless of utterance language,
   so a mangled embedded term gets repaired the same way an accent artifact
   does. Aliases may be written in either language's phonetics.
3. What we deliberately do **not** do in v1: dual-decode every utterance in
   both languages and merge (doubles GPU latency for marginal gain), or
   word-level language tagging (research-grade, not product-grade). Recorded
   as considered-and-rejected.

**(d) Groundwork for personal adaptation (the accent flywheel).** A global
hotkey / tray action "**flag last utterance**" saves the utterance's audio
(WAV) plus the emitted text into `%LOCALAPPDATA%\Scriba\adaptation\`, and
opens a tiny dialog where the user types what they *actually* said. Opt-in,
local-only, off by default (`[adaptation] enabled = false`). This costs almost
nothing to build in v1 and quietly accumulates a (user-voice audio → corrected
text) dataset. Future work (§14) turns that into a LoRA fine-tune of the
Whisper model — the only real fix for an individual accent — and until then
the flagged pairs are a goldmine for writing better sounds-like aliases.

### 7.11 Logging & diagnostics

- Rotating file log at `%LOCALAPPDATA%\Scriba\logs\scriba.log` (1 MB × 3),
  console when run from a terminal. INFO default, DEBUG via config/env var.
- Log per utterance: winning device, utterance duration, STT wall time, final
  text (INFO), corrections applied, injection target exe.
- A `--diagnose` CLI flag prints: detected devices, CUDA availability,
  cuDNN/cublas resolution, model cache state, and runs a synthetic 3 s
  transcription benchmark. This is the first thing to ask a user (or a Claude
  session) to run when something is broken.

---

## 8. Repository layout

```
scriba/                     # the package (proper __init__.py, relative imports)
  app.py                    # entry point: bootstrap, single-instance mutex, Qt loop
  config.py
  messages.py               # shared dataclasses: AudioFrame/AudioChunk/Transcript/
                             # InjectJob/ForegroundWindow/PostprocState (§5, deviation)
  logging_setup.py
  singleinstance.py         # named-mutex single-instance guard
  audio/
    capture.py              # device enumeration, streams, ring buffers, hot-plug
  detect/
    arbiter.py               # multi-mic arbitration
    vad.py                  # Silero ONNX wrapper (via onnxruntime, no torch) +
                             # segmentation state machine
    wakeword.py             # openWakeWord wrapper
  stt/
    base.py                 # SttBackend protocol, Transcript
    whisper_local.py        # faster-whisper backend + provisioning
    streaming.py            # re-decode loop + LocalAgreement-2 (§7.4a)
    language.py             # language policy: fixed/auto/mixed resolution (§7.10)
    # aws_transcribe.py     # future (M5)
  text/
    pipeline.py             # ordered pure pipeline
    commands.py             # spoken-command table + matching
    corrections.py          # vocabulary fuzzy correction
    vocabulary.py           # vocabulary.txt parsing/watching
    casing.py               # casing/spacing state machine
  inject/
    base.py
    windows.py              # SendInput UNICODE, paste mode, foreground query
  ui/
    tray.py
    settings.py
    firstrun.py
    hotkeys.py
    sounds.py
tests/
  test_commands.py          # golden tests, EN+DE
  test_corrections.py
  test_casing.py
  test_pipeline.py
  test_vad_segmentation.py  # fixture WAVs through segmentation only
  fixtures/*.wav
docs/
  DESIGN.md                 # this file
  PLAN.md                   # milestones & acceptance criteria
pyproject.toml              # uv-managed; console entry `scriba = scriba.app:main`
```

Dependencies (runtime): `faster-whisper`, `sounddevice`, `numpy`,
`onnxruntime` + a runtime-downloaded Silero VAD ONNX file, `openwakeword`,
`PySide6`, `rapidfuzz`, `pywin32`, `keyboard`, `tomlkit`; plain (non-extra)
deps: `nvidia-cudnn-cu12`, `nvidia-cublas-cu12`.
Dev: `pytest`, `ruff`.

**Deviations (implemented):**
- The `silero-vad` PyPI package unconditionally requires `torch`+`torchaudio`
  (multi-GB; only its `onnx-cpu` extra skips them) — this contradicts the "tiny
  ONNX, no torch" rationale in §3, so `detect/vad.py` depends on `onnxruntime`
  directly and downloads the ~2 MB `silero_vad.onnx` file at runtime (same
  provisioning treatment as the STT model, into `models_dir()`), not the pip
  package.
- `nvidia-cudnn-cu12`/`nvidia-cublas-cu12` are plain dependencies, not a pip
  extras group as "GPU extra" might imply — there is exactly one target
  machine (§0) and it always has the RTX 3050, so an extras flag would only
  create a way to forget them.
- `pyproject.toml` sets `[tool.uv] environments = ["sys_platform == 'win32'"]`
  so uv's universal resolver doesn't try to solve Linux-only markers (e.g.
  `openwakeword`'s `tflite-runtime` dependency) that have no matching wheel
  for our Python version — irrelevant anyway since this is Windows-only.

---

## 9. Failure handling & degradation

**Model fallback ladder** (attempted in order at load; current rung shown in
tray tooltip; below rung 1 ⇒ DEGRADED/orange):

1. configured model, `int8_float16`, CUDA
2. same model, CPU `int8` — only if CUDA init fails; likely too slow for turbo, so:
3. `small`, `int8_float16`, CUDA (~0.5 GB — the "GPU is busy" rung)
4. `small`, `int8`, CPU (real-time capable on a modern laptop CPU)
5. ERROR state with actionable message

Trigger rungs on: CUDA unavailable, cuDNN load failure, CUDA OOM (also caught
per-transcription — an OOM mid-run drops a rung and retries the utterance once).

**Idle-unload (implemented, user request, not in the original design):**
CTranslate2 keeps a full host-RAM copy of the model weights resident
alongside the GPU copy for the life of the model object — confirmed
empirically (host RAM scales with model file size, e.g. ~1.35 GB for
large-v3-turbo vs. ~200 MB for `small`; `gc.collect()`/forcing a Windows
working-set trim don't reduce it) and matches an open, unresolved upstream
issue ([CTranslate2 #1787](https://github.com/OpenNMT/CTranslate2/issues/1787)).
No supported flag exists to disable this. Mitigation: `stt.idle_unload_minutes`
(default 60, 0 disables) unloads the backend after that long with dictation
disabled, freeing the host RAM; the next activation (hotkey/tray) or an
explicit model switch (new tray Model submenu, large-v3-turbo vs.
`distil-large-v3`) transparently reloads it first — tray goes PROVISIONING
with a start/live-progress/finish toast sequence so a multi-second reload is
never mistaken for the app being stuck, then dictation proceeds.

**Other failures:**

- No input device at startup → ERROR + toast; recover automatically on hot-plug.
- Device disappears mid-utterance → discard partial utterance, toast, re-arm.
- Wake-word/VAD model file corrupt → re-download via provisioning path.
- Worker thread exception → log with traceback, tray ERROR blink, restart the
  worker loop; after 3 crashes in 60 s, stay in ERROR (no crash-loop spin).
- Injection failure (UIPI / no foreground) → toast with the reason; the
  transcript is also copied to the clipboard as a consolation so the words
  aren't lost.

---

## 10. Testing strategy

- **Unit (no hardware, the bulk):** the entire `text/` pipeline is pure —
  golden-file tests for commands (EN+DE), corrections (including the
  "cube control → kubectl" style cases), casing across utterance sequences,
  hallucination filtering. These encode the product's behavior; write them
  alongside, not after, the pipeline.
- **Component:** VAD segmentation over committed fixture WAVs (short clips w/
  known speech spans) — assert utterance boundaries within tolerance. Runs on
  CPU, no GPU needed.
- **Integration (manual/local, GPU):** `pytest -m gpu` — fixture WAV →
  whisper_local → pipeline → assert expected text. Excluded by default.
- **Injection:** a `FakeInjector` capturing InjectJobs for pipeline tests; a
  manual smoke script that types into Notepad.
- **Bench:** `scriba --diagnose` doubles as the perf harness (per-stage
  timings against the §6 budget).
- No CI initially (the legacy repo's CI never ran once); local `pytest` +
  `ruff` are the gate. GitHub Actions for lint+unit (CPU-only) can come later.

---

## 11. Security & privacy

- Audio never leaves the machine in v1. The only network traffic is the
  first-run model download from Hugging Face.
- Everything the user says near an ARMED machine may be typed into the focused
  window — the tray state must always be glanceable, and DISABLED must be one
  click/hotkey away. Wake-word mode plays audible cues for state changes.
- No telemetry.

---

## 12. Packaging & install (M4)

- Primary: `uv tool install` / `pipx` from a wheel, `scriba` console script,
  optional `--autostart` flag that registers a Run-key entry.
- Stretch: PyInstaller one-dir build. Note: CUDA wheels make this multi-GB;
  evaluate whether the wheel+uv path is simply better. Decision deferred to M4.
- The `scriba.ico` at repo root is the app/tray icon asset.

---

## 13. Why not … (recorded for posterity)

- **Rust/Go:** single-binary distribution is the only major win; every
  latency-critical piece here is already native code behind Python bindings,
  and the Windows UI/audio/injection ecosystem in Python is mature. Revisit
  only if packaging (M4) becomes painful.
- **WhisperX:** its extras (alignment, diarization) solve problems dictation
  doesn't have; standalone Silero VAD gives us the same anti-hallucination
  benefit with less machinery.
- **insanely-fast-whisper:** optimized for batch throughput on large-VRAM
  GPUs; dictation is one short utterance at a time on a 4 GB card.
- **Streaming cloud STT for latency:** local turbo inference is already inside
  the human-perceptible budget; the cloud adds cost, a network dependency, and
  the v1 failure mode back.

---

## 14. Future work (explicitly out of v1 scope)

- **AWS Transcribe backend** (`stt/aws_transcribe.py`): implements
  `SttBackend` using the `amazon-transcribe` SDK (not the hand-rolled SigV4 of
  v1); custom vocabulary synced from vocabulary.txt; **cost guards are
  mandatory**: VAD-gated connect only, idle disconnect ≤ 10 s, and a local
  daily minute budget that flips the tray to DEGRADED when exhausted.
- **Client/server split:** the `detector → stt` queue boundary becomes a
  WebSocket (16 kHz PCM frames up, transcripts down); a thin edge client owns
  capture+injection, the engine runs in WSL, on a LAN box, or centrally with
  one shared AWS key. The dataclasses in §5 are the wire schema, verbatim
  (including partial transcripts and revision jobs).
- **Linux/macOS clients:** `inject/` grows `linux.py` (wtype/ydotool/uinput)
  and `macos.py` (CGEventPost + Accessibility permission); capture and UI are
  already cross-platform (PortAudio/Qt).
- **Custom "hey Scriba" wake-word model** via openWakeWord's synthetic
  training pipeline.
- **Personal accent fine-tune:** once the adaptation flywheel (§7.10d) has
  accumulated a few hours of (audio, corrected text) pairs, LoRA-fine-tune the
  Whisper model on the user's voice. Training won't fit the 4 GB laptop GPU —
  but the user has CLI access to a local **NVIDIA DGX H200 cluster running
  Run:ai**, which makes this a routine job rather than a cloud errand:
  `runai training submit` a single-GPU PyTorch job (HF `transformers` + PEFT
  LoRA on the turbo checkpoint; a few hours of personal audio trains in well
  under an hour on an H200), sync the adaptation dataset up as the job's
  input, then convert the merged model back for local inference with
  `ct2-transformers-converter` (faster-whisper runs CTranslate2 format, not
  HF checkpoints — the conversion step is mandatory, budget it into the
  pipeline). The same cluster is the natural place to run openWakeWord's
  synthetic-data training for the custom "hey Scriba" model. Personal
  fine-tuning is the only real fix for individual-accent systematics, and it
  is why flagged utterances are collected from day one.
- **USB button/footswitch support** is free already: such devices enumerate as
  keyboards, so binding the PTT/toggle hotkey to the button's keycode suffices.
  Document it; no code needed.

### 14.1 Readout mode — the reverse direction (TTS)

Roadmap item, fully feasible, fully local. The user wants AI-agent output
(Claude Code in particular) read aloud by a **female British-accented voice**:
a spoken *summary* of what the agent wrote, with a voice keyword like "more
details" to get the full picture — hands-free consumption to match the
hands-free dictation.

**TTS engine:** **Kokoro-82M** (Apache-licensed, ~82 M params) is the current
recommendation: near-human quality, ships British female voices (`bf_emma`,
`bf_isabella`), runs faster than real-time on CPU and trivially on the GPU
alongside Whisper (it's tiny). Fallback option: Piper (`en_GB` voices, lighter,
lower quality). Both are offline. A new `scriba/tts/` module mirrors `stt/`:
a `TtsBackend` protocol, a Kokoro implementation, and playback through a
sounddevice output stream with a playback queue (sentence-chunked so "stop"
takes effect immediately, not after a monologue).

**Getting the text out of Claude Code — two clean options, both supported:**

1. **MCP server (preferred).** Scriba exposes a local MCP server with a
   `speak(summary, full_text?)` tool. Claude Code is instructed (via
   CLAUDE.md / output style) to call it with a 1–3 sentence spoken summary of
   each substantive response. Elegant consequence: **the agent writes its own
   summary** — no second summarizer model needed, and the summary quality is
   the agent's own.
2. **Stop-hook adapter.** A Claude Code Stop hook posts the final assistant
   message to Scriba's local endpoint (localhost HTTP or named pipe). Scriba
   then needs a summarizer of its own: a small local LLM (Llama 3.2 3B / Qwen
   3B int4 — tight next to Whisper on 4 GB, or CPU) or a cheap cloud call.
   This path works with *any* agent, not just MCP-capable ones, at the cost of
   owning summarization.

**Voice-command intents ("more details").** Scriba already owns the microphone
and STT — readout mode adds an **intent layer** in front of dictation: while
(or right after) something was read aloud, a small phrase table is matched
before text is treated as dictation — "more details" (types the follow-up
request into the focused agent window via the existing injector and hits
Enter), "repeat", "read everything", "stop". This reuses the entire existing
pipeline; the only new machinery is the mode flag and phrase table.

**The one real technical constraint: half-duplex.** While TTS plays, the mics
hear the speaker — Scriba must suspend VAD/wake-word during playback (plus a
~300 ms tail) or it will transcribe its own voice. v1 of readout mode is
half-duplex with the mic re-armed between sentence chunks (so "stop" works in
the gaps); true barge-in (listening while speaking, via echo cancellation) is
a further refinement, not a prerequisite.

### 14.2 Rewrite mode — fix selected text in place (LLM)

Roadmap item. The user selects text in *any* application (a prompt line in a
terminal, a paragraph in a browser or editor), presses a hotkey, and Scriba
replaces the selection with a corrected version — grammar/spelling/style fix,
or a full "rewrite in proper English" for non-native phrasing.

**Mechanics (reuses M1 infrastructure almost entirely):**

1. **Capture the selection.** Save the current clipboard → send the app's
   copy chord → wait for a clipboard change (timeout ~500 ms) → read the text.
   The copy chord is configurable **per app** (default `Ctrl+C`; terminals
   need `Ctrl+Shift+C` since bare Ctrl+C may be SIGINT — reuse the §7.7
   per-app override table). A cleaner capture path exists for apps that
   support UI Automation's TextPattern (read the selection without touching
   the clipboard); use it opportunistically, fall back to the clipboard trick.
2. **Transform** via a pluggable provider (below), with a named *action*
   selecting the instruction. Ships with two actions on two hotkeys:
   `fix` ("correct grammar, spelling, punctuation; preserve meaning, line
   breaks, and formatting; output only the corrected text") and `rewrite`
   ("rewrite in clear, natural English; preserve meaning and formatting").
   The action table lives in config; users add their own prompts/hotkeys.
   Later synergy with dictation: hold the rewrite hotkey and *speak* the
   instruction ("make this more formal") — the STT pipeline already exists.
3. **Replace in place.** Set the clipboard to the result → send the paste
   chord (per-app configurable, `Ctrl+Shift+V` for terminals) → restore the
   original clipboard. In editable controls the still-active selection is
   replaced by the paste. **Terminal caveat, stated honestly:** scrollback is
   read-only — replacement only makes sense for text selected on the *input
   line*; pasting inserts at the cursor, so the user must have the original
   selected/deleted there. Document it; don't try to outsmart terminals.
4. **Safety valves:** keep the original text in memory with a tray action
   "Revert last rewrite" (re-paste the original); optional
   `review_before_paste` config that pops a small before/after diff with
   Accept/Cancel instead of replacing instantly.

**Provider interface** — this is where the user's cloud credentials come in:

```python
class RewriteProvider(Protocol):
    def rewrite(self, text: str, instruction: str) -> str: ...
```

Implementations, all thin: **Anthropic API** (a Haiku-class model is ideal —
fast, cheap, excellent at grammar), **AWS Bedrock** (uses the user's existing
AWS credentials), **Azure AI Foundry** (endpoint + key), and **local**
(Ollama/llama.cpp — a 3B int4 model fits beside Whisper in the 2 GB of spare
VRAM, with quality trade-offs). Provider + model + credentials live in a
`[rewrite]` config section and settings tab; timeouts and failures toast and
leave the original text untouched.

**Privacy note (mandatory):** unlike everything else in Scriba, rewrite mode
with a cloud provider sends the selected text off-machine. It is therefore
off until a provider is explicitly configured, the settings tab says which
provider receives the text, and rewrites are logged (locally) at INFO.
```
