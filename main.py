"""
MyDrive Gemini Live API — Unified Chat Backend
===============================================

Architecture
────────────
  ONE Gemini session per chat (session_id), ONE WebSocket connection per session.
  The Gemini session is always opened in AUDIO modality so we always receive:
    - output audio PCM (24 kHz, 16-bit PCM)
    - output_audio_transcription  → always sent to client as "assistant_text"
    - input_audio_transcription   → sent to client as "user_transcript" (voice turns only)
    - tool_call / tool_response   → always handled regardless of speaker mode

  speaker_mode is a server-side flag on the SessionState — toggling it never
  restarts the Gemini session. When OFF the PCM bytes are discarded silently;
  the output_audio_transcription still arrives and is forwarded as text.

Single endpoint
───────────────
  /ws/chat?session_id=<uuid>

  Client → Server (JSON)
    {"type": "text_input",  "text": "..."}
    {"type": "voice_start"}                  # followed by raw binary PCM frames
    {"type": "voice_end"}                    # flushes accumulated audio
    {"type": "set_speaker", "enabled": bool} # toggle speaker mode on the fly
    {"type": "interrupt"}                    # clear in-flight audio buffer
  Client → Server (binary)
    raw 16-bit PCM chunks at 16 kHz (only while voice_start is active)

  Server → Client (JSON)
    {"type": "session_ready",        "session_id": "...", "speaker_mode": false}
    {"type": "user_transcript",      "text": "..."}
    {"type": "assistant_text",       "text": "..."}
    {"type": "tool_call",            "tool": "...", "args": {...}, "result": {...}}
    {"type": "turn_complete"}
    {"type": "speaker_mode_updated", "enabled": bool}
    {"type": "error",                "message": "..."}
    {"type": "session_ended"}
  Server → Client (binary)
    raw 16-bit PCM chunks at 24 kHz — ONLY when speaker_mode is True
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

# ──────────────────────────────────────────────────────────────
# Bootstrap
# ──────────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("mydrive")

# ──────────────────────────────────────────────────────────────
# Configuration  (all tunables in one place)
# ──────────────────────────────────────────────────────────────

GEMINI_API_KEY    = os.environ["GEMINI_KEY"]          # hard-fail if missing
GEMINI_MODEL      = "gemini-2.5-flash-native-audio-preview-12-2025"

# How long an idle session lives before it is torn down (seconds)
SESSION_IDLE_TTL  = int(os.environ.get("SESSION_IDLE_TTL",  "300"))
# How often the cleanup task wakes up (seconds)
CLEANUP_INTERVAL  = int(os.environ.get("CLEANUP_INTERVAL",  "60"))
# Inbox queue capacity (number of InputFrames)
INBOX_MAX_SIZE    = int(os.environ.get("INBOX_MAX_SIZE",    "32"))
# Outbox queue capacity (number of outgoing frames)
OUTBOX_MAX_SIZE   = int(os.environ.get("OUTBOX_MAX_SIZE",   "256"))
# Max raw audio bytes buffered per voice turn (~10 s at 16 kHz 16-bit mono)
AUDIO_BUFFER_MAX  = int(os.environ.get("AUDIO_BUFFER_MAX",  str(10 * 16_000 * 2)))

# ──────────────────────────────────────────────────────────────
# Gemini configuration
# ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a helpful AI voice assistant for 'MyDrive', an automobile service platform.
Your job is to have a natural conversation with the user to understand their vehicle-related
issue, then trigger the correct service action once their intent is clear.

You have access to four service tools:
- request_roadside_assistance: For flat tyres, dead batteries, fuel delivery, locked-out
  vehicles, or other minor roadside help.
- request_tow_truck: For accidents, non-starting engines, major mechanical failures,
  overheating, or smoke coming from the vehicle.
- search_spare_parts: For users looking to find or order specific car parts (glass, mirrors,
  tyres, engine parts, filters, etc.).
- book_garage_service: For routine maintenance, unusual sounds or smells, warning lights, or
  scheduling an inspection or service appointment.

RULES:
- Always respond in a warm, conversational, spoken style — your response will be read aloud.
- Ask ONE focused follow-up question at a time if the user's intent is unclear.
- Once the intent is unambiguous, call the appropriate tool immediately. Do NOT ask for
  confirmation.
- If the user says something unrelated to vehicle services, politely explain that you can only
  help with MyDrive services.
- Keep responses concise. This is a voice interface; avoid long paragraphs.
"""

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "request_roadside_assistance",
        "description": (
            "Dispatches a roadside assistance unit. Use for flat tyres, dead batteries, "
            "fuel delivery, locked-out vehicles, and any minor roadside issue that does "
            "not require towing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {
                    "type": "string",
                    "description": "Brief description of the roadside issue.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "Vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "request_tow_truck",
        "description": (
            "Dispatches a tow truck. Use for accidents, non-starting cars, major mechanical "
            "failures, overheating, or any situation where the vehicle cannot be driven safely."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {
                    "type": "string",
                    "description": "Brief description of why towing is needed.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "Vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "search_spare_parts",
        "description": (
            "Searches the MyDrive spare parts marketplace. Use when the user wants "
            "to find, order, or enquire about car parts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "part_name": {
                    "type": "string",
                    "description": "Name or description of the spare part.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "Vehicle make/model/year if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["part_name"],
        },
    },
    {
        "name": "book_garage_service",
        "description": (
            "Books a garage service appointment. Use for routine maintenance, "
            "unusual sounds/warning lights, or any situation needing a garage inspection."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_type": {
                    "type": "string",
                    "description": "Type of garage service or inspection needed.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "Vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["service_type"],
        },
    },
]

# Built once; reused for every new Gemini session.
# Always AUDIO modality → output_audio_transcription always available as text reply.
GEMINI_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=SYSTEM_PROMPT,
    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
    thinking_config=types.ThinkingConfig(thinking_budget=0, include_thoughts=False),
    output_audio_transcription={},
    input_audio_transcription={},
    tools=[{"function_declarations": TOOL_DECLARATIONS}],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="AOEDE")
        )
    ),
)

