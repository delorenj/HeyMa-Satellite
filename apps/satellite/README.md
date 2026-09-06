# Tonny push-to-talk client

Python 3.11+ and ALSA `arecord`/`aplay` are required. The Pi only captures and
plays audio; recognition, Pipecat and the agent run on the gateway.

```bash
python3 -m venv /tmp/tonny-client-venv
/tmp/tonny-client-venv/bin/pip install -r apps/satellite/requirements.txt
/tmp/tonny-client-venv/bin/python apps/satellite/satellite.py --once
```

Without `--once`, the client waits for `SIGUSR1`. Send that signal to the printed
PID to capture a six-second turn, submit it, and play the reply. Signals received
while a turn is pending or active are discarded. `SIGTERM` stops capture/playback
and reaps the child process. There is no wake word or barge-in in this skeleton.

The defaults are `ws://big-chungus.local:18778/v1/voice`, ALSA `capture` and
`playback`. Override them with `--url`, `--capture-device` and `--playback-device`.
`--capture-seconds` accepts up to 15 seconds. `--connect-timeout` bounds the
initial retry window; `--response-timeout` bounds the upload and complete reply.
`TONNY_GATEWAY_URL` can supply the URL instead of a command-line flag.

`--once --input-wav /tmp/question.wav` sends a known 16 kHz mono PCM16 WAV through
the same wire path and still plays the response. `--output-wav /tmp/reply.wav`
saves the validated reply for test evidence. Keep audio outside the source tree.

Input stays in RAM, limited to 15 seconds (480,000 bytes). Initial connection
failures retry with backoff for up to 60 seconds by default. A turn is never
automatically resubmitted after audio upload starts. Replies are limited to 4 MiB
and checked for valid RIFF/PCM headers and complete frames before playback.
Logs contain stages, sizes, timing and capture RMS, without audio or transcripts.

Run the hardware-free wire and process-cleanup tests with a Python environment
that has the pinned dependency installed:

```bash
python -m unittest discover -s apps/satellite/tests -v
```
