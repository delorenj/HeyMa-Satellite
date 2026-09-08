# Run Tonny without the Pi

The local stack runs the same voice gateway and WebSocket protocol as Tonny's
hardware deployment. Your browser supplies the microphone and speakers. An
on-demand satellite container runs the actual Python client against WAV files.

You need Docker Engine with Compose on Linux, or Docker Desktop on macOS. Both
AMD64 and ARM64 images build natively. The mise shortcuts also need Python 3.10+
on the host. No ALSA devices, Pi, SSH connection, home-network DNS, or host Python
voice dependencies are needed.

## Start and talk

```sh
mise run tonny:local:up
```

Open **http://localhost:18779**. Click **Record**, speak, and either click
**Stop and send** or let the six-second timer finish. Tonny plays the reply;
the player and **Play reply** button remain available if autoplay is blocked.
You can download both the submitted WAV and reply.

The default is **Offline · audio echo**. It returns your recorded audio with
exactly the same PCM samples. It does not transcribe, generate an answer, call
providers, or retain conversation history. `/healthz` reports `mode: loopback`
and zero STT/LLM/TTS activity. After the initial image build, this path works
without internet access or API keys. Browser assets are bundled in the image.

For a real conversation:

```sh
mise run tonny:local:up -- --live
```

This uses Deepgram STT, Cartesia Line with OpenRouter, and Cartesia TTS, matching
the existing gateway defaults. The launcher resolves the references in
`deploy/tonny/voice.env.op` through 1Password CLI. Sign in to `op` first, or supply
all three `TONNY_DEEPGRAM_API_KEY`, `TONNY_CARTESIA_API_KEY`, and
`TONNY_LLM_API_KEY` values through your process environment. Credentials never
belong in a Dockerfile, build argument, checked-in config, or plaintext env file.
The 1Password service token is not passed into containers.

If 1Password reports a rate limit, the launcher exits before replacing the
running stack. Retry later or use the supported process-environment credentials.

The page reports the gateway's actual mode. Live mode sends audio/text to the
configured providers; loopback performs only local audio transport. Live
conversation history is held in gateway memory and can be cleared with
**Reset conversation**. Run `tonny:local:up` again to return to offline mode.

Microphone permission works on `localhost`. A browser visiting an ordinary HTTP
LAN hostname may disable its microphone API; use localhost on the development
machine, or forward the port to localhost when working through SSH.

## Test WAV files

The browser accepts PCM16 WAV files with one channel at 16,000 Hz, up to 15
seconds. The same constraints apply to the Python satellite:

```sh
mise run tonny:local:wav -- --input /absolute/path/request.wav --output /absolute/path/reply.wav
```

This executes the satellite with `--once --input-wav --output-wav --no-playback`.
It uses the running gateway's current mode, saves a validated response, and
exits nonzero on failure. It never starts `arecord` or `aplay`. Output goes to
the path you supply and is owned by your host user. Keep recordings and other
runtime artifacts outside the source checkout.

## Edit and inspect

Python source changes reload the gateway automatically. Refresh the page after
changing HTML, CSS, or JavaScript. The satellite reads current source on its next
WAV invocation. Dependency changes require another `tonny:local:up` build.
Container virtual environments stay separate from the host checkout.

A reload clears live conversation history and can interrupt an active turn.
The browser reports interrupted uploads/responses and lets you submit a new
turn. It retries initial connections for up to 60 seconds, keeping captured
audio in memory; after submission begins it never automatically replays audio.

```sh
mise run tonny:local:status
mise run tonny:local:logs
mise run tonny:local:logs -- --follow
mise run tonny:local:down
```

The Compose project is `tonny-local`; the gateway binds only
`127.0.0.1:18779`. Set `TONNY_LOCAL_PORT` before `up` to choose another port.
Commands discover the running container's published port afterward. Containers
use `ws://gateway:18778/v1/voice` internally. These tasks do not manage systemd,
connect to the Pi, or replace the deployed gateway on port 18778.

## Run the acceptance checks

```sh
mise run tonny:local:check
```

The check builds an isolated project with a temporary source copy and a random
localhost port. It verifies offline image execution with networking disabled,
exact PCM round trips through the satellite container, the Python unit suites,
Chromium microphone/WAV/error flows, and an actual Uvicorn source reload followed
by another successful turn. Containers and temporary recordings are removed at
the end. Your development stack keeps running.

Chromium uses a generated microphone fixture inside the test container; physical
mic intelligibility, speaker audibility, HAT drivers, and GPIO still need hardware
acceptance. A live provider turn separately proves the STT/LLM/TTS integration.

Verified on 2026-09-08: the combined container check passed all 97 tests
(54 gateway, 19 Chromium, 24 satellite), exact PCM echo with no network/provider
access, and source reload followed by another complete turn. AMD64 and ARM64
images built; the ARM64 satellite and gateway completed an exact PCM round trip
under QEMU. Native macOS/Docker Desktop execution has not been observed here.
A live browser WAV turn completed STT, Line, TTS, playback, download, and reset;
the 24 kHz reply was 1.81 seconds long and arrived in about 5.9 seconds. Fresh
1Password reads were rate-limited during that check, so it used already-resolved
credentials from the existing Tonny process, passed directly through memory.

## Docker Compose directly

These commands work without mise or a host voice environment:

```sh
docker compose --env-file /dev/null -f compose.local.yaml up -d --build --wait gateway
docker compose --env-file /dev/null -f compose.local.yaml build satellite
docker compose --env-file /dev/null -f compose.local.yaml logs --tail 100 gateway
docker compose --env-file /dev/null -f compose.local.yaml down
```

For live providers, resolve references into the launch environment:

```sh
TONNY_MODE=live op run --env-file deploy/tonny/voice.env.op -- \
  docker compose --env-file /dev/null -f compose.local.yaml up -d --build --wait gateway
```

The launcher deliberately ignores the repo's general `.env`; only the explicit
voice variables and nonsecret `voice.conf` settings enter these containers.
`TONNY_MODE` defaults to `live` in the application itself, preserving existing
hardware deployments. Compose alone chooses the local `loopback` default.
