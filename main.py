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

Streaming audio design (v2)
───────────────────────────
  Auto-VAD is DISABLED. The backend drives speech-activity signals manually:

    voice_start  →  send activity_start to Gemini
                    launch _consume_gemini_responses as a background task so
                    Gemini responses can arrive WHILE audio is still being sent
    <binary PCM> →  forward each chunk to Gemini immediately (no buffering)
    voice_end    →  send activity_end to Gemini
                    await the background receive task to drain the full response

  This means Gemini starts processing the first words while the user is still
  speaking. The latency saving equals the full utterance duration — typically
  2–8 seconds in practice.

  Concurrency fix: during a voice turn the session_runner no longer blocks on
  _consume_gemini_responses. Instead it keeps pulling frames from the inbox
  (forwarding audio to Gemini) while a separate asyncio.Task drains Gemini's
  response stream in parallel.

Single endpoint
───────────────
  /ws/chat?session_id=<uuid>

  Client → Server (JSON)
    {"type": "text_input",  "text": "..."}
    {"type": "voice_start"}
    {"type": "voice_end"}
    {"type": "set_speaker", "enabled": bool}
    {"type": "interrupt"}
  Client → Server (binary)
    raw 16-bit PCM chunks at 16 kHz (streamed continuously while mic is open)

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
import SYSTEM__PROMPTS

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
# Configuration
# ──────────────────────────────────────────────────────────────

GEMINI_API_KEY   = os.environ["GEMINI_KEY"]
GEMINI_MODEL     = "gemini-2.5-flash-native-audio-preview-12-2025"

SESSION_IDLE_TTL = int(os.environ.get("SESSION_IDLE_TTL", "300"))
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "60"))
# Raised from 32: many small audio-chunk frames arrive during a voice turn
INBOX_MAX_SIZE   = int(os.environ.get("INBOX_MAX_SIZE",   "512"))
OUTBOX_MAX_SIZE  = int(os.environ.get("OUTBOX_MAX_SIZE",  "256"))

TOOLS: list[dict[str, Any]] = [
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
# Gemini configuration
# ──────────────────────────────────────────────────────────────

GEMINI_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=SYSTEM__PROMPTS.SYSTEM_PROPMPT_WITH_SINHALA_EXAMPLES,
    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
    thinking_config=types.ThinkingConfig(thinking_budget=0, include_thoughts=False),
    output_audio_transcription={},
    input_audio_transcription={},
    tools=[{"function_declarations": TOOLS}],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
        )
    ),
    temperature=0.7,
    realtime_input_config=types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
    ),
)

# ──────────────────────────────────────────────────────────────
# Domain types
# ──────────────────────────────────────────────────────────────

class FrameKind(StrEnum):
    TEXT           = "text"
    AUDIO_CHUNK    = "audio_chunk"    # raw PCM — forward to Gemini immediately
    ACTIVITY_START = "activity_start" # user opened mic
    ACTIVITY_END   = "activity_end"   # user closed mic
    SET_SPEAKER    = "set_speaker"
    STOP           = "stop"

@dataclass(slots=True)
class InputFrame:
    kind:    FrameKind
    payload: Any  # str | bytes | bool | None

_OUTBOX_STOP = object()

# ──────────────────────────────────────────────────────────────
# Session state
# ──────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    """All mutable state for one chat session."""
    session_id:  str
    inbox:       asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=INBOX_MAX_SIZE))
    outbox:      asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=OUTBOX_MAX_SIZE))
    _speaker:    bool          = field(default=False, repr=False)
    _lock:       asyncio.Lock  = field(default_factory=asyncio.Lock, repr=False)
    last_active: float         = field(default_factory=time.monotonic)
    worker_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def speaker_mode(self) -> bool:
        return self._speaker

    async def set_speaker_mode(self, enabled: bool) -> None:
        async with self._lock:
            self._speaker = enabled

    async def get_speaker_mode(self) -> bool:
        async with self._lock:
            return self._speaker

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def is_idle(self, ttl: float) -> bool:
        return (time.monotonic() - self.last_active) > ttl

    async def send_outbox(self, frame: tuple | object) -> None:
        try:
            self.outbox.put_nowait(frame)
        except asyncio.QueueFull:
            log.warning("[%s] outbox full — dropping frame", self.session_id)

