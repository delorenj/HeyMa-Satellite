"""Chromium acceptance against the real local loopback gateway.

Run with the browser dependency group and TONNY_TEST_URL. Ordinary gateway
pytest runs skip this module before importing Playwright or launching a browser.
All audio artifacts are generated in pytest's temporary directory.
"""

import io
import json
import math
import os
import struct
import wave
from pathlib import Path
from urllib.parse import urlsplit

import pytest

if not os.environ.get("TONNY_TEST_URL"):
    pytest.skip("Set TONNY_TEST_URL to run Chromium acceptance", allow_module_level=True)

BASE_URL = os.environ["TONNY_TEST_URL"].rstrip("/")
ORIGIN = "{0.scheme}://{0.netloc}".format(urlsplit(BASE_URL))

MEDIA_OBSERVER = """
window.__mediaProbe = {contexts: [], tracks: []};
const NativeAudioContext = window.AudioContext;
window.AudioContext = new Proxy(NativeAudioContext, {
  construct(Target, args) {
    const options = window.__captureSampleRate
      ? [{...args[0], sampleRate: window.__captureSampleRate}]
      : args;
    const context = Reflect.construct(Target, options);
    window.__mediaProbe.contexts.push(context);
    return context;
  }
});
const nativeGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async (...args) => {
  const stream = await nativeGetUserMedia(...args);
  window.__mediaProbe.tracks.push(...stream.getTracks());
  return stream;
};
"""


def wav_bytes(*, rate=16000, channels=1, seconds=1.0):
    samples = [
        round(12000 * math.sin(2 * math.pi * 440 * index / rate))
        for index in range(int(rate * seconds))
    ]
    pcm = b"".join(struct.pack("<h", value) * channels for value in samples)
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return output.getvalue()


def read_wav(value):
    with wave.open(io.BytesIO(value), "rb") as audio:
        shape = (audio.getframerate(), audio.getnchannels(), audio.getsampwidth())
        return shape, audio.readframes(audio.getnframes())


@pytest.fixture(scope="module")
def chromium(tmp_path_factory):
    # Deliberately deferred: normal development installations need no browser group.
    from playwright.sync_api import sync_playwright

    fixture = tmp_path_factory.mktemp("tonny-microphone") / "fake-mic-48k.wav"
    fixture.write_bytes(wav_bytes(rate=48000, seconds=12))
    with sync_playwright() as playwright:
        request = playwright.request.new_context(base_url=BASE_URL)
        health = request.get("/healthz").json()
        request.dispose()
        assert health["mode"] == "loopback", "Browser acceptance must target offline loopback"
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("TONNY_CHROMIUM", "/usr/bin/chromium"),
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={fixture}",
            ],
        )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page_factory(chromium):
    contexts = []

    def make(*, microphone=False, capture_rate=None, scripts=()):
        context = chromium.new_context(
            base_url=BASE_URL, accept_downloads=True, viewport={"width": 1280, "height": 960}
        )
        contexts.append(context)
        if microphone:
            context.grant_permissions(["microphone"], origin=ORIGIN)
        # Playwright does not guarantee ordering between separate init scripts.
        # Install the observer before wrappers that deliberately delay permission.
        context.add_init_script(
            f"window.__captureSampleRate = {json.dumps(capture_rate)};\n"
            + MEDIA_OBSERVER
            + "\n".join(scripts)
        )
        page = context.new_page()
        page.set_default_timeout(10000)
        return page

    yield make
    for context in reversed(contexts):
        context.close()


@pytest.fixture
def page(page_factory):
    page = page_factory()
    open_lab(page)
    return page


@pytest.fixture
def valid_wav():
    return wav_bytes()


def open_lab(page):
    page.goto("/")
    page.wait_for_function(
        "document.querySelector('[data-testid=mode]').dataset.mode === 'loopback'"
    )


def upload(page, data):
    page.get_by_test_id("wav-upload").set_input_files(
        {"name": "probe.wav", "mimeType": "audio/wav", "buffer": data}
    )