# ──────────────────────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────────────────────

def _tool_roadside(issue_description: str, vehicle_info: str = "unknown") -> dict:
    log.info("TOOL roadside | issue=%r vehicle=%r", issue_description, vehicle_info)
    return {"status": "dispatched", "service": "roadside_assistance", "eta_minutes": 20}


def _tool_tow_truck(issue_description: str, vehicle_info: str = "unknown") -> dict:
    log.info("TOOL tow_truck | issue=%r vehicle=%r", issue_description, vehicle_info)
    return {"status": "dispatched", "service": "tow_truck", "eta_minutes": 35}


def _tool_spare_parts(part_name: str, vehicle_info: str = "unknown") -> dict:
    log.info("TOOL spare_parts | part=%r vehicle=%r", part_name, vehicle_info)
    return {"status": "search_initiated", "part": part_name, "results_count": 12}


def _tool_garage(service_type: str, vehicle_info: str = "unknown") -> dict:
    log.info("TOOL garage | service=%r vehicle=%r", service_type, vehicle_info)
    return {
        "status": "booking_initiated",
        "service_type": service_type,
        "next_available": "tomorrow 10:00 AM",
    }


_TOOL_REGISTRY: dict[str, Any] = {
    "request_roadside_assistance": _tool_roadside,
    "request_tow_truck":           _tool_tow_truck,
    "search_spare_parts":          _tool_spare_parts,
    "book_garage_service":         _tool_garage,
}


def execute_tool(name: str, args: dict) -> dict:
    fn = _TOOL_REGISTRY.get(name)
    if fn is None:
        log.warning("Unknown tool requested: %s", name)
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except TypeError as exc:
        log.error("Tool %s called with bad args %s: %s", name, args, exc)
        return {"error": f"Invalid arguments for {name}: {exc}"}
    except Exception as exc:
        log.exception("Tool %s raised an unexpected error", name)
        return {"error": str(exc)}


# ──────────────────────────────────────────────────────────────
# Domain types
# ──────────────────────────────────────────────────────────────

class FrameKind(StrEnum):
    TEXT        = "text"
    AUDIO       = "audio"
    SET_SPEAKER = "set_speaker"
    STOP        = "stop"


@dataclass(slots=True)
class InputFrame:
    """A message travelling WebSocket handler → session runner."""
    kind:    FrameKind
    payload: Any  # str | bytes | bool | None


# Sentinel pushed into the outbox to signal the forward loop to exit cleanly
_OUTBOX_STOP = object()

