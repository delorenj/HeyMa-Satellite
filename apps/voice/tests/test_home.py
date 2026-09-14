"""HomeControl — the DeLoHome bridge.

These run against a stubbed hub rather than a live house, so they stay honest about
behaviour (alias resolution, room defaulting, refusing to guess) without needing Hue to
be reachable. The transport itself was verified against the real service.
"""

from __future__ import annotations

import pytest

from tonny_voice.home import MAX_RESULT_CHARS, HomeControl, _norm, _strip_filler

ROOMS = [
    {"room": "office", "title": "Computer Room", "aliases": ["office", "computer room", "my office", "the computer"]},
    {"room": "bedroom", "title": "Master Bedroom", "aliases": ["bedroom", "master bedroom", "our room"]},
    {"room": "dommy", "title": "Dommy's Room", "aliases": ["dommy", "dommys room", "dominics room"]},
]

SCHEMAS = {
    ("lights", "set_lights"): {
        "properties": {"room": {"type": "string"}, "brightness": {"type": "integer"}},
        "required": ["room"],
    },
    ("lights", "list_lights"): {"properties": {}, "required": []},
    ("media", "now_playing"): {"properties": {"room": {"type": "string"}}, "required": []},
}


def make_home(**kw) -> HomeControl:
    """A HomeControl wired to a stub hub, warmed without touching the network."""
    h = HomeControl(**kw)
    h._session = "stub-session"           # marks it ready
    h._rooms = [r["room"] for r in ROOMS]
    h._aliases = {}
    for r in ROOMS:
        for k in {r["room"], r["title"], *r["aliases"]}:
            h._aliases[_norm(k)] = r["room"]
    h._schemas = dict(SCHEMAS)
    h._inventory = "- lights: set_lights, list_lights\n- media: now_playing"
    h.calls = []                           # type: ignore[attr-defined]

    async def _call_hub(name, arguments):
        h.calls.append((name, arguments))   # type: ignore[attr-defined]
        return {"ok": True, "echo": arguments}

    h._call_hub = _call_hub                # type: ignore[assignment]
    return h


# -- location ----------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("office", "office"),
        ("the computer room", "office"),
        ("Computer Room", "office"),
        ("my office", "office"),
        ("I'm in the computer room", "office"),
        ("the bedroom", "bedroom"),
        ("master bedroom", "bedroom"),
        ("dommys room", "dommy"),
    ],
)
async def test_location_resolves_the_way_people_speak(spoken, expected):
    """The owner said "I'm in the computer room or office" — both must work, and so must
    a whole sentence, because that is what speech-to-text hands over."""
    h = make_home()
    await h._set_location(spoken)
    assert h.location == expected


async def test_an_unknown_room_is_refused_with_the_real_list():
    h = make_home()
    msg = await h._set_location("the dungeon")
    assert h.location is None
    assert "office" in msg and "bedroom" in msg


# -- room defaulting ---------------------------------------------------------


async def test_current_location_fills_in_an_omitted_room():
    h = make_home()
    await h._set_location("the computer room")
    await h._dispatch("lights", "set_lights", {"brightness": 40})
    _, args = h.calls[-1]
    assert args["arguments"] == {"brightness": 40, "room": "office"}


async def test_an_explicit_room_beats_the_current_location():
    """Saying "turn on the bedroom lights" from the office must mean the bedroom."""
    h = make_home()
    await h._set_location("office")
    await h._dispatch("lights", "set_lights", {"room": "bedroom", "brightness": 40})
    _, args = h.calls[-1]
    assert args["arguments"]["room"] == "bedroom"


async def test_without_a_location_a_required_room_asks_rather_than_guesses():
    """Guessing puts lights on in a room nobody is standing in. Asking is cheaper."""
    h = make_home()
    out = await h._dispatch("lights", "set_lights", {"brightness": 40})
    assert "don't know which room" in out
    assert not h.calls, "it must not have called the house at all"


async def test_a_tool_with_no_room_param_is_unaffected():
    h = make_home()
    await h._dispatch("lights", "list_lights", {})
    _, args = h.calls[-1]
    assert "room" not in args["arguments"]


async def test_an_optional_room_is_left_absent_when_location_is_unknown():
    """now_playing's room is optional — it means 'everywhere', so do not block on it."""
    h = make_home()
    await h._dispatch("media", "now_playing", {})
    assert h.calls, "an optional room must not stop the call"
    _, args = h.calls[-1]
    assert "room" not in args["arguments"]


# -- degradation -------------------------------------------------------------


def test_an_unwarmed_bridge_costs_tools_not_the_voice():
    """A house that is down must not take the assistant with it."""
    h = HomeControl()
    assert h.ready is False
    assert h.tools() == []
    assert h.prompt_fragment() == ""


async def test_a_failing_call_is_spoken_not_raised():
    """A dropped turn is worse than "that didn't work" — the model should say something."""
    h = make_home()
    await h._set_location("office")

    async def boom(name, arguments):
        raise RuntimeError("the Hue bridge is unreachable")

    h._call_hub = boom  # type: ignore[assignment]
    out = await h._dispatch("lights", "set_lights", {"brightness": 40})
    assert "didn't work" in out and "unreachable" in out


# -- shaping results ---------------------------------------------------------


def test_the_hub_result_envelope_is_unwrapped():
    """The hub wraps lists as {"result": [...]}; the model should not have to know."""
    assert HomeControl._compact({"result": [{"room": "office"}]}).startswith("[")


def test_oversized_results_are_truncated_visibly():
    big = {"rooms": [{"name": f"room-{i}", "detail": "x" * 60} for i in range(200)]}
    out = HomeControl._compact(big)
    assert len(out) <= MAX_RESULT_CHARS + 80
    assert "truncated" in out


# -- prompt ------------------------------------------------------------------


def test_the_prompt_lists_real_tools_so_discovery_can_be_skipped():
    """The inventory is what makes the dispatcher affordable: without it the model burns
    two round trips before acting."""
    h = make_home()
    frag = h.prompt_fragment()
    assert "set_lights" in frag and "now_playing" in frag
    assert "never ask for a list first" in frag
    assert "office" in frag


def test_filler_stripping_keeps_the_meaningful_word():
    assert _strip_filler(_norm("I'm in the computer room")) == "computer"
    assert _strip_filler(_norm("my office")) == "office"