# ──────────────────────────────────────────────────────────────
# Gemini response consumer
# ──────────────────────────────────────────────────────────────

async def _consume_gemini_responses(gsession: Any, state: SessionState) -> bool:
    """
    Drain Gemini's response stream until turn_complete (or session close).

    Returns True  → turn completed normally.
    Returns False → generator exhausted without turn_complete (session closed).

    During voice turns this runs as a background asyncio.Task so that audio
    forwarding (inbox → Gemini) and response receiving (Gemini → outbox) happen
    concurrently. During text turns it is called directly (awaited inline).
    """
    sid = state.session_id
    try:
        async for response in gsession.receive():
            sc = response.server_content

            if sc is not None:
                # ── Audio PCM output ──────────────────────────────────
                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            if await state.get_speaker_mode():
                                await state.send_outbox(("audio_pcm", part.inline_data.data))

                # ── Output transcription ──────────────────────────────
                if sc.output_transcription and sc.output_transcription.text:
                    await state.send_outbox(("assistant_text", sc.output_transcription.text))
                    log.debug("[%s] assistant_text: %r", sid, sc.output_transcription.text)

                # ── Input transcription ───────────────────────────────
                if sc.input_transcription and sc.input_transcription.text:
                    await state.send_outbox(("user_transcript", sc.input_transcription.text))
                    log.debug("[%s] user_transcript: %r", sid, sc.input_transcription.text)

                # ── Interrupted (barge-in from Gemini's side) ─────────
                if sc.interrupted:
                    log.info("[%s] Gemini interrupted generation", sid)
                    await state.send_outbox(("turn_complete", None))
                    return True

                # ── Turn complete ─────────────────────────────────────
                if sc.turn_complete:
                    await state.send_outbox(("turn_complete", None))
                    log.info("[%s] turn_complete", sid)
                    return True

            # ── Tool calls ────────────────────────────────────────────
            if response.tool_call:
                await _handle_tool_call(gsession, state, response.tool_call)

        return False  # generator exhausted — Gemini closed the session

    except asyncio.CancelledError:
        raise  # let the caller handle it
    except Exception as exc:
        log.error("[%s] error receiving from Gemini: %s", sid, exc)
        await state.send_outbox(("error", f"Gemini receive error: {exc}"))
        return False


async def _handle_tool_call(gsession: Any, state: SessionState, tool_call: Any) -> None:
    sid = state.session_id
    function_responses: list[types.FunctionResponse] = []

    for fc in tool_call.function_calls:
        args = dict(fc.args) if fc.args else {}
        log.info("[%s] tool_call: %s(%s)", sid, fc.name, args)
        result = execute_tool(fc.name, args)
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
# Session runner
# ──────────────────────────────────────────────────────────────