def wait_reply(page):
    page.get_by_test_id("download-reply").wait_for(state="visible")
    page.wait_for_function("['ready', 'playback'].includes(document.body.dataset.phase)")


def error_code(page):
    page.get_by_test_id("error").wait_for(state="visible")
    return page.get_by_test_id("error").get_attribute("data-code")


def download(page, test_id, destination):
    with page.expect_download() as pending:
        page.get_by_test_id(test_id).click()
    pending.value.save_as(destination)
    return destination.read_bytes()


def assert_capture_released(page, *, expect_track=True):
    page.wait_for_function(
        "window.__mediaProbe.contexts.every(context => context.state === 'closed')"
    )
    if expect_track:
        assert page.evaluate("window.__mediaProbe.tracks.length") > 0
    page.wait_for_function(
        "window.__mediaProbe.tracks.every(track => track.readyState === 'ended')"
    )


def wait_gateway_idle(page):
    page.wait_for_function(
        "async () => !(await fetch('/healthz').then(response => response.json())).active_session"
    )


def test_real_wav_upload_download_playback_and_reset(page, valid_wav, tmp_path):
    before = page.request.get("/healthz").json()["evidence_since_start"]
    upload(page, valid_wav)
    wait_reply(page)
    submitted = download(page, "download-request", tmp_path / "submitted.wav")
    response = download(page, "download-reply", tmp_path / "reply.wav")
    assert read_wav(submitted) == read_wav(valid_wav)
    assert read_wav(response) == read_wav(valid_wav)
    page.get_by_test_id("reply-audio").evaluate(
        "audio => { audio.pause(); audio.currentTime = 0; }"
    )
    page.get_by_test_id("play-reply").click()
    page.wait_for_function("document.querySelector('[data-testid=reply-audio]').currentTime > 0")
    after = page.request.get("/healthz").json()["evidence_since_start"]
    assert after["responses_sent"] == before["responses_sent"] + 1
    assert after["sessions"] == before["sessions"] + 1
    assert [after[name] for name in ("stt_turns", "llm_turns", "tts_turns")] == [0, 0, 0]
    screenshot = os.environ.get("TONNY_SCREENSHOT_PATH")
    if screenshot:
        destination = Path(screenshot)
        destination.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(destination), full_page=True)
    with page.expect_response(
        lambda response: response.url.endswith("/v1/reset") and response.request.method == "POST"
    ) as reset:
        page.get_by_test_id("reset").click()
    assert reset.value.status == 200
    assert reset.value.json()["conversation_turns"] == 0
    page.wait_for_function("document.body.dataset.phase === 'idle'")


@pytest.mark.parametrize("capture_rate", [44100, 48000])
def test_real_microphone_resamples_to_16k_and_echoes_exact_pcm(
    page_factory, tmp_path, capture_rate
):
    # A fake 48 kHz microphone file does not choose the browser's output rate.
    # Use real AudioContexts at both common rates to cover actual resampling.
    page = page_factory(microphone=True, capture_rate=capture_rate)
    open_lab(page)
    page.get_by_test_id("duration").select_option("3")
    page.get_by_test_id("record").click()
    page.wait_for_function("document.body.dataset.phase === 'recording'")
    wait_reply(page)
    request = download(page, "download-request", tmp_path / "microphone.wav")
    reply = download(page, "download-reply", tmp_path / "microphone-reply.wav")
    shape, pcm = read_wav(request)
    assert shape == (16000, 1, 2)
    assert len(pcm) == 3 * 16000 * 2
    assert max(abs(value[0]) for value in struct.iter_unpack("<h", pcm)) > 100
    assert read_wav(reply) == (shape, pcm)
    assert page.evaluate("window.__mediaProbe.contexts.map(context => context.sampleRate)") == [
        capture_rate
    ]
    assert_capture_released(page)


