# Windows setup — prerequisites before development starts

One-time setup on the target machine (Windows 11, RTX 3050). Everything below
is deliberately minimal: **no CUDA Toolkit, no Visual Studio, no Node.js, no
separate Python installer** are needed. Run commands in a regular (not
elevated) PowerShell.

## 1. Install the three tools

```powershell
# Git for Windows (also provides the bash Claude Code uses for shell commands)
winget install --id Git.Git

# uv — package manager; it will download and manage Python 3.12 itself
winget install --id astral-sh.uv

# Claude Code (native Windows installer; requires Git for Windows from above)
irm https://claude.ai/install.ps1 | iex
```

Open a **new** PowerShell afterwards so PATH changes take effect.

## 2. Verify the GPU driver (already installed — just confirm)

```powershell
nvidia-smi
```

Expected: driver 537.x, "CUDA Version: 12.2", RTX 3050. That is sufficient —
faster-whisper's CUDA libraries (cuDNN, cuBLAS) arrive later as pip wheels
inside the project venv (see DESIGN §3). Do **not** install the CUDA Toolkit;
it is not needed and system-wide CUDA libraries are a known source of version
conflicts with the pip wheels.

## 3. Allow desktop apps to use the microphone

Settings → Privacy & security → Microphone:

- "Microphone access" → **On**
- "Let desktop apps access your microphone" → **On**

Without this, audio capture silently returns nothing — it looks like a code
bug but isn't. Check here first if no devices show input levels.

## 4. Clone and start

```powershell
git clone https://github.com/dirkpetersen/scriba.git
cd scriba
claude
```

Git for Windows bundles Git Credential Manager, which will pop a browser
window to authenticate with GitHub on the first push.

Then tell Claude Code to start milestone M0 (see `docs/PLAN.md`). Once M0
lands, the day-to-day commands are `uv sync`, `uv run scriba`,
`uv run pytest`, `uv run ruff check` — uv fetches Python 3.12 automatically
on first `uv sync`; no manual Python install.

## Troubleshooting notes (for the implementing session)

- **Every runtime dependency has prebuilt Windows wheels** (PySide6,
  sounddevice ships its own PortAudio DLL, rapidfuzz, pywin32, onnxruntime,
  ctranslate2/faster-whisper, nvidia-cudnn-cu12, nvidia-cublas-cu12). If
  `uv sync` ever tries to *compile* something, the dependency choice is wrong
  — stop and reconsider rather than installing build tools.
- **onnxruntime / PySide6 DLL import errors** on a fresh machine usually mean
  the MSVC runtime is missing (rare on updated Win11):
  `winget install Microsoft.VCRedist.2015+.x64`.
- **Do not run the terminal (or Scriba) elevated** during normal development —
  UIPI behavior differs for elevated processes and injection tests would lie
  to you (DESIGN §7.7).
- **Microsoft Store Python** — the machine has Python 3.13 installed from the
  Store. **Do not use it.** The project pins uv-managed Python 3.12
  (deliberately: the ML wheel ecosystem — CTranslate2, onnxruntime, PySide6 —
  trails the newest CPython, and 3.12 guarantees prebuilt wheels for every
  dependency). uv fetches its own 3.12 on first `uv sync`; always run things
  via `uv run ...`, never bare `python`. Leaving the Store install in place is
  harmless.
- **Poor recognition in loud environments (car, etc.) — check mic input
  level first.** On this machine the built-in mic array's system input
  volume was found maxed at 100% (+35 dB), leaving zero headroom before
  clipping in a noisy environment; clipped samples are corrupted before any
  software (VAD, denoising, Whisper) ever sees them, and no downstream
  processing can recover them. Check via Settings → Sound → Input → device
  properties → Volume, and back off to ~60–70% if maxed. This was found
  empirically after first ruling out a related theory: sounddevice/PortAudio
  cannot request any WASAPI stream category at all (confirmed via raw
  `IAudioClient2::SetClientProperties` COM calls) — an initial A/B test of
  `AudioCategory_Communications` and `AudioCategory_Speech` against identical
  synthetic noise showed no measurable difference, but that test's category
  property may simply not have taken effect (no independent way to verify it
  at the time). A later, WinRT-`AudioCaptureEffectsManager`-verified check
  confirmed `AudioCategory_Speech` genuinely does attach
  AEC/NoiseSuppression/AGC on this hardware that the default category never
  gets — see DESIGN.md §7.1's deviation note for the full story, including
  the mixed lab-test evidence for whether it actually helps in practice.
  `scriba/audio/wasapi_speech.py` now requests this category by default
  (`audio.wasapi_speech_category`, opt-out via config); the mic-gain finding
  above remains a separate, real, mundane fix in its own right regardless of
  that flag.
