"""Wire and process-lifecycle tests; no microphone, provider or real gateway needed."""

import asyncio
from dataclasses import replace
import io
import json
from pathlib import Path
import signal
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import satellite as sat
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


def wav_bytes(pcm=b"\x01\x00" * 320, rate=16_000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm)
    return output.getvalue()


class AudioValidationTests(unittest.TestCase):
    def test_input_wav_requires_pinned_format_and_complete_frames(self):
        for wav in (wav_bytes(rate=24_000), wav_bytes(channels=2), wav_bytes(b"\x00" * 641)):
            with self.subTest(length=len(wav)), self.assertRaises(sat.ClientError):
                sat.validate_wav(wav, capture=True)
        info = sat.validate_wav(wav_bytes(), capture=True)
        self.assertEqual(info.data_size, 640)

    def test_truncated_or_forged_wav_is_rejected(self):
        wav = wav_bytes()
        bad_alignment = bytearray(wav)
        struct.pack_into("<H", bad_alignment, 32, 4)
        bad_length = bytearray(wav)
        struct.pack_into("<I", bad_length, 40, 100_000)
        for value in (b"not wav", wav[:-2], wav + b"junk", bad_alignment, bad_length):
            with self.subTest(length=len(value)), self.assertRaises(sat.ClientError):
                sat.validate_wav(bytes(value))

    def test_capture_pcm_and_file_sizes_are_bounded(self):
        for pcm in (b"", b"\0", b"\0" * (sat.MAX_PCM_BYTES + 2)):
            with self.assertRaises(sat.ClientError):
                sat.validate_pcm(pcm)
        with self.assertRaises(sat.ClientError):
            sat.validate_wav(wav_bytes(b"\0" * (sat.MAX_PCM_BYTES + 2)), capture=True)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "oversized.wav"
            source.write_bytes(b"\0" * (sat.MAX_WAV_BYTES + 1))
            with self.assertRaisesRegex(sat.ClientError, "wav_too_large"):
                sat.load_input_wav(source)

    def test_rms_works_without_removed_audioop_module(self):
        self.assertEqual(sat.pcm_rms(struct.pack("<hh", 16_384, -16_384)), 0.5)
        self.assertEqual(sat.pcm_rms(b"\0\0"), 0)

    def test_untrusted_gateway_error_text_is_not_propagated(self):
        with self.assertRaisesRegex(sat.ClientError, "^gateway_error$"):
            sat.control(json.dumps({"type": "error", "message": "sensitive transcript"}))
        for value in ("[]", "null", "{", b"{}", json.dumps({"type": "x" * 4096})):
            with self.assertRaises(sat.ClientError):
                sat.control(value)

    def test_one_pending_trigger_and_no_overlap(self):
        gate = sat.TriggerGate()
        self.assertTrue(gate.trigger())
        self.assertFalse(gate.trigger())
        gate.pending.clear()
        gate.busy = True
        self.assertFalse(gate.trigger())
        self.assertFalse(gate.pending.is_set())
        gate.busy = False
        self.assertTrue(gate.trigger())


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = None
        self.received = []
        self.connections = 0

    async def asyncTearDown(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def start_server(self, handler):
        self.server = await serve(handler, "127.0.0.1", 0, compression=None, close_timeout=0.1)
        port = self.server.sockets[0].getsockname()[1]
        return sat.Options(url=f"ws://127.0.0.1:{port}/v1/voice", connect_timeout=1, response_timeout=1)

    async def read_turn(self, websocket):
        self.connections += 1
        hello = json.loads(await websocket.recv())
        self.received.append(hello)
        await websocket.send(json.dumps({"type": "ready", "session_id": hello["session_id"]}))
        while True:
            message = await websocket.recv()
            self.received.append(message)
            if isinstance(message, str):
                self.assertEqual(json.loads(message), {"type": "end_of_input"})
                return

    async def normal_response(self, websocket):
        await self.read_turn(websocket)
        wav = wav_bytes(rate=24_000)
        await websocket.send(json.dumps({"type": "response_start", "format": "wav"}))
        await websocket.send(wav[:31])
        await websocket.send(wav[31:])
        await websocket.send(json.dumps({"type": "response_end"}))
        # The gateway may close immediately upon completing its response.

    async def test_complete_wire_turn_keeps_pcm_exact_and_accepts_reply_rate(self):
        options = await self.start_server(self.normal_response)
        pcm = b"\x11\x00" * 2501
        result = await sat.exchange(options, pcm, "test-session")
        self.assertEqual(result, wav_bytes(rate=24_000))
        hello = self.received[0]
        self.assertEqual(hello["sample_rate"], 16_000)
        self.assertEqual(hello["encoding"], "pcm_s16le")
        self.assertEqual(hello["channels"], 1)
        self.assertEqual(hello["client"], "tonny")
        chunks = [value for value in self.received if isinstance(value, bytes)]
        self.assertEqual(b"".join(chunks), pcm)
        self.assertTrue(all(0 < len(chunk) <= 2560 and len(chunk) % 2 == 0 for chunk in chunks))

    async def test_input_wav_uses_wire_path_saves_reply_and_still_plays(self):
        options = await self.start_server(self.normal_response)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.wav"
            output = Path(directory) / "reply.wav"
            pcm = b"\x01\x00" * 160
            source.write_bytes(wav_bytes(pcm))
            options = replace(options, once=True, input_wav=source, output_wav=output)
            with patch.object(sat, "play_audio", new_callable=AsyncMock) as playback:
                await sat.run_turn(options)
            playback.assert_awaited_once_with(options, wav_bytes(rate=24_000))
            self.assertEqual(output.read_bytes(), wav_bytes(rate=24_000))
        self.assertEqual(b"".join(value for value in self.received if isinstance(value, bytes)), pcm)

    async def test_invalid_ready_closes_without_sending_audio(self):
        received = []
        async def wrong_session(websocket):
            received.append(await websocket.recv())
            await websocket.send(json.dumps({"type": "ready", "session_id": "wrong"}))
            async for message in websocket:
                received.append(message)
        options = await self.start_server(wrong_session)
        with self.assertRaisesRegex(sat.ClientError, "invalid_ready"):
            await sat.exchange(options, b"\0\0", "expected")
        self.assertEqual(len(received), 1)

    async def test_response_order_and_format_are_enforced(self):
        for messages in (
            [b"wav before start"],
            [json.dumps({"type": "response_end"})],
            [json.dumps({"type": "response_start", "format": "mp3"})],
            [json.dumps({"type": "response_start", "format": "wav"})] * 2,
            [json.dumps({"type": "response_start", "format": "wav"}), b"invalid", json.dumps({"type": "response_end"})],
        ):
            async def malformed(websocket):
                await self.read_turn(websocket)
                for message in messages:
                    await websocket.send(message)
            options = await self.start_server(malformed)
            with self.subTest(messages=messages), self.assertRaises(sat.ClientError):
                await sat.exchange(options, b"\0\0", "session")
            self.server.close()
            await self.server.wait_closed()

    async def test_response_accumulation_has_a_total_bound(self):
        async def oversized(websocket):
            await self.read_turn(websocket)
            await websocket.send(json.dumps({"type": "response_start", "format": "wav"}))
            await websocket.send(b"\0" * 80)
            await websocket.send(b"\0" * 80)
        options = await self.start_server(oversized)
        with patch.object(sat, "MAX_WAV_BYTES", 100):
            with self.assertRaisesRegex(sat.ClientError, "response_too_large"):
                await sat.exchange(options, b"\0\0", "session")

    async def test_stalled_response_times_out_and_closes(self):
        async def stalled(websocket):
            await self.read_turn(websocket)
            await websocket.wait_closed()
        options = await self.start_server(stalled)
        with self.assertRaises(TimeoutError):
            await sat.exchange(replace(options, response_timeout=0.03), b"\0\0", "session")

    async def test_disconnect_after_submission_does_not_replay(self):
        async def disconnect(websocket):
            await self.read_turn(websocket)
            await websocket.close()
        options = await self.start_server(disconnect)
        with self.assertRaises(ConnectionClosed):
            await sat.exchange(options, b"\0\0", "session")
        self.assertEqual(self.connections, 1)

    async def test_initial_failures_retry_with_original_pcm(self):
        options = await self.start_server(self.normal_response)
        original_connect = sat.connect
        original_sleep = asyncio.sleep
        calls = []
        sleeps = []
        async def flaky(*args, **kwargs):
            calls.append(args)
            if len(calls) <= 2:
                raise OSError("offline")
            return await original_connect(*args, **kwargs)
        async def fast_sleep(seconds):
            if seconds in (1, 2):
                sleeps.append(seconds)
                await original_sleep(0)
            else:
                # Patching asyncio.sleep also affects the WebSocket keepalive.
                await original_sleep(seconds)
        pcm = b"\x22\x00" * 160
        with patch.object(sat, "connect", flaky), patch.object(sat.asyncio, "sleep", fast_sleep):
            await sat.exchange(replace(options, connect_timeout=10), pcm, "session")
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(len(calls), 3)
        self.assertEqual(b"".join(value for value in self.received if isinstance(value, bytes)), pcm)

    async def test_initial_retry_budget_is_bounded(self):
        async def offline(*args, **kwargs):
            raise OSError("offline")
        started = time.monotonic()
        with patch.object(sat, "connect", offline):
            with self.assertRaisesRegex(sat.ClientError, "connect_timeout"):
                await sat.connect_ready(sat.Options(connect_timeout=0.02), "session")
        self.assertLess(time.monotonic() - started, 0.5)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.processes = []
        self.real_spawn = asyncio.create_subprocess_exec

    async def asyncTearDown(self):
        for process in self.processes:
            await sat.terminate_process(process)

    def fake_alsa(self, script, event=None):
        async def spawn(*args, **kwargs):
            process = await self.real_spawn(sys.executable, "-c", script, **kwargs)
            self.processes.append(process)
            if event:
                event.set()
            return process
        return spawn

    async def test_capture_is_exact_and_subprocess_is_reaped(self):
        script = "import sys; sys.stdout.buffer.write(b'\\x01\\x00' * 320)"
        with patch.object(sat.asyncio, "create_subprocess_exec", self.fake_alsa(script)):
            pcm = await sat.capture_audio(sat.Options(capture_seconds=0.02))
        self.assertEqual(pcm, b"\x01\x00" * 320)
        self.assertEqual(self.processes[0].returncode, 0)

    async def test_overflow_terminates_even_with_a_full_stdout_pipe(self):
        script = "import sys,time; sys.stdout.buffer.write(b'\\0' * 1000000); sys.stdout.flush(); time.sleep(60)"
        with patch.object(sat.asyncio, "create_subprocess_exec", self.fake_alsa(script)):
            with self.assertRaisesRegex(sat.ClientError, "capture_too_large"):
                await asyncio.wait_for(sat.capture_audio(sat.Options(capture_seconds=0.02)), 3)
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_cancelling_capture_and_playback_reaps_child(self):
        for operation in ("capture", "playback"):
            created = asyncio.Event()
            with patch.object(sat.asyncio, "create_subprocess_exec", self.fake_alsa("import time; time.sleep(60)", created)):
                action = sat.capture_audio(sat.Options()) if operation == "capture" else sat.play_audio(sat.Options(), wav_bytes())
                task = asyncio.create_task(action)
                await created.wait()
                await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            self.assertIsNotNone(self.processes[-1].returncode)

    async def test_unresponsive_child_is_killed_and_reaped(self):
        process = await self.real_spawn(
            sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
        )
        self.processes.append(process)
        await process.stdout.readline()
        await asyncio.wait_for(sat.terminate_process(process), 3)
        self.assertEqual(process.returncode, -signal.SIGKILL)

    async def test_invalid_wav_never_launches_playback(self):
        with patch.object(sat.asyncio, "create_subprocess_exec") as spawn:
            with self.assertRaises(sat.ClientError):
                await sat.play_audio(sat.Options(), b"not wav")
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
