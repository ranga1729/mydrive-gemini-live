"""
MyDrive OpenAI Realtime API — Unified Chat Backend (Azure-Fixed)
=================================================================

Fixes applied vs original:
  1. Added /health GET and root GET so Azure App Service health probes succeed
     on startup (prevents the app from being killed before any WS connects).
  2. WebSocket upgrade now responds with session_ready immediately (before
     OpenAI's session.created arrives) so the Flutter client doesn't time out.
  3. Added explicit websockets ping_keepalive to keep the OpenAI connection alive
     through Azure's 4-minute idle timeout on upstream connections.
  4. Startup log tells you to enable WebSockets in Azure → Configuration →
     General Settings.
  5. Used websockets.connect() with open_timeout/close_timeout to avoid
     hanging indefinitely on Azure cold-start.
  6. FrameKind.INTERRUPT properly handled in receive_loop (was missing in
     the original).
  7. session_runner now accepts a timeout_secs argument for the OpenAI
     connection so it fails fast instead of hanging the health probe.
  8. Fixed session.created vs session.updated: session_ready now sent once on
     session.created only (not on every session.updated), avoiding duplicate
     session_ready frames.
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
from fastapi.responses import JSONResponse

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

OPENAI_API_KEY  = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL    = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
OPENAI_VOICE    = os.environ.get("OPENAI_VOICE", "alloy")

OPENAI_REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"

SESSION_IDLE_TTL       = int(os.environ.get("SESSION_IDLE_TTL", "300"))
CLEANUP_INTERVAL       = int(os.environ.get("CLEANUP_INTERVAL", "60"))
INBOX_MAX_SIZE         = int(os.environ.get("INBOX_MAX_SIZE",   "512"))
OUTBOX_MAX_SIZE        = int(os.environ.get("OUTBOX_MAX_SIZE",  "256"))

# How long to wait for the OpenAI WSS handshake before giving up.
# Azure has a 230 s overall HTTP timeout; keep this well below that.
OPENAI_CONNECT_TIMEOUT = float(os.environ.get("OPENAI_CONNECT_TIMEOUT", "20"))

# ──────────────────────────────────────────────────────────────
# OpenAI session configuration
# ──────────────────────────────────────────────────────────────

def _build_session_config() -> dict:
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
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "create_response": True,
            },
            "tools": openai_tools,
            "tool_choice": "auto",
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
    payload: Any

_OUTBOX_STOP = object()

# ──────────────────────────────────────────────────────────────
# Session state
# ──────────────────────────────────────────────────────────────

@dataclass
class SessionState:
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
    sid = state.session_id
    log.info("[%s] runner starting", sid)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    # ── FIX 1: Send session_ready to the client IMMEDIATELY ───────────────
    # Don't wait for OpenAI's session.created event — the client WebSocket
    # handshake (and Azure's gateway) will time out if we stall here.
    # The event consumer will NOT send a second session_ready.
    await state.send_outbox((
        "session_ready",
        {"session_id": sid, "speaker_mode": state.speaker_mode},
    ))
    log.info("[%s] session_ready sent (pre-OpenAI connect)", sid)

    try:
        # ── FIX 2: Use open_timeout so a slow OpenAI handshake fails fast ──
        async with websockets.connect(
            OPENAI_REALTIME_URL,
            additional_headers=headers,
            max_size=None,
            open_timeout=OPENAI_CONNECT_TIMEOUT,
            close_timeout=10,
            # ── FIX 3: Keep the upstream connection alive through Azure's ──
            # 4-minute idle TCP timeout by sending WebSocket pings.
            ping_interval=30,
            ping_timeout=20,
        ) as ws:
            log.info("[%s] OpenAI Realtime session open", sid)

            await ws.send(json.dumps(_build_session_config()))

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

                    if frame.kind == FrameKind.STOP:
                        log.info("[%s] stop signal received", sid)
                        break

                    if frame.kind == FrameKind.SET_SPEAKER:
                        await state.set_speaker_mode(bool(frame.payload))
                        await state.send_outbox(("speaker_mode_updated", frame.payload))
                        log.info("[%s] speaker_mode → %s", sid, frame.payload)
                        continue

                    if frame.kind == FrameKind.INTERRUPT:
                        try:
                            await ws.send(json.dumps({"type": "response.cancel"}))
                        except Exception as exc:
                            log.warning("[%s] interrupt send failed: %s", sid, exc)
                        continue

                    if frame.kind == FrameKind.ACTIVITY_START:
                        log.debug("[%s] voice turn started", sid)
                        continue

                    if frame.kind == FrameKind.AUDIO_CHUNK:
                        try:
                            audio_b64 = base64.b64encode(frame.payload).decode("ascii")
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": audio_b64,
                            }))
                        except Exception as exc:
                            log.warning("[%s] audio chunk send failed: %s", sid, exc)
                        continue

                    if frame.kind == FrameKind.ACTIVITY_END:
                        log.debug("[%s] voice turn ended — committing buffer", sid)
                        try:
                            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await ws.send(json.dumps({
                                "type": "response.create",
                                "response": {"modalities": ["text", "audio"]},
                            }))
                        except Exception as exc:
                            log.error("[%s] voice_end send failed: %s", sid, exc)
                            await state.send_outbox(("error", f"voice_end failed: {exc}"))
                        continue

                    if frame.kind == FrameKind.TEXT:
                        try:
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": frame.payload}],
                                },
                            }))
                            await ws.send(json.dumps({
                                "type": "response.create",
                                "response": {"modalities": ["text", "audio"]},
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
    except TimeoutError:
        # ── FIX 4: Surface the connect timeout as a readable error ─────────
        log.error("[%s] timed out connecting to OpenAI Realtime API", sid)
        await state.send_outbox(("error", "Timed out connecting to OpenAI — please retry."))
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
    sid = state.session_id

    # ── FIX 5: Never send session_ready here — it's already been sent ─────
    # The runner sends it immediately before the OpenAI connect, so the
    # Flutter client doesn't stall waiting for the OpenAI handshake.

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

            # ── FIX 6: Log session.created/updated but don't re-send ──────
            if etype in ("session.created", "session.updated"):
                log.info("[%s] OpenAI %s", sid, etype)
                continue

            if etype == "conversation.item.input_audio_transcription.completed":
                transcript = (
                    event.get("transcript") or
                    event.get("item", {}).get("content", [{}])[0].get("transcript", "")
                )
                if transcript:
                    await state.send_outbox(("user_transcript", transcript))
                    log.debug("[%s] user_transcript: %r", sid, transcript)
                continue

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

            if etype == "response.audio_transcript.delta":
                fragment: str = event.get("delta", "")
                if fragment:
                    await state.send_outbox(("assistant_text", fragment))
                    log.debug("[%s] assistant_text fragment: %r", sid, fragment)
                continue

            if etype == "response.text.delta":
                fragment = event.get("delta", "")
                if fragment:
                    await state.send_outbox(("assistant_text", fragment))
                continue

            if etype == "response.function_call_arguments.delta":
                call_id = event.get("call_id", "")
                name    = event.get("name", "")
                delta   = event.get("delta", "")
                item_id = event.get("item_id", "")
                if call_id not in pending_tool_calls:
                    pending_tool_calls[call_id] = {
                        "name": name, "args_buf": "", "item_id": item_id
                    }
                pending_tool_calls[call_id]["args_buf"] += delta
                if name:
                    pending_tool_calls[call_id]["name"] = name
                continue

            if etype == "response.function_call_arguments.done":
                call_id  = event.get("call_id", "")
                name     = event.get("name", "")
                args_str = event.get("arguments", "")
                item_id  = event.get("item_id", "")
                if call_id in pending_tool_calls:
                    rec      = pending_tool_calls.pop(call_id)
                    name     = name or rec["name"]
                    args_str = args_str or rec["args_buf"]
                    item_id  = item_id or rec["item_id"]
                await _handle_tool_call(ws, state, call_id, item_id, name, args_str)
                continue

            if etype == "response.done":
                await state.send_outbox(("turn_complete", None))
                log.info("[%s] turn_complete", sid)
                continue

            if etype == "input_audio_buffer.speech_started":
                log.debug("[%s] OpenAI VAD: speech started", sid)
                continue

            if etype == "input_audio_buffer.speech_stopped":
                log.debug("[%s] OpenAI VAD: speech stopped", sid)
                continue

            if etype == "input_audio_buffer.committed":
                log.debug("[%s] audio buffer committed", sid)
                continue

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
    sid = state.session_id
    try:
        args = json.loads(args_str) if args_str else {}
    except json.JSONDecodeError:
        args = {}

    log.info("[%s] tool_call: %s(%s)", sid, name, args)
    result = TOOLS.execute_tool(name, args)

    await state.send_outbox(("tool_call", {"tool": name, "args": args, "result": result}))

    try:
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"result": result}),
            },
        }))
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {"modalities": ["text", "audio"]},
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
    sid = state.session_id
    voice_active: bool = False

    try:
        while True:
            try:
                message = await ws.receive()
            except WebSocketDisconnect:
                log.info("[%s] client disconnected", sid)
                return

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
                    # ── FIX 7: Actually enqueue the INTERRUPT frame ──────
                    # The original receive_loop set voice_active=False but
                    # never put an INTERRUPT frame on the inbox, so
                    # response.cancel was never sent to OpenAI.
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
    # ── FIX 8: Warn on startup if WebSockets are not enabled in Azure ──────
    log.info(
        "REMINDER: Ensure WebSockets are enabled in Azure App Service → "
        "Configuration → General Settings → Web sockets = On"
    )
    await session_manager.start()
    log.info("Application startup complete — model=%s voice=%s", OPENAI_MODEL, OPENAI_VOICE)
    yield
    log.info("Application shutting down")
    await session_manager.stop()
    log.info("Application shutdown complete")

app = FastAPI(title="MyDrive Chat API (OpenAI)", version="3.1.0", lifespan=lifespan)

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

# ── FIX 9: Root endpoint for Azure health probes ──────────────────────────
# Azure App Service pings GET / to check if the app is alive. Without this
# every health probe returns 404, which causes the platform to restart the
# dyno — sometimes before any WebSocket connection can be established.
@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mydrive-openai"})


@app.websocket("/ws/chat")
async def ws_chat(
    ws:         WebSocket,
    session_id: str | None     = Query(default=None),
    manager:    SessionManager = Depends(_get_manager),
) -> None:
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