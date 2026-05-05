# TonnyBox — Agent Guide

**Generated:** 2026-05-04 | **Commit:** ececc75 | **Branch:** main

Raspberry Pi Zero W hardware satellite for household voice capture in the HeyMa ecosystem. Acts as a capture + relay device — streams audio to HeyMa server via Wisconsin Protocol for processing. Zero local ML inference.

## Structure

```
./
├── rec.py                   # ReSpeaker mic test/recording script
├── find_enable_pin.sh       # GPIO pin discovery for ReSpeaker
├── chip-failure.md          # ReSpeaker HAT hardware failure analysis
├── mise.toml                # Mise task runner (fetch-deps)
├── .plane.json              # Plane ticket board (workspace: 33god, proj: TONNY)
├── hey_tonny_training/      # Wake word training guide + sample scripts
├── custom_wakewords/        # Deployed .tflite wake word models
├── deps/                    # Git submodules (wyoming-satellite, openwakeword, seeed-voicecard)
├── _bmad/                   # BMAD methodology framework
├── .opencode/ .claude/ .gemini/ .agents/ .github/  # Agent/CI config
└── docs/                    # Project documentation (empty)
```

## Where to Look

| Task | Location | Notes |
|------|----------|-------|
| Audio recording on the Pi | `rec.py` | sounddevice-based, 16kHz mono, targets ReSpeaker |
| GPIO pin mapping | `find_enable_pin.sh` | Finds ReSpeaker enable pin |
| Wake word training | `hey_tonny_training/TRAINING_GUIDE.txt` | Google Colab workflow |
| Dependency source code | `deps/wyoming-satellite/` | Wyoming protocol satellite |
| Wake word detection | `deps/wyoming-openwakeword/` | openWakeWord integration |
| Audio driver overlay | `deps/seeed-voicecard/` dtoverlays | ReSpeaker kernel support |
| Ticket tracking | `.plane.json` → `https://plane.delo.sh/33god/` | Project ID: TONNY |
| BMAD methodology | `_bmad/` | `/bmalph` to navigate phases |

## Tech Stack

- **Hardware:** Raspberry Pi Zero W (512MB RAM)
- **Audio:** ReSpeaker 2-Mic HAT v2.0 (TLV320AIC3104 codec) or USB mic
- **Protocol:** Wisconsin Protocol for audio relay to HeyMa
- **Wake Word:** openWakeWord (`.tflite` models, "hey tonny")
- **Task Runner:** mise (`.mise.toml`)
- **Ticketing:** Plane (board: 33god)
- **Methodology:** BMAD

## Key Dependencies (Git Submodules)

| Submodule | Purpose | Update |
|-----------|---------|--------|
| `deps/wyoming-satellite` | Audio streaming satellite | `mise run fetch-deps` |
| `deps/wyoming-openwakeword` | Wake word detection | `mise run fetch-deps` |
| `deps/seeed-voicecard` | ReSpeaker audio drivers | `mise run fetch-deps` |
| `deps/seeed-linux-dtoverlays` | Device tree overlays | `mise run fetch-deps` |

## Known Hardware Issue

ReSpeaker 2-Mic HAT v2.0 experienced codec failure (24.576 MHz crystal oscillator). Documented in `chip-failure.md`. Fallback: USB microphone. Replacement options listed in the failure analysis.

## Conventions

- Target RPi Zero constraints — keep resource usage minimal
- Wisconsin Protocol for all voice relay
- Network must be assumed unreliable — implement reconnection + local buffering
- Audio format validation required before relay
- Ticket-before-code: move Plane ticket to "In Progress" before any code change
- Branch names must include ticket reference; commits must reference tickets

## Anti-Patterns

- **NEVER** run ML inference on the Pi Zero
- **NEVER** assume stable network — always implement reconnection logic
- **NEVER** skip audio format validation before relay
- **NEVER** commit without Plane ticket reference (emergency: `ALLOW_NO_TICKET=1`)

## Commands

```bash
mise trust                # Load environment
mise tasks                # List available tasks
mise run fetch-deps       # Init/update git submodules
```