def test_stop_and_send_flushes_a_short_recording(page_factory, tmp_path):
    page = page_factory(microphone=True, capture_rate=48000)
    open_lab(page)
    page.get_by_test_id("duration").select_option("6")
    page.get_by_test_id("record").click()
    page.wait_for_function("document.body.dataset.phase === 'recording'")
    # Let the real AudioWorklet capture audio before exercising its explicit flush.
    page.wait_for_timeout(500)
    page.get_by_test_id("stop-send").click()
    wait_reply(page)
    request = download(page, "download-request", tmp_path / "stopped-request.wav")
    reply = download(page, "download-reply", tmp_path / "stopped-reply.wav")
    shape, pcm = read_wav(request)
    assert shape == (16000, 1, 2)
    assert 0 < len(pcm) < 6 * 16000 * 2
    assert read_wav(reply) == (shape, pcm)
    assert_capture_released(page)


@pytest.mark.parametrize("kind", ["bad_header", "48k", "stereo", "too_long", "truncated"])
def test_invalid_upload_never_opens_a_session(page, kind):
    data = {
        "bad_header": b"not a WAV",
        "48k": wav_bytes(rate=48000),
        "stereo": wav_bytes(channels=2),
        "too_long": wav_bytes(seconds=16),
        "truncated": wav_bytes()[:-2],
    }[kind]
    sockets = []
    page.on("websocket", lambda socket: sockets.append(socket))
    before = page.request.get("/healthz").json()["evidence_since_start"]["sessions"]
    upload(page, data)
    page.wait_for_function("document.body.dataset.phase === 'error'")
    assert error_code(page)
    assert not sockets
    assert not page.get_by_test_id("download-request").is_visible()
    assert page.request.get("/healthz").json()["evidence_since_start"]["sessions"] == before


def test_microphone_denial_is_recoverable_with_wav_upload(page_factory, valid_wav):
    page = page_factory()
    # Chromium's permission allowlist rejects all permissions not listed; unlike
    # --use-fake-ui-for-media-stream this permits a real NotAllowedError path.
    page.context.grant_permissions([], origin=ORIGIN)
    open_lab(page)
    assert (
        page.evaluate("async () => (await navigator.permissions.query({name: 'microphone'})).state")
        == "denied"
    )
    page.get_by_test_id("record").click()
    assert error_code(page) == "NotAllowedError"
    assert_capture_released(page, expect_track=False)
    upload(page, valid_wav)
    wait_reply(page)


def test_cancel_recording_releases_tracks_without_submitting(page_factory):
    page = page_factory(microphone=True)
    open_lab(page)
    sockets = []
    page.on("websocket", lambda socket: sockets.append(socket))
    page.get_by_test_id("record").click()
    page.wait_for_function("document.body.dataset.phase === 'recording'")
    page.get_by_test_id("cancel").click()
    page.wait_for_function("document.body.dataset.phase === 'idle'")
    assert_capture_released(page)
    assert not sockets
    assert page.get_by_test_id("record").is_enabled()
    assert not page.get_by_test_id("download-request").is_visible()


def test_cancel_before_late_permission_resolution_stops_the_late_stream(page_factory):
    page = page_factory(
        microphone=True,
        scripts=[
            """
const getStream = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async (...args) => {
  const stream = await getStream(...args);
  return await new Promise(resolve => { window.__releasePermission = () => resolve(stream); });
};
"""
        ],
    )
    open_lab(page)
    sockets = []
    page.on("websocket", lambda socket: sockets.append(socket))
    page.get_by_test_id("record").click()
    page.wait_for_function("typeof window.__releasePermission === 'function'")
    page.get_by_test_id("cancel").click()
    page.evaluate("window.__releasePermission()")
    assert_capture_released(page)
    assert not sockets
    assert page.locator("body").get_attribute("data-phase") == "idle"


def test_real_gateway_busy_error_is_structured_and_recoverable(page, valid_wav):
    page.evaluate("""async () => {
      const url = new URL('/v1/voice', location.href); url.protocol = 'ws:';
      const socket = new WebSocket(url); window.__heldSocket = socket;
      await new Promise((resolve, reject) => {
        socket.onerror = reject;
        socket.onopen = () => socket.send(JSON.stringify({
          type: 'hello', session_id: crypto.randomUUID(), sample_rate: 16000,
          encoding: 'pcm_s16le', channels: 1
        }));
        socket.onmessage = event => JSON.parse(event.data).type === 'ready' ? resolve() : reject();
      });
    }""")
    try:
        upload(page, valid_wav)
        assert error_code(page) == "busy"
        assert "another turn" in page.get_by_test_id("error").inner_text()
    finally:
        page.evaluate("window.__heldSocket.close()")
    wait_gateway_idle(page)
    upload(page, valid_wav)
    wait_reply(page)