# ──────────────────────────────────────────────────────────────
# Session state
# ──────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    """
    All mutable state for one chat session.

    speaker_mode is guarded by _lock because it is written by the receive_loop
    (via the inbox → runner path) and read inside _process_gemini_turn — both
    run concurrently in the same event-loop thread.  An asyncio.Lock is enough.
    """
    session_id:  str
    inbox:       asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=INBOX_MAX_SIZE))
    outbox:      asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=OUTBOX_MAX_SIZE))
    _speaker:    bool          = field(default=False, repr=False)
    _lock:       asyncio.Lock  = field(default_factory=asyncio.Lock, repr=False)
    last_active: float         = field(default_factory=time.monotonic)
    worker_task: asyncio.Task | None = field(default=None, repr=False)

    # ── speaker_mode accessors (async-safe) ───────────────────

    @property
    def speaker_mode(self) -> bool:
        """Non-blocking read — safe for single-threaded async code."""
        return self._speaker

    async def set_speaker_mode(self, enabled: bool) -> None:
        async with self._lock:
            self._speaker = enabled

    async def get_speaker_mode(self) -> bool:
        async with self._lock:
            return self._speaker

    # ── helpers ────────────────────────────────────────────────

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def is_idle(self, ttl: float) -> bool:
        return (time.monotonic() - self.last_active) > ttl

    async def send_outbox(self, frame: tuple | object) -> None:
        """Non-blocking put; logs a warning and drops the frame if outbox is full."""
        try:
            self.outbox.put_nowait(frame)
        except asyncio.QueueFull:
            log.warning("[%s] outbox full — dropping frame", self.session_id)


# ──────────────────────────────────────────────────────────────
# Session runner  (one long-lived task per Gemini session)
# ──────────────────────────────────────────────────────────────

async def session_runner(state: SessionState, client: genai.Client) -> None:
    """
    Opens one Gemini Live session and processes turns until stopped.

    The runner:
      • waits on the inbox queue for the next InputFrame
      • forwards it to Gemini
      • consumes all response events via _process_gemini_turn
      • handles the special SET_SPEAKER frame without touching Gemini
      • exits on STOP frame, inbox idle-timeout, or Gemini session close
    """
    sid = state.session_id
    log.info("[%s] runner starting", sid)

    try:
        async with client.aio.live.connect(model=GEMINI_MODEL, config=GEMINI_CONFIG) as gsession:
            log.info("[%s] Gemini session open", sid)
            await state.send_outbox((
                "session_ready",
                {"session_id": sid, "speaker_mode": state.speaker_mode},
            ))

            while True:
                # Use wait_for so the runner exits cleanly if the client
                # disconnects and never sends anything again.
                try:
                    frame: InputFrame = await asyncio.wait_for(
                        state.inbox.get(), timeout=SESSION_IDLE_TTL
                    )
                except TimeoutError:
                    log.info("[%s] inbox idle timeout — closing session", sid)
                    break

                state.touch()

                if frame.kind == FrameKind.STOP:
                    log.info("[%s] stop signal received", sid)
                    break

                # ── Speaker-mode toggle (no Gemini interaction needed) ──

                if frame.kind == FrameKind.SET_SPEAKER:
                    await state.set_speaker_mode(bool(frame.payload))
                    await state.send_outbox(("speaker_mode_updated", frame.payload))
                    log.info("[%s] speaker_mode → %s", sid, frame.payload)
                    continue

                # ── Forward input to Gemini ────────────────────────────

                try:
                    if frame.kind == FrameKind.TEXT:
                        await gsession.send_client_content(
                            turns=[
                                types.Content(
                                    role="user",
                                    parts=[types.Part(text=frame.payload)],
                                )
                            ],
                            turn_complete=True,
                        )
                    elif frame.kind == FrameKind.AUDIO:
                        await gsession.send_realtime_input(
                            audio=types.Blob(
                                data=frame.payload,
                                mime_type="audio/pcm;rate=16000",
                            )
                        )
                except Exception as exc:
                    log.error("[%s] send to Gemini failed: %s", sid, exc)
                    await state.send_outbox(("error", f"Failed to send to Gemini: {exc}"))
                    continue

                # ── Collect Gemini's response ──────────────────────────

                session_alive = await _process_gemini_turn(gsession, state)
                if not session_alive:
                    log.warning("[%s] Gemini session ended unexpectedly", sid)
                    break

    except asyncio.CancelledError:
        log.info("[%s] runner cancelled", sid)
    except Exception as exc:
        log.exception("[%s] runner fatal error: %s", sid, exc)
        await state.send_outbox(("error", f"Session error: {exc}"))
    finally:
        log.info("[%s] runner shutting down", sid)
        await state.send_outbox(("session_ended", None))
        # Signal the forward loop to exit
        await state.send_outbox(_OUTBOX_STOP)


async def _process_gemini_turn(gsession: Any, state: SessionState) -> bool:
    """
    Consume all server events for one turn.

    Returns True  → turn completed normally; session is still alive.
    Returns False → generator exhausted without turn_complete; session closed.
    """
    sid = state.session_id
    received_any = False

    try:
        async for response in gsession.receive():
            received_any = True
            sc = response.server_content

            if sc is not None:

                # ── Audio PCM ─────────────────────────────────────────
                # Discard silently when speaker is off; text always arrives
                # via output_audio_transcription below.
                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            if await state.get_speaker_mode():
                                await state.send_outbox(("audio_pcm", part.inline_data.data))

                # ── Output transcription → text reply for chat bubble ─
                if sc.output_transcription and sc.output_transcription.text:
                    await state.send_outbox(("assistant_text", sc.output_transcription.text))
                    log.debug("[%s] assistant_text: %s", sid, sc.output_transcription.text)

                # ── Input transcription (voice turns only) ────────────
                if sc.input_transcription and sc.input_transcription.text:
                    await state.send_outbox(("user_transcript", sc.input_transcription.text))
                    log.debug("[%s] user_transcript: %s", sid, sc.input_transcription.text)

                # ── Turn complete ─────────────────────────────────────
                if sc.turn_complete:
                    await state.send_outbox(("turn_complete", None))
                    log.info("[%s] turn_complete", sid)
                    return True

            # ── Tool calls ────────────────────────────────────────────
            if response.tool_call:
                await _handle_tool_call(gsession, state, response.tool_call)

        # Generator exhausted without a turn_complete → Gemini closed the session
        return False

    except Exception as exc:
        log.error("[%s] error receiving from Gemini: %s", sid, exc)
        await state.send_outbox(("error", f"Gemini receive error: {exc}"))
        return False


async def _handle_tool_call(gsession: Any, state: SessionState, tool_call: Any) -> None:
    """
    Execute every function call in a tool_call event, notify the client,
    and send the results back to Gemini in a single send_tool_response call.
    """
    sid = state.session_id
    function_responses: list[types.FunctionResponse] = []

    for fc in tool_call.function_calls:
        args = dict(fc.args) if fc.args else {}
        log.info("[%s] tool_call: %s(%s)", sid, fc.name, args)

        result = execute_tool(fc.name, args)

        # Always notify the client regardless of speaker / input mode
        await state.send_outbox((
            "tool_call",
            {"tool": fc.name, "args": args, "result": result},
        ))

        function_responses.append(
            types.FunctionResponse(id=fc.id, name=fc.name, response={"result": result})
        )

    try:
        await gsession.send_tool_response(function_responses=function_responses)
    except Exception as exc:
        log.error("[%s] failed to send tool response: %s", sid, exc)
        await state.send_outbox(("error", f"Tool response failed: {exc}"))


# ──────────────────────────────────────────────────────────────
# Session manager
# ──────────────────────────────────────────────────────────────

class SessionManager:
    """
    Registry for all active SessionState objects.

    All mutations to _sessions are serialised through _lock (asyncio.Lock).
    """

    def __init__(self, gemini_client: genai.Client) -> None:
        self._client:   genai.Client = gemini_client
        self._sessions: dict[str, SessionState] = {}
        self._lock:     asyncio.Lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    # ── App lifecycle ─────────────────────────────────────────

    async def start(self) -> None:
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="session-cleanup"
        )
        log.info("SessionManager started")

    async def stop(self) -> None:
        log.info("SessionManager stopping")
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            sids = list(self._sessions.keys())
        for sid in sids:
            await self._terminate(sid)
        log.info("SessionManager stopped")

    # ── Public API ────────────────────────────────────────────

    async def get_or_create(self, session_id: str) -> SessionState:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is not None:
                state.touch()
                log.info("[%s] reusing existing session", session_id)
                return state

            state = SessionState(session_id=session_id)
            self._sessions[session_id] = state

        task = asyncio.create_task(
            session_runner(state, self._client),
            name=f"runner-{session_id}",
        )
        state.worker_task = task
        task.add_done_callback(
            lambda _: asyncio.create_task(self._on_runner_done(session_id))
        )
        log.info("[%s] new session created", session_id)
        return state

    async def active_session_count(self) -> int:
        async with self._lock:
            return len(self._sessions)
        
    async def get_active_session_ids(self) -> list[str]:
        """Return a list of all active session IDs (thread-safe)."""
        async with self._lock:
            return list(self._sessions.keys())

    # ── Internal ──────────────────────────────────────────────

    async def _on_runner_done(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
        log.info("[%s] session removed from registry", session_id)

    async def _terminate(self, session_id: str) -> None:
        async with self._lock:
            state = self._sessions.pop(session_id, None)
        if state is None:
            return
        try:
            state.inbox.put_nowait(InputFrame(kind=FrameKind.STOP, payload=None))
        except asyncio.QueueFull:
            pass
        if state.worker_task and not state.worker_task.done():
            state.worker_task.cancel()
            try:
                await state.worker_task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("[%s] session terminated", session_id)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            async with self._lock:
                stale = [
                    sid for sid, s in self._sessions.items()
                    if s.is_idle(SESSION_IDLE_TTL)
                ]
            for sid in stale:
                log.info("[%s] cleaning up idle session", sid)
                await self._terminate(sid)


# ──────────────────────────────────────────────────────────────
# WebSocket helpers
# ──────────────────────────────────────────────────────────────

async def _forward_loop(ws: WebSocket, state: SessionState) -> None:
    """
    Drain the session outbox and write frames to the WebSocket.
    Exits when it dequeues the _OUTBOX_STOP sentinel or a send fails.
    """
    while True:
        item = await state.outbox.get()

        if item is _OUTBOX_STOP:
            break

        kind, payload = item

        try:
            match kind:
                case "audio_pcm":
                    await ws.send_bytes(payload)

                case "session_ready":
                    await ws.send_text(json.dumps({"type": "session_ready", **payload}))

                case "assistant_text":
                    await ws.send_text(json.dumps({"type": "assistant_text", "text": payload}))

                case "user_transcript":
                    await ws.send_text(json.dumps({"type": "user_transcript", "text": payload}))

                case "tool_call":
                    await ws.send_text(json.dumps({"type": "tool_call", **payload}))

                case "turn_complete":
                    await ws.send_text(json.dumps({"type": "turn_complete"}))

                case "speaker_mode_updated":
                    await ws.send_text(
                        json.dumps({"type": "speaker_mode_updated", "enabled": payload})
                    )

                case "error":
                    await ws.send_text(json.dumps({"type": "error", "message": payload}))

                case "session_ended":
                    await ws.send_text(json.dumps({"type": "session_ended"}))

                case _:
                    log.warning("forward_loop: unknown frame kind %r", kind)

        except WebSocketDisconnect:
            log.info("[%s] WebSocket disconnected during send", state.session_id)
            break
        except Exception as exc:
            log.error("[%s] forward_loop send error: %s", state.session_id, exc)
            break


async def _receive_loop(ws: WebSocket, state: SessionState) -> None:
    """
    Read messages from the WebSocket and push InputFrames onto the inbox.

    Handles:
      - binary frames  : raw 16-bit PCM audio (buffered between voice_start / voice_end)
      - text frames    : JSON control messages
    """
    sid = state.session_id
    audio_buffer:   list[bytes] = []
    audio_buffered: int         = 0   # bytes accumulated this voice turn
    voice_active:   bool        = False

    try:
        while True:
            try:
                message = await ws.receive()
            except WebSocketDisconnect:
                log.info("[%s] client disconnected", sid)
                return

            # ── Binary: raw PCM during active voice turn ──────────────
            if "bytes" in message and message["bytes"]:
                if not voice_active:
                    log.debug("[%s] audio bytes received outside voice turn — ignoring", sid)
                    continue
                chunk: bytes = message["bytes"]
                remaining = AUDIO_BUFFER_MAX - audio_buffered
                if remaining <= 0:
                    log.warning("[%s] audio buffer limit reached — dropping chunk", sid)
                    continue
                # Clamp chunk to remaining capacity to avoid going over budget
                clipped = chunk[:remaining]
                audio_buffer.append(clipped)
                audio_buffered += len(clipped)
                continue

            # ── Text: JSON control messages ───────────────────────────
            raw_text = message.get("text", "")
            if not raw_text:
                continue

            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({
                    "type": "error", "message": "Invalid JSON — could not parse message."
                }))
                continue

            msg_type = payload.get("type")

            match msg_type:

                case "text_input":
                    text = (payload.get("text") or "").strip()
                    if not text:
                        continue
                    state.touch()
                    try:
                        state.inbox.put_nowait(InputFrame(FrameKind.TEXT, text))
                    except asyncio.QueueFull:
                        await ws.send_text(json.dumps({
                            "type": "error", "message": "Server busy — try again shortly."
                        }))

                case "voice_start":
                    if voice_active:
                        log.debug("[%s] voice_start received while already active — resetting", sid)
                    audio_buffer.clear()
                    audio_buffered = 0
                    voice_active = True
                    log.debug("[%s] voice_start", sid)

                case "voice_end":
                    voice_active = False
                    if audio_buffer:
                        combined = b"".join(audio_buffer)
                        audio_buffer.clear()
                        audio_buffered = 0
                        state.touch()
                        try:
                            state.inbox.put_nowait(InputFrame(FrameKind.AUDIO, combined))
                        except asyncio.QueueFull:
                            await ws.send_text(json.dumps({
                                "type": "error", "message": "Server busy — try again shortly."
                            }))
                    else:
                        log.debug("[%s] voice_end with empty buffer — ignored", sid)

                case "set_speaker":
                    enabled = bool(payload.get("enabled", False))
                    try:
                        state.inbox.put_nowait(InputFrame(FrameKind.SET_SPEAKER, enabled))
                    except asyncio.QueueFull:
                        await ws.send_text(json.dumps({
                            "type": "error", "message": "Server busy — try again shortly."
                        }))

                case "interrupt":
                    # Clear in-flight audio state; any currently-processing Gemini
                    # turn cannot be cancelled mid-stream but stale audio is discarded.
                    audio_buffer.clear()
                    audio_buffered = 0
                    voice_active = False
                    log.debug("[%s] interrupt — audio buffer cleared", sid)

                case _:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type!r}",
                    }))

    except Exception as exc:
        log.error("[%s] receive_loop unexpected error: %s", sid, exc)


# ──────────────────────────────────────────────────────────────
# Application wiring
# ──────────────────────────────────────────────────────────────

gemini_client   = genai.Client(api_key=GEMINI_API_KEY)
session_manager = SessionManager(gemini_client)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await session_manager.start()
    log.info("Application startup complete")
    yield
    log.info("Application shutting down")
    await session_manager.stop()
    log.info("Application shutdown complete")


app = FastAPI(title="MyDrive Chat API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_manager() -> SessionManager:
    return session_manager


# ──────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def ws_chat(
    ws:         WebSocket,
    session_id: str | None     = Query(default=None),
    manager:    SessionManager = Depends(_get_manager),
) -> None:
    """
    Unified chat endpoint.

    Query params
    ────────────
    session_id  optional UUID — omit to start a brand-new chat.
                Reuse the same UUID to resume a session (memory preserved as
                long as the Gemini session is still alive).
    """
    await ws.accept()

    sid = session_id or str(uuid.uuid4())
    log.info("[%s] WebSocket connected", sid)

    state = await manager.get_or_create(sid)

    try:
        # receive_loop and forward_loop run concurrently.
        # asyncio.gather cancels the other when either exits.
        await asyncio.gather(
            _receive_loop(ws, state),
            _forward_loop(ws, state),
        )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("[%s] unhandled WebSocket error: %s", sid, exc)
    finally:
        # WebSocket is gone; the session_runner keeps running so the Gemini
        # session (and its conversation memory) survives reconnects.
        log.info("[%s] WebSocket handler exiting — session persists", sid)


@app.get("/health")
async def health(manager: SessionManager = Depends(_get_manager)) -> dict:
    return {
        "status":          "ok",
        "model":           GEMINI_MODEL,
        "active_sessions": await manager.active_session_count(),
        "session_ids":     await manager.get_active_session_ids(),
    }