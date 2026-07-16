# Scriba

> *In ancient Rome, the scriba was a public notary or clerk — an eager helper
> who takes down your words.*

**Local-first voice dictation for Windows.** Speak, and the text is typed into
whatever window has focus — your editor, a terminal, Claude Code. All
speech-to-text runs on your own GPU (Whisper via faster-whisper): no cloud
account, no per-minute billing, works offline.

## Why v2

The original Scriba streamed audio to AWS Transcribe; it worked, but metered
billing made it expensive and generic models kept misspelling technical terms.
v2 is a ground-up redesign:

- **Local Whisper (`large-v3-turbo`) on the GPU** — zero marginal cost, low
  latency, offline
- **Custom vocabulary** — bias recognition toward your terms (`kubectl`,
  `systemd`, ...) and auto-correct accent-driven mishearings
- **English + German**, including a mixed mode for code-switching speakers
- **Push-to-talk, toggle, or wake word** — the wake word enables hands-free
  "car mode"
- **Spoken commands** — "period", "comma", "new line" (and German equivalents)
- Proper tray app with settings UI, first-run wizard, and runtime model
  download with progress

## Status

**Design phase — no code yet.** The full architecture and implementation plan
live in:

- [docs/DESIGN.md](docs/DESIGN.md) — architecture, component specs, decisions
- [docs/PLAN.md](docs/PLAN.md) — milestones and acceptance criteria

The legacy AWS-based implementation is preserved on the
[`legacy`](../../tree/legacy) branch.

## License

MIT — see [LICENSE](LICENSE).