async def session_runner(state: SessionState, client: genai.Client) -> None:
    """
    Opens one Gemini Live session and processes turns until stopped.

    Voice-turn concurrency model
    ────────────────────────────
    ACTIVITY_START arrives
      → send activity_start to Gemini
      → spawn _consume_gemini_responses as a background Task (_recv_task)
         Gemini may begin producing partial responses immediately.

    AUDIO_CHUNK frames arrive (many, rapidly)
      → each chunk is forwarded to Gemini via send_realtime_input immediately
      → _recv_task runs concurrently, draining Gemini's response stream

    ACTIVITY_END arrives
      → send activity_end to Gemini (utterance is complete)
      → await _recv_task to finish draining the full response

    This gives true pipelining: Gemini processes the first half of the utterance
    while the user is still saying the second half.
    """
    sid = state.session_id
    log.info("[%s] runner starting", sid)

    _recv_task: asyncio.Task | None = None  # active during voice turns only

    try:
        async with client.aio.live.connect(model=GEMINI_MODEL, config=GEMINI_CONFIG) as gsession:
            log.info("[%s] Gemini session open", sid)
            await state.send_outbox((
                "session_ready",
                {"session_id": sid, "speaker_mode": state.speaker_mode},
            ))

            while True:
                try:
                    frame: InputFrame = await asyncio.wait_for(
                        state.inbox.get(), timeout=SESSION_IDLE_TTL
                    )
                except TimeoutError:
                    log.info("[%s] inbox idle timeout — closing session", sid)
                    break

                state.touch()

                # ── Stop ──────────────────────────────────────────────
                if frame.kind == FrameKind.STOP:
                    log.info("[%s] stop signal received", sid)
                    break

                # ── Speaker toggle ────────────────────────────────────
                if frame.kind == FrameKind.SET_SPEAKER:
                    await state.set_speaker_mode(bool(frame.payload))
                    await state.send_outbox(("speaker_mode_updated", frame.payload))
                    log.info("[%s] speaker_mode → %s", sid, frame.payload)
                    continue

                # ── Voice: activity start ─────────────────────────────
                if frame.kind == FrameKind.ACTIVITY_START:
                    log.debug("[%s] activity_start → Gemini", sid)
                    try:
                        await gsession.send_realtime_input(
                            activity_start=types.ActivityStart()
                        )
                    except Exception as exc:
                        log.error("[%s] activity_start failed: %s", sid, exc)
                        await state.send_outbox(("error", f"activity_start failed: {exc}"))
                        continue

                    # Start the response consumer concurrently — it will drain
                    # Gemini responses while AUDIO_CHUNK frames are still arriving.
                    _recv_task = asyncio.create_task(
                        _consume_gemini_responses(gsession, state),
                        name=f"recv-{sid}",
                    )
                    continue

                # ── Voice: stream PCM chunk immediately ───────────────
                if frame.kind == FrameKind.AUDIO_CHUNK:
                    try:
                        await gsession.send_realtime_input(
                            audio=types.Blob(
                                data=frame.payload,
                                mime_type="audio/pcm;rate=16000",
                            )
                        )
                    except Exception as exc:
                        # One dropped chunk is survivable — log and continue.
                        log.warning("[%s] audio chunk send failed: %s", sid, exc)
                    continue

                # ── Voice: activity end ───────────────────────────────
                if frame.kind == FrameKind.ACTIVITY_END:
                    log.debug("[%s] activity_end → Gemini", sid)
                    try:
                        await gsession.send_realtime_input(
                            activity_end=types.ActivityEnd()
                        )
                    except Exception as exc:
                        log.error("[%s] activity_end failed: %s", sid, exc)
                        await state.send_outbox(("error", f"activity_end failed: {exc}"))

                    # Now block until the full response has been received.
                    if _recv_task is not None:
                        try:
                            session_alive = await _recv_task
                        except Exception as exc:
                            log.error("[%s] recv task raised: %s", sid, exc)
                            session_alive = False
                        _recv_task = None
                        if not session_alive:
                            log.warning("[%s] Gemini session ended during voice turn", sid)
                            break
                    continue

                # ── Text input ────────────────────────────────────────
                if frame.kind == FrameKind.TEXT:
                    try:
                        await gsession.send_client_content(
                            turns=[
                                types.Content(
                                    role="user",
                                    parts=[types.Part(text=frame.payload)],
                                )
                            ],
                            turn_complete=True,
                        )
                    except Exception as exc:
                        log.error("[%s] text send failed: %s", sid, exc)
                        await state.send_outbox(("error", f"Failed to send to Gemini: {exc}"))
                        continue

                    # Text turns remain sequential — no concurrent task needed.
                    session_alive = await _consume_gemini_responses(gsession, state)
                    if not session_alive:
                        log.warning("[%s] Gemini session ended after text turn", sid)
                        break

    except asyncio.CancelledError:
        log.info("[%s] runner cancelled", sid)
        if _recv_task and not _recv_task.done():
            _recv_task.cancel()
    except Exception as exc:
        log.exception("[%s] runner fatal error: %s", sid, exc)
        await state.send_outbox(("error", f"Session error: {exc}"))
    finally:
        if _recv_task and not _recv_task.done():
            _recv_task.cancel()
        log.info("[%s] runner shutting down", sid)
        await state.send_outbox(("session_ended", None))
        await state.send_outbox(_OUTBOX_STOP)

# ──────────────────────────────────────────────────────────────
# Session manager
# ──────────────────────────────────────────────────────────────