@pytest.mark.parametrize("kind", ["binary_before_start", "bad_json", "bad_wav", "oversized"])
def test_malformed_or_oversized_reply_never_becomes_playable(page_factory, valid_wav, kind):
    page = page_factory()
    intercepted = []

    def route(ws):
        intercepted.append(ws)

        def message(value):
            if isinstance(value, bytes):
                return
            control = json.loads(value)
            if control["type"] == "hello":
                ws.send(json.dumps({"type": "ready", "session_id": control["session_id"]}))
            elif control["type"] == "end_of_input":
                if kind == "binary_before_start":
                    ws.send(b"wrong state")
                elif kind == "bad_json":
                    ws.send("{")
                else:
                    ws.send(json.dumps({"type": "response_start", "format": "wav", "final": True}))
                    ws.send(b"\0" * (4 * 1024 * 1024 + 2) if kind == "oversized" else b"bad wav")
                    if kind == "bad_wav":
                        ws.send(json.dumps({"type": "response_end"}))

        ws.on_message(message)

    page.route_web_socket("**/v1/voice", route)
    open_lab(page)
    upload(page, valid_wav)
    expected = "response_too_large" if kind == "oversized" else "invalid_response"
    assert error_code(page) == expected
    assert len(intercepted) == 1
    assert not page.get_by_test_id("download-reply").is_visible()
    assert not page.get_by_test_id("reply-audio").get_attribute("src")
    assert page.get_by_test_id("record").is_enabled()


def test_disconnect_after_upload_is_not_replayed_and_next_turn_uses_real_gateway(
    page_factory, valid_wav
):
    page = page_factory()
    opened = []
    submitted = bytearray()

    def route(ws):
        opened.append(ws)
        if len(opened) > 1:
            ws.connect_to_server()
            return

        def message(value):
            if isinstance(value, bytes):
                submitted.extend(value)
                return
            control = json.loads(value)
            if control["type"] == "hello":
                ws.send(json.dumps({"type": "ready", "session_id": control["session_id"]}))
            elif control["type"] == "end_of_input":
                ws.close(code=1012, reason="controlled gateway restart")

        ws.on_message(message)

    page.route_web_socket("**/v1/voice", route)
    open_lab(page)
    upload(page, valid_wav)
    assert error_code(page) == "connection_lost"
    assert submitted == read_wav(valid_wav)[1]
    # The first retry delay is one second. Observe past that point to prove
    # already-submitted audio is not silently sent a second time.
    page.wait_for_timeout(1300)
    assert len(opened) == 1
    upload(page, valid_wav)
    wait_reply(page)
    assert len(opened) == 2


def test_blocked_autoplay_keeps_manual_play_and_download_usable(page_factory, valid_wav):
    page = page_factory(
        scripts=[
            """
const nativePlay = HTMLMediaElement.prototype.play;
let attempts = 0;
HTMLMediaElement.prototype.play = function (...args) {
  if (attempts++ === 0) {
    return Promise.reject(new DOMException('Controlled autoplay block', 'NotAllowedError'));
  }
  return nativePlay.apply(this, args);
};
"""
        ]
    )
    open_lab(page)
    upload(page, valid_wav)
    wait_reply(page)
    assert error_code(page) == "playback_blocked"
    assert page.get_by_test_id("play-reply").is_enabled()
    assert page.get_by_test_id("download-reply").is_visible()
    page.get_by_test_id("play-reply").click()
    page.wait_for_function("document.querySelector('[data-testid=reply-audio]').currentTime > 0")
    assert not page.get_by_test_id("error").is_visible()
