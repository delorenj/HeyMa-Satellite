# TonnyBox — Agent Guide

Raspberry Pi Zero hardware satellite for household voice capture in the HeyMa ecosystem.

## Tech Stack

- **Hardware:** Raspberry Pi Zero W
- **Protocol:** Wisconsin Protocol (custom voice relay)
- **Parent:** HeyMa voice interface
- **Companion Service:** `services/tonny/`

## Context

- Hardware-constrained environment (RPi Zero)
- Streams audio to HeyMa server for processing
- Minimal local computation — acts as capture + relay device

## Conventions

- Keep resource usage minimal (RPi Zero has 512MB RAM)
- Use Wisconsin Protocol for audio relay
- Fail gracefully on network disconnection — buffer locally if needed

## Anti-Patterns

- Never run ML inference on the Pi Zero
- Never assume stable network — implement reconnection logic
- Never skip audio format validation before relay