class SessionManager:
    def __init__(self, gemini_client: genai.Client) -> None:
        self._client:   genai.Client = gemini_client
        self._sessions: dict[str, SessionState] = {}
        self._lock:     asyncio.Lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

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
        async with self._lock:
            return list(self._sessions.keys())

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
    """Drain the outbox and write frames to the WebSocket."""
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

    What changed from the original implementation
    ─────────────────────────────────────────────
    OLD: Binary PCM chunks were buffered in audio_buffer[]. On voice_end the
         entire combined blob was sent to Gemini as one AUDIO frame.

    NEW: Each binary PCM chunk becomes an AUDIO_CHUNK frame and is enqueued
         immediately — no local buffering at all. voice_start emits
         ACTIVITY_START; voice_end emits ACTIVITY_END. The session_runner
         forwards each chunk to Gemini the moment it dequeues it.
    """
    sid = state.session_id
    voice_active: bool = False

    try:
        while True:
            try:
                message = await ws.receive()
            except WebSocketDisconnect:
                log.info("[%s] client disconnected", sid)
                return

            # ── Binary: raw PCM → enqueue immediately, no buffering ────
            if "bytes" in message and message["bytes"]:
                if not voice_active:
                    log.debug("[%s] audio bytes outside voice turn — ignoring", sid)
                    continue
                chunk: bytes = message["bytes"]
                state.touch()
                try:
                    state.inbox.put_nowait(InputFrame(FrameKind.AUDIO_CHUNK, chunk))
                except asyncio.QueueFull:
                    # Prefer dropping one chunk over stalling the receive loop.
                    log.warning("[%s] inbox full — dropping audio chunk", sid)
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
                        log.debug("[%s] voice_start while already active — resetting", sid)
                    voice_active = True
                    state.touch()
                    log.debug("[%s] voice_start", sid)
                    try:
                        state.inbox.put_nowait(InputFrame(FrameKind.ACTIVITY_START, None))
                    except asyncio.QueueFull:
                        await ws.send_text(json.dumps({
                            "type": "error", "message": "Server busy — try again shortly."
                        }))

                case "voice_end":
                    if not voice_active:
                        log.debug("[%s] voice_end without active turn — ignored", sid)
                        continue
                    voice_active = False
                    state.touch()
                    log.debug("[%s] voice_end", sid)
                    try:
                        state.inbox.put_nowait(InputFrame(FrameKind.ACTIVITY_END, None))
                    except asyncio.QueueFull:
                        await ws.send_text(json.dumps({
                            "type": "error", "message": "Server busy — try again shortly."
                        }))

                case "set_speaker":
                    enabled = bool(payload.get("enabled", False))
                    try:
                        state.inbox.put_nowait(InputFrame(FrameKind.SET_SPEAKER, enabled))
                    except asyncio.QueueFull:
                        await ws.send_text(json.dumps({
                            "type": "error", "message": "Server busy — try again shortly."
                        }))

                case "interrupt":
                    # Reset local voice state. No stray audio chunks will be
                    # forwarded because voice_active is False from this point.
                    voice_active = False
                    log.debug("[%s] interrupt", sid)

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

app = FastAPI(title="MyDrive Chat API", version="2.1.0", lifespan=lifespan)

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

    session_id  optional UUID — omit to start a brand-new chat.
                Reuse the same UUID to resume a session (memory preserved as
                long as the Gemini session is still alive).
    """
    await ws.accept()
    sid = session_id or str(uuid.uuid4())
    log.info("[%s] WebSocket connected", sid)
    state = await manager.get_or_create(sid)
    try:
        await asyncio.gather(
            _receive_loop(ws, state),
            _forward_loop(ws, state),
        )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("[%s] unhandled WebSocket error: %s", sid, exc)
    finally:
        log.info("[%s] WebSocket handler exiting — session persists", sid)


@app.get("/health")
async def health(manager: SessionManager = Depends(_get_manager)) -> dict:
    return {
        "status":          "ok",
        "model":           GEMINI_MODEL,
        "active_sessions": await manager.active_session_count(),
        "session_ids":     await manager.get_active_session_ids(),
    }