"""DeLoHome bridge — lets Tonny operate the house.

Talks to a running DeLoHome over its MCP HTTP endpoint using plain httpx, rather than
importing it. That is not the first design: importing DeLoHome in-process was measured
and worked, but its dependency tree and this gateway's are irreconcilable —

    cartesia-line 0.2.17      -> websockets >=13,<14
    delohome -> fastmcp 2.14.4 -> websockets >=15.0.1

with no overlapping version. Going over HTTP costs nothing to fix it: measured against
the real service, `call_domain_tool(lights.list_lights)` takes 262 ms over HTTP and
262 ms in-process, because that time is the Hue bridge's own round trip, not ours. The
DeLoHome process stays warm, so its ~1.75 s of import-and-registry startup is paid once
by that service and never by an utterance. Shelling out to the `delohome` CLI would
have paid it on every single turn.

The side benefit is that the two projects no longer share a dependency tree at all, so
upgrading either cannot break the other.

ROOM IS CONVERSATION STATE, NOT DEVICE CONFIG. This satellite travels, so it has no
fixed room and the wire contract deliberately carries none — `Hello` stays strict and
unchanged. The speaker says where they are ("I'm in the office"), the model calls
`set_location`, and later commands default to it. With no location known and none
spoken, a tool ASKS: turning lights on in a room nobody is standing in is worse than
one clarifying question.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

import httpx
from line.llm_agent import ToolEnv, loopback_tool

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 1400
"""Cap on what one tool result hands back to the LLM.

