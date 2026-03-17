"""
MyDrive OpenAI Realtime API — Unified Chat Backend
====================================================

Architecture
────────────
  ONE OpenAI Realtime session per chat (session_id), ONE WebSocket connection
  per session. The OpenAI Realtime session is always opened with BOTH text and
  audio modalities so we always receive:
    - output audio PCM (24 kHz, 16-bit PCM)
    - response.audio_transcript.delta → always sent to client as "assistant_text"
    - conversation.item.input_audio_transcription.completed → sent as "user_transcript"
    - response.function_call_arguments.done → always handled regardless of speaker mode

  speaker_mode is a server-side flag on the SessionState — toggling it never
  restarts the OpenAI Realtime session. When OFF the PCM bytes are discarded
  silently; the audio transcript still arrives and is forwarded as text.

Streaming audio design
───────────────────────
  Server VAD is used for voice activity detection (OpenAI handles VAD server-side).
  The backend drives speech-activity signals manually via the client flag:

    voice_start  →  set voice_active=True on server, signal background recv task
    <binary PCM> →  forward each chunk to OpenAI immediately (no buffering)
                    via input_audio_buffer.append events
    voice_end    →  set voice_active=False, commit buffer via
                    input_audio_buffer.commit, then request response via
                    response.create

  OpenAI Realtime API WebSocket events used:
    Client → Server:
      session.update          — configure session (tools, modalities, voice, VAD)
      input_audio_buffer.append  — stream PCM audio chunks
      input_audio_buffer.commit  — signal end of utterance
      response.create         — trigger model response
      conversation.item.create — send text message
      response.cancel         — interrupt current response

    Server → Client events handled:
      session.created / session.updated
      input_audio_buffer.speech_started / speech_stopped
      conversation.item.input_audio_transcription.completed → user_transcript
      response.audio.delta   → binary PCM out (when speaker_mode)
      response.audio_transcript.delta → assistant_text
      response.function_call_arguments.done → tool execution
      response.done          → turn_complete
      error                  → error

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

  Server → Client (JSON)   — IDENTICAL to original Gemini backend
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

Flutter client requires NO changes — the WebSocket protocol is identical.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import websockets
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import SYSTEM__PROMPTS
import TOOLS

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

OPENAI_API_KEY   = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL     = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
OPENAI_VOICE     = os.environ.get("OPENAI_VOICE", "alloy")   # alloy | echo | shimmer | etc.

# OpenAI Realtime WebSocket endpoint
OPENAI_REALTIME_URL = (
    f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
)

SESSION_IDLE_TTL = int(os.environ.get("SESSION_IDLE_TTL", "300"))
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "60"))
INBOX_MAX_SIZE   = int(os.environ.get("INBOX_MAX_SIZE",   "512"))
OUTBOX_MAX_SIZE  = int(os.environ.get("OUTBOX_MAX_SIZE",  "256"))

# ──────────────────────────────────────────────────────────────
# OpenAI session configuration payload
# ──────────────────────────────────────────────────────────────

def _build_session_config() -> dict:
    """
    Build the session.update payload sent to OpenAI immediately after connect.
    Configures: modalities, voice, system prompt, tools, VAD, transcription.
    """
    # Convert TOOLS.TOOLS (list of function defs) to OpenAI tool format
    openai_tools = [
        {
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("parameters", {}),
        }
        for t in TOOLS.TOOLS
    ]

    return {
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": SYSTEM__PROMPTS.SYSTEM_PROPMPT_WITH_SINHALA_EXAMPLES,
            "voice": OPENAI_VOICE,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            # Enable server-side input audio transcription (Whisper)
            "input_audio_transcription": {
                "model": "whisper-1",
            },
            # Server VAD — OpenAI detects speech boundaries automatically.
            # We still drive activity manually via input_audio_buffer.commit
            # + response.create for precise turn control, but keeping VAD
            # enabled lets OpenAI handle partial utterance detection gracefully.
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "create_response": True,
            },
            "tools": openai_tools,
            "tool_choice": "auto",
            # Maximum output tokens per response
            "max_response_output_tokens": 4096,
        },
    }

# ──────────────────────────────────────────────────────────────
# Domain types
# ──────────────────────────────────────────────────────────────

class FrameKind(StrEnum):
    TEXT           = "text"
    AUDIO_CHUNK    = "audio_chunk"
    ACTIVITY_START = "activity_start"
    ACTIVITY_END   = "activity_end"
    SET_SPEAKER    = "set_speaker"
    STOP           = "stop"
    INTERRUPT      = "interrupt"

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
# OpenAI Realtime session runner
# ──────────────────────────────────────────────────────────────

async def session_runner(state: SessionState) -> None:
    """
    Opens one OpenAI Realtime WebSocket session and processes turns until stopped.

    Concurrency model
    ─────────────────
    A single background task (_recv_task) continuously drains OpenAI's event
    stream for the lifetime of the session. The main loop reads InputFrames
    from the inbox and forwards them to OpenAI as WebSocket messages.

    Voice turn sequence:
      ACTIVITY_START → begin accumulating audio (voice_active=True)
      AUDIO_CHUNK    → forward base64-encoded PCM to OpenAI immediately
      ACTIVITY_END   → commit buffer + request response
      OpenAI events  → _recv_task delivers assistant_text / audio / tool_calls
    """
    sid = state.session_id
    log.info("[%s] runner starting", sid)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with websockets.connect(
            OPENAI_REALTIME_URL,
            additional_headers=headers,
            max_size=None,           # no message size limit
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            log.info("[%s] OpenAI Realtime session open", sid)

            # Configure the session immediately
            await ws.send(json.dumps(_build_session_config()))

            # Spawn the persistent event-consumer background task
            recv_task = asyncio.create_task(
                _consume_openai_events(ws, state),
                name=f"recv-{sid}",
            )

            try:
                while True:
                    try:
                        frame: InputFrame = await asyncio.wait_for(
                            state.inbox.get(), timeout=SESSION_IDLE_TTL
                        )
                    except TimeoutError:
                        log.info("[%s] inbox idle timeout — closing session", sid)
                        break

                    state.touch()

                    # ── Stop ─────────────────────────────────────────
                    if frame.kind == FrameKind.STOP:
                        log.info("[%s] stop signal received", sid)
                        break

                    # ── Speaker toggle ────────────────────────────────
                    if frame.kind == FrameKind.SET_SPEAKER:
                        await state.set_speaker_mode(bool(frame.payload))
                        await state.send_outbox(("speaker_mode_updated", frame.payload))
                        log.info("[%s] speaker_mode → %s", sid, frame.payload)
                        continue

                    # ── Interrupt ─────────────────────────────────────
                    if frame.kind == FrameKind.INTERRUPT:
                        try:
                            await ws.send(json.dumps({"type": "response.cancel"}))
                        except Exception as exc:
                            log.warning("[%s] interrupt send failed: %s", sid, exc)
                        continue

                    # ── Voice: activity start ─────────────────────────
                    if frame.kind == FrameKind.ACTIVITY_START:
                        # OpenAI server VAD handles detection; we just start
                        # forwarding audio.  No explicit API call needed here —
                        # audio chunks will be appended to the input buffer.
                        log.debug("[%s] voice turn started", sid)
                        continue

                    # ── Voice: stream PCM chunk immediately ───────────
                    if frame.kind == FrameKind.AUDIO_CHUNK:
                        try:
                            # OpenAI expects base64-encoded PCM16 at 16 kHz mono
                            audio_b64 = base64.b64encode(frame.payload).decode("ascii")
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": audio_b64,
                            }))
                        except Exception as exc:
                            log.warning("[%s] audio chunk send failed: %s", sid, exc)
                        continue

                    # ── Voice: activity end ───────────────────────────
                    if frame.kind == FrameKind.ACTIVITY_END:
                        log.debug("[%s] voice turn ended — committing buffer", sid)
                        try:
                            # Commit the audio buffer (marks end of utterance)
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.commit"
                            }))
                            # Explicitly request a response (works alongside VAD)
                            await ws.send(json.dumps({
                                "type": "response.create",
                                "response": {
                                    "modalities": ["text", "audio"],
                                },
                            }))
                        except Exception as exc:
                            log.error("[%s] voice_end send failed: %s", sid, exc)
                            await state.send_outbox(("error", f"voice_end failed: {exc}"))
                        continue

                    # ── Text input ────────────────────────────────────
                    if frame.kind == FrameKind.TEXT:
                        try:
                            # Create a user conversation item with text content
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {"type": "input_text", "text": frame.payload}
                                    ],
                                },
                            }))
                            # Request a response
                            await ws.send(json.dumps({
                                "type": "response.create",
                                "response": {
                                    "modalities": ["text", "audio"],
                                },
                            }))
                        except Exception as exc:
                            log.error("[%s] text send failed: %s", sid, exc)
                            await state.send_outbox(("error", f"Failed to send to OpenAI: {exc}"))
                        continue

            finally:
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, Exception):
                    pass

    except asyncio.CancelledError:
        log.info("[%s] runner cancelled", sid)
    except Exception as exc:
        log.exception("[%s] runner fatal error: %s", sid, exc)
        await state.send_outbox(("error", f"Session error: {exc}"))
    finally:
        log.info("[%s] runner shutting down", sid)
        await state.send_outbox(("session_ended", None))
        await state.send_outbox(_OUTBOX_STOP)


# ──────────────────────────────────────────────────────────────
# OpenAI Realtime event consumer
# ──────────────────────────────────────────────────────────────

async def _consume_openai_events(ws: Any, state: SessionState) -> None:
    """
    Continuously drain OpenAI Realtime WebSocket events and translate them
    into outbox frames understood by the Flutter client.

    Event mapping (OpenAI Realtime → client protocol)
    ──────────────────────────────────────────────────
    session.created/updated                   → session_ready (once)
    input_audio_buffer.speech_started         → (internal log)
    conversation.item.input_audio_transcription.completed → user_transcript
    response.audio.delta                      → audio_pcm (if speaker_mode)
    response.audio_transcript.delta           → assistant_text
    response.function_call_arguments.done     → tool execution + tool_call
    response.done                             → turn_complete
    error                                     → error

    Partial tool-call argument assembly:
      OpenAI streams function call arguments via response.function_call_arguments.delta.
      We accumulate them per call_id and process on .done.
    """
    sid = state.session_id
    session_ready_sent = False

    # Accumulate function-call arguments streamed as deltas
    # { call_id: {"name": str, "args_buf": str, "item_id": str} }
    pending_tool_calls: dict[str, dict] = {}

    try:
        async for raw_msg in ws:
            try:
                event = json.loads(raw_msg)
            except json.JSONDecodeError:
                log.warning("[%s] received non-JSON from OpenAI", sid)
                continue

            etype = event.get("type", "")
            log.debug("[%s] OpenAI event: %s", sid, etype)

            # ── Session ready ─────────────────────────────────────────
            if etype in ("session.created", "session.updated"):
                if not session_ready_sent:
                    await state.send_outbox((
                        "session_ready",
                        {"session_id": sid, "speaker_mode": state.speaker_mode},
                    ))
                    session_ready_sent = True
                    log.info("[%s] session ready", sid)
                continue

            # ── Input audio transcription (user voice → text) ─────────
            if etype == "conversation.item.input_audio_transcription.completed":
                transcript = (
                    event.get("transcript") or
                    event.get("item", {}).get("content", [{}])[0].get("transcript", "")
                )
                if transcript:
                    await state.send_outbox(("user_transcript", transcript))
                    log.debug("[%s] user_transcript: %r", sid, transcript)
                continue

            # ── Output audio PCM delta ────────────────────────────────
            if etype == "response.audio.delta":
                if await state.get_speaker_mode():
                    audio_b64: str = event.get("delta", "")
                    if audio_b64:
                        try:
                            pcm_bytes = base64.b64decode(audio_b64)
                            await state.send_outbox(("audio_pcm", pcm_bytes))
                        except Exception as exc:
                            log.warning("[%s] audio decode error: %s", sid, exc)
                continue

            # ── Output text transcript delta ──────────────────────────
            if etype == "response.audio_transcript.delta":
                fragment: str = event.get("delta", "")
                if fragment:
                    await state.send_outbox(("assistant_text", fragment))
                    log.debug("[%s] assistant_text fragment: %r", sid, fragment)
                continue

            # Also handle text-only response deltas (when audio modality
            # produces no audio, e.g. after a tool call reply)
            if etype == "response.text.delta":
                fragment = event.get("delta", "")
                if fragment:
                    await state.send_outbox(("assistant_text", fragment))
                continue

            # ── Function call argument streaming ──────────────────────
            if etype == "response.function_call_arguments.delta":
                call_id  = event.get("call_id", "")
                name     = event.get("name", "")
                delta    = event.get("delta", "")
                item_id  = event.get("item_id", "")
                if call_id not in pending_tool_calls:
                    pending_tool_calls[call_id] = {
                        "name": name, "args_buf": "", "item_id": item_id
                    }
                pending_tool_calls[call_id]["args_buf"] += delta
                if name:  # name may arrive on first delta only
                    pending_tool_calls[call_id]["name"] = name
                continue

            # ── Function call completed — execute the tool ────────────
            if etype == "response.function_call_arguments.done":
                call_id   = event.get("call_id", "")
                name      = event.get("name", "")
                args_str  = event.get("arguments", "")
                item_id   = event.get("item_id", "")

                # Prefer fully streamed buffer if available
                if call_id in pending_tool_calls:
                    rec = pending_tool_calls.pop(call_id)
                    name     = name or rec["name"]
                    args_str = args_str or rec["args_buf"]
                    item_id  = item_id or rec["item_id"]

                await _handle_tool_call(ws, state, call_id, item_id, name, args_str)
                continue

            # ── Response done (turn complete) ─────────────────────────
            if etype == "response.done":
                await state.send_outbox(("turn_complete", None))
                log.info("[%s] turn_complete", sid)
                continue

            # ── Speech detection events (informational) ───────────────
            if etype == "input_audio_buffer.speech_started":
                log.debug("[%s] OpenAI VAD: speech started", sid)
                continue

            if etype == "input_audio_buffer.speech_stopped":
                log.debug("[%s] OpenAI VAD: speech stopped", sid)
                continue

            if etype == "input_audio_buffer.committed":
                log.debug("[%s] audio buffer committed", sid)
                continue

            # ── Errors ────────────────────────────────────────────────
            if etype == "error":
                err_obj = event.get("error", {})
                msg = err_obj.get("message", str(event))
                log.error("[%s] OpenAI error event: %s", sid, msg)
                await state.send_outbox(("error", f"OpenAI error: {msg}"))
                continue

    except asyncio.CancelledError:
        raise
    except websockets.exceptions.ConnectionClosed as exc:
        log.info("[%s] OpenAI WebSocket closed: %s", sid, exc)
    except Exception as exc:
        log.error("[%s] event consumer error: %s", sid, exc)
        await state.send_outbox(("error", f"Event stream error: {exc}"))


async def _handle_tool_call(
    ws: Any,
    state: SessionState,
    call_id: str,
    item_id: str,
    name: str,
    args_str: str,
) -> None:
    """Execute a tool call and send the result back to OpenAI."""
    sid = state.session_id
    try:
        args = json.loads(args_str) if args_str else {}
    except json.JSONDecodeError:
        args = {}

    log.info("[%s] tool_call: %s(%s)", sid, name, args)

    # Execute the tool via TOOLS module
    result = TOOLS.execute_tool(name, args)

    # Notify the Flutter client
    await state.send_outbox((
        "tool_call",
        {"tool": name, "args": args, "result": result},
    ))

    # Send tool result back to OpenAI as a function_call_output conversation item
    try:
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"result": result}),
            },
        }))
        # Ask OpenAI to continue generating a response incorporating the tool output
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "modalities": ["text", "audio"],
            },
        }))
    except Exception as exc:
        log.error("[%s] failed to send tool response: %s", sid, exc)
        await state.send_outbox(("error", f"Tool response failed: {exc}"))


# ──────────────────────────────────────────────────────────────
# Session manager
# ──────────────────────────────────────────────────────────────

class SessionManager:
    def __init__(self) -> None:
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
            session_runner(state),
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
    """Drain the outbox and write frames to the Flutter WebSocket client."""
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
    Read messages from the Flutter WebSocket and push InputFrames onto the inbox.

    Binary frames: raw PCM16 @ 16 kHz — enqueued immediately as AUDIO_CHUNK,
    no local buffering. Identical behaviour to the original Gemini backend.
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

            # ── Binary: raw PCM → enqueue immediately ─────────────────
            if "bytes" in message and message["bytes"]:
                if not voice_active:
                    log.debug("[%s] audio bytes outside voice turn — ignoring", sid)
                    continue
                chunk: bytes = message["bytes"]
                state.touch()
                try:
                    state.inbox.put_nowait(InputFrame(FrameKind.AUDIO_CHUNK, chunk))
                except asyncio.QueueFull:
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
                    voice_active = False
                    log.debug("[%s] interrupt", sid)
                    try:
                        state.inbox.put_nowait(InputFrame(FrameKind.INTERRUPT, None))
                    except asyncio.QueueFull:
                        pass

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

session_manager = SessionManager()

@asynccontextmanager
async def lifespan(_: FastAPI):
    await session_manager.start()
    log.info("Application startup complete")
    yield
    log.info("Application shutting down")
    await session_manager.stop()
    log.info("Application shutdown complete")

app = FastAPI(title="MyDrive Chat API (OpenAI)", version="3.0.0", lifespan=lifespan)

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
                long as the OpenAI Realtime session is still alive).

    Wire protocol is 100% identical to the original Gemini backend so the
    Flutter client requires zero changes.
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
        "model":           OPENAI_MODEL,
        "voice":           OPENAI_VOICE,
        "active_sessions": await manager.active_session_count(),
        "session_ids":     await manager.get_active_session_ids(),
    }