Some DeLoHome reads are large — `list_lights` returns every room, bulb count and
brightness. Voice has no use for that volume, it costs latency and context, and the
model only needs enough to summarise. Oversized results are truncated with a visible
marker so the model knows it is not seeing everything.
"""

_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


class HomeUnavailable(RuntimeError):
    """DeLoHome could not be reached. The gateway still serves conversation."""


class HomeControl:
    """An MCP session against DeLoHome, plus the current spoken location."""

    def __init__(self, url: str = "http://127.0.0.1:8091/mcp", timeout: float = 20.0) -> None:
        self.url = url
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session: str | None = None
        self._schemas: dict[tuple[str, str], dict[str, Any]] = {}
        self._inventory = ""
        self._rooms: list[str] = []
        self._aliases: dict[str, str] = {}
        self._room: str | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._session is not None

    @property
    def location(self) -> str | None:
        return self._room

    # -- transport ---------------------------------------------------------

    async def _rpc(self, method: str, params: Any = None, *, notify: bool = False) -> Any:
        """One JSON-RPC call over MCP streamable-http.

        Two details this transport requires and that are easy to get wrong: the endpoint
        answers a POST with a 307, so redirects must be followed (and httpx replays the
        POST body, which urllib will not); and replies are framed as SSE `data:` lines
        even for a plain request/response call.
        """
        assert self._client is not None
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = 1
        if params is not None:
            body["params"] = params
        headers = dict(_HEADERS)
        if self._session:
            headers["mcp-session-id"] = self._session

        resp = await self._client.post(self.url, json=body, headers=headers)
        if resp.headers.get("mcp-session-id"):
            self._session = resp.headers["mcp-session-id"]
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(resp.text) if resp.text.strip() else None

    async def warm(self) -> bool:
        """Open the MCP session and cache the tool inventory. Call once, at startup.

        Returns False rather than raising when DeLoHome is unreachable: a house that is
        down should cost Tonny its tools, not its voice.
        """
        async with self._lock:
            if self.ready:
                return True
            try:
                self._client = httpx.AsyncClient(follow_redirects=True, timeout=self._timeout)
                await self._rpc(
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "tonny-voice", "version": "1"},
                    },
                )
                await self._rpc("notifications/initialized", {}, notify=True)
                await self._build_inventory()
            except Exception as exc:  # noqa: BLE001
                logger.warning("home_control_unavailable url=%s error=%s", self.url, type(exc).__name__)
                await self.aclose()
                return False
            logger.info("home_control_ready url=%s rooms=%d", self.url, len(self._rooms))
            return True

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._session = None

    async def _call_hub(self, name: str, arguments: dict[str, Any]) -> Any:
        reply = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not reply or "result" not in reply:
            raise HomeUnavailable(f"DeLoHome returned no result for {name}")
        result = reply["result"]
        if result.get("isError"):
            raise HomeUnavailable(_first_text(result) or f"{name} failed")
        text = _first_text(result)
        try:
            return json.loads(text) if text else None
        except (TypeError, json.JSONDecodeError):
            return text

    # -- inventory ---------------------------------------------------------

    async def _build_inventory(self) -> None:
        """Cache every domain, tool and schema so the model can skip discovery.

        This is what makes the dispatcher affordable. Without it the model spends two
        round trips (list_domains, then list_domain_tools) before it can act — on a
        pipeline already several seconds deep, that is the difference between "turn on
        the lights" working and the speaker giving up. Built from the live registry at
        startup, so it cannot drift from the tools that actually exist.
        """
        domains = await self._call_hub("list_domains", {}) or []
        lines: list[str] = []
        for dom in domains:
            name = dom.get("domain")
            if dom.get("status") != "ok":
                lines.append(f"- {name}: UNAVAILABLE ({str(dom.get('error', ''))[:80]})")
                continue
            tools = await self._call_hub("list_domain_tools", {"domain": name}) or []
            for spec in tools:
                self._schemas[(name, spec["name"])] = spec.get("input_schema") or {}
            lines.append(f"- {name}: " + ", ".join(t["name"] for t in tools))
        self._inventory = "\n".join(lines)

        rooms = await self._call_hub("list_rooms", {}) or []
        self._rooms = [r["room"] for r in rooms if isinstance(r, dict) and "room" in r]
        # Keep the aliases DeLoHome publishes, not just the keys. The household says
        # "the computer room" for the room keyed `office`, and matching keys alone
        # rejects it — reimplementing the matcher here would just be a second, worse
        # copy of the one DeLoHome already has.
        self._aliases = {}
        for r in rooms:
            if not isinstance(r, dict) or "room" not in r:
                continue
            keys = {r["room"], r.get("title", "")} | set(r.get("aliases") or [])
            for k in keys:
                if k:
                    self._aliases[_norm(k)] = r["room"]

    def prompt_fragment(self) -> str:
        """The house section appended to the system prompt."""
        if not self.ready:
            return ""
        return (
            "\n\nYou can operate this house with the `home_control` tool. "
            "Domains and their tools:\n"
            f"{self._inventory}\n"
            f"Rooms: {', '.join(self._rooms)}.\n"
            "Call home_control(domain, tool, arguments) directly — you already know what "
            "exists, so never ask for a list first. Most tools take a `room`; omit it and "
            "the speaker's current location is used. If you do not know where they are and "
            "they did not say, ask which room instead of guessing. When they mention where "
            "they are, call set_location. Report the outcome in one short spoken sentence."
        )

    # -- tools -------------------------------------------------------------

    def tools(self) -> list[Any]:
        if not self.ready:
            return []

        @loopback_tool
        async def set_location(
            ctx: ToolEnv,
            room: Annotated[str, "The room the speaker is in, e.g. 'office' or 'bedroom'"],
        ) -> str:
            """Remember which room the speaker is in. Call this whenever they say where
            they are — this satellite is portable, so its location changes."""
            return await self._set_location(room)

        @loopback_tool
        async def home_control(
            ctx: ToolEnv,
            domain: Annotated[str, "Which domain: lights, media, or displays"],
            tool: Annotated[str, "The tool name within that domain"],
            arguments: Annotated[
                dict | None,
                "Arguments object for the tool; omit room to use the speaker's location",
            ] = None,
        ) -> str:
            """Operate the house — lights, media playback, or TV panels."""
            return await self._dispatch(domain, tool, arguments or {})

        return [set_location, home_control]

    async def _set_location(self, room: str) -> str:
        """Resolve a spoken room against DeLoHome's own aliases.

        Speech arrives as "the computer room", "my office", or a whole sentence, and
        DeLoHome already publishes every alias the household uses. Exact alias first,
        then filler-stripped, then containment — mirroring its ladder without
        duplicating its judgement.
        """
        probe = _norm(room)
        hit = self._aliases.get(probe) or self._aliases.get(_strip_filler(probe))
        if not hit:
            stripped = _strip_filler(probe)
            for alias, key in self._aliases.items():
                if alias and (alias in probe or (len(stripped) >= 4 and stripped in alias)):
                    hit = key
                    break
        if not hit:
            return f"I don't know a room called {room}. I know: {', '.join(self._rooms)}."
        self._room = hit
        logger.info("home_location_set room=%s spoken=%r", hit, room)
        return f"Location set to {hit}."

    async def _dispatch(self, domain: str, tool: str, arguments: dict[str, Any]) -> str:
        if not self.ready:
            raise HomeUnavailable("DeLoHome is not connected.")

        schema = self._schemas.get((domain, tool), {})
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        if "room" in props and not arguments.get("room"):
            if self._room:
                arguments = {**arguments, "room": self._room}
            elif "room" in required:
                return (
                    "I don't know which room you're in. Ask which room they mean, then "
                    "call set_location before retrying."
                )

        try:
            result = await self._call_hub(
                "call_domain_tool", {"domain": domain, "tool": tool, "arguments": arguments}
            )
        except Exception as exc:  # noqa: BLE001
            # DeLoHome's errors are already written to be read aloud, so hand the text to
            # the model rather than raising — a failed action should be spoken, not turn
            # into a dropped turn.
            logger.info("home_control_failed domain=%s tool=%s err=%s", domain, tool, type(exc).__name__)
            return f"That didn't work: {exc}"
        return self._compact(result)

    @staticmethod
    def _compact(result: Any) -> str:
        # The hub wraps list returns as {"result": [...]}; unwrap so the model does not
        # have to reason about the envelope.
        if isinstance(result, dict) and set(result) == {"result"}:
            result = result["result"]
        text = result if isinstance(result, str) else json.dumps(result, default=str, separators=(",", ":"))
        if len(text) > MAX_RESULT_CHARS:
            return text[:MAX_RESULT_CHARS] + " …(truncated; ask about one room for detail)"
        return text


_FILLER = {"the", "a", "an", "in", "im", "i", "am", "at", "my", "room", "is", "are"}


def _norm(text: str) -> str:
    import re

    text = re.sub(r"[\u2018\u2019']", "", text.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _strip_filler(text: str) -> str:
    words = [w for w in text.split() if w not in _FILLER]
    return " ".join(words) or text


def _first_text(result: dict[str, Any]) -> str:
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    return ""
