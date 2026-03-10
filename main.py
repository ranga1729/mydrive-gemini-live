"""
FastAPI WebSocket Proxy for Gemini Live API — MyDrive Edition (Session-Centric Refactor)
=========================================================================================
Architecture: Decoupled Transport ↔ Intelligence layers.

  The Gemini session lives inside a long-lived background worker task that is
  NOT tied to any WebSocket connection. WebSockets act as "viewports" that
  attach and detach from this worker freely.

  /ws/chat?session_id=<id>  — voice input  (binary PCM + "END_OF_SPEECH")
  /ws/text?session_id=<id>  — text input   (JSON messages)

  Both endpoints share the same SessionContainer. Switching between them
  preserves full Gemini conversation context because the gemini_session
  inside the worker task never stops.

Component overview
──────────────────
  InputFrame          — unified internal message type (text or audio)
  SessionContainer    — holds session state, queues, worker task
  LiveSessionManager  — singleton registry; get_or_create / cleanup
  session_runner()    — the engine: reads inbox → sends to Gemini → broadcasts output
  forward_loop()      — per-WebSocket coroutine: drains subscriber queue → ws.send
  send_loop_*()       — per-WebSocket coroutine: ws.receive → pushes to inbox

Queue / Pub-Sub model
──────────────────────
  inbox_queue       : asyncio.Queue(maxsize=32)
      WebSocket handler → session_runner
  outbox_broadcast  : list[asyncio.Queue]   (one queue per connected viewport)
      session_runner → each connected WebSocket

Audio formats
──────────────
  Input  (client → /ws/chat): raw 16-bit PCM, 16 kHz, mono
  Output (backend → client) : raw 16-bit PCM, 24 kHz, mono
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from dotenv import load_dotenv

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

GEMINI_API_KEY  = os.environ.get("GEMINI_KEY")
GEMINI_MODEL    = "gemini-2.5-flash-native-audio-preview-12-2025"

# Sessions idle longer than this (seconds) are eligible for cleanup
SESSION_GRACE_PERIOD = 300   # 5 minutes
# How often the cleanup loop runs (seconds)
CLEANUP_INTERVAL     = 60
# Max buffered input frames per session (backpressure)
INBOX_MAX_SIZE       = 32
# Catch-up buffer: how many recent output items a new viewport receives
CATCHUP_BUFFER_SIZE  = 20

# ── System prompt & tools ─────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a helpful AI voice assistant for 'MyDrive', an automobile service platform.
Your job is to have a natural conversation with the user to understand their vehicle-related issue,
then trigger the correct service action once their intent is clear.

You have access to four service tools:
- request_roadside_assistance: For flat tyres, dead batteries, fuel delivery, locked-out vehicles, or other minor roadside help.
- request_tow_truck: For accidents, non-starting engines, major mechanical failures, overheating, or smoke coming from the vehicle.
- search_spare_parts: For users looking to find or order specific car parts (glass, mirrors, tyres, engine parts, filters, etc.).
- book_garage_service: For routine maintenance, unusual sounds or smells, warning lights, or scheduling an inspection or service appointment.

RULES:
- Always respond in a warm, conversational, spoken style — your response will be read aloud.
- Ask ONE focused follow-up question at a time if the user's intent is unclear. Do NOT ask multiple questions at once.
- Once the intent is unambiguous, call the appropriate tool immediately. Do NOT ask for confirmation — just call it.
- If the user says something unrelated to vehicle services, politely explain that you can only help with MyDrive services.
- Keep responses concise. This is a voice interface; avoid long paragraphs.

Examples of clear intent → tool mapping:
  "My tyre is flat on the highway" → request_roadside_assistance
  "My car won't start and there's smoke" → request_tow_truck
  "I need a side mirror for my Toyota Corolla" → search_spare_parts
  "I want to book an oil change" → book_garage_service
"""

TOOL_DECLARATIONS = [
    {
        "name": "request_roadside_assistance",
        "description": (
            "Dispatches a roadside assistance unit to help the user. "
            "Use this for flat tyres, dead batteries, fuel delivery, locked-out vehicles, "
            "and any minor roadside issue that does not require towing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {
                    "type": "string",
                    "description": "Brief description of the roadside issue the user is experiencing.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "User's vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "request_tow_truck",
        "description": (
            "Dispatches a tow truck to the user's location. "
            "Use this for accidents, non-starting cars, major engine/mechanical failures, "
            "overheating, or any situation where the vehicle cannot be driven safely."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {
                    "type": "string",
                    "description": "Brief description of why the vehicle needs towing.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "User's vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "search_spare_parts",
        "description": (
            "Searches the MyDrive spare parts marketplace for a specific automotive part. "
            "Use this when the user wants to find, order, or enquire about car parts or components."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "part_name": {
                    "type": "string",
                    "description": "The name or description of the spare part being searched for.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "User's vehicle make/model/year if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["part_name"],
        },
    },
    {
        "name": "book_garage_service",
        "description": (
            "Books a garage service appointment for the user. "
            "Use this for routine maintenance (oil change, tyre rotation, etc.), "
            "unusual sounds or warning lights, or any situation needing a garage inspection."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_type": {
                    "type": "string",
                    "description": "The type of garage service or inspection needed.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "User's vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["service_type"],
        },
    },
]

GEMINI_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=SYSTEM_PROMPT,
    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
    thinking_config=types.ThinkingConfig(
        thinking_budget=0,
        include_thoughts=False,
    ),
    output_audio_transcription={},
    input_audio_transcription={},
    tools=[{"function_declarations": TOOL_DECLARATIONS}],
    speech_config={
        "voice_config": {
            "prebuilt_voice_config": {
                "voice_name": "AOEDE",
            }
        }
    },
)

# ── Tool implementations ───────────────────────────────────────────────────────

def request_roadside_assistance(issue_description: str, vehicle_info: str = "unknown") -> dict:
    print(f"[TOOL] request_roadside_assistance | issue='{issue_description}' | vehicle='{vehicle_info}'")
    return {"status": "dispatched", "service": "roadside_assistance", "eta_minutes": 20}

def request_tow_truck(issue_description: str, vehicle_info: str = "unknown") -> dict:
    print(f"[TOOL] request_tow_truck | issue='{issue_description}' | vehicle='{vehicle_info}'")
    return {"status": "dispatched", "service": "tow_truck", "eta_minutes": 35}

def search_spare_parts(part_name: str, vehicle_info: str = "unknown") -> dict:
    print(f"[TOOL] search_spare_parts | part='{part_name}' | vehicle='{vehicle_info}'")
    return {"status": "search_initiated", "part": part_name, "results_count": 12}

def book_garage_service(service_type: str, vehicle_info: str = "unknown") -> dict:
    print(f"[TOOL] book_garage_service | service='{service_type}' | vehicle='{vehicle_info}'")
    return {"status": "booking_initiated", "service_type": service_type, "next_available": "tomorrow 10:00 AM"}

TOOL_REGISTRY: dict = {
    "request_roadside_assistance": request_roadside_assistance,
    "request_tow_truck":           request_tow_truck,
    "search_spare_parts":          search_spare_parts,
    "book_garage_service":         book_garage_service,
}

def execute_tool(name: str, args: dict) -> dict:
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        print(f"[TOOL] Unknown tool: {name}")
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except Exception as e:
        print(f"[TOOL] Error in '{name}': {e}")
        return {"error": str(e)}

# ── Internal message types ─────────────────────────────────────────────────────

@dataclass
class InputFrame:
    """
    Unified message pushed into the session's inbox_queue.

    kind = "text"   → payload is a str (user's text message)
    kind = "audio"  → payload is bytes (combined PCM chunk)
    kind = "stop"   → signal the runner to shut down
    """
    kind: str        # "text" | "audio" | "stop"
    payload: Any     # str | bytes | None


# Sentinel placed on outbox subscriber queues to signal EOF
_STOP = object()


# ── SessionContainer ───────────────────────────────────────────────────────────

@dataclass
class SessionContainer:
    session_id: str
    inbox_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=INBOX_MAX_SIZE))
    # Each connected WebSocket gets its own subscriber queue in this list
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    # Ring-buffer of recent output frames for catch-up on new connections
    catchup_buffer: list[tuple] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)
    worker_task: asyncio.Task | None = None
    # Event to coordinate: set when a message has been sent to Gemini
    send_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Lock protecting subscriber list mutations
    _sub_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self):
        self.last_active = time.time()

    async def subscribe(self) -> asyncio.Queue:
        """Create and register a new subscriber queue, pre-loaded with catch-up frames."""
        q: asyncio.Queue = asyncio.Queue()
        async with self._sub_lock:
            # Replay recent output so the new viewport isn't blank
            for frame in self.catchup_buffer:
                await q.put(frame)
            self.subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue):
        async with self._sub_lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    async def broadcast(self, frame: tuple):
        """Push a frame to all subscriber queues and update catch-up buffer."""
        async with self._sub_lock:
            # Maintain ring buffer
            self.catchup_buffer.append(frame)
            if len(self.catchup_buffer) > CATCHUP_BUFFER_SIZE:
                self.catchup_buffer.pop(0)
            for q in list(self.subscribers):
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    print(f"[session {self.session_id}] subscriber queue full, dropping frame")


# ── Session runner (the engine) ────────────────────────────────────────────────

async def session_runner(container: SessionContainer, gemini_client: genai.Client):
    """
    Long-lived coroutine that owns the Gemini Live session for one user.

    Loop:
      1. Wait for an InputFrame from inbox_queue.
      2. Send it to Gemini (text or audio).
      3. Drain the Gemini turn via _run_one_turn(), broadcasting all output.
      4. Repeat until a "stop" frame arrives or an unrecoverable error occurs.
    """
    sid = container.session_id
    print(f"[runner {sid}] starting")

    try:
        async with gemini_client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            print(f"[runner {sid}] Gemini session open")
            await container.broadcast(("status", "ready"))

            while True:
                # ── Wait for next input from any connected WebSocket ──────────
                frame: InputFrame = await container.inbox_queue.get()
                container.touch()

                if frame.kind == "stop":
                    print(f"[runner {sid}] stop signal received")
                    break

                # ── Send to Gemini ────────────────────────────────────────────
                try:
                    if frame.kind == "text":
                        await gemini_session.send_client_content(
                            turns=[types.Content(
                                role="user",
                                parts=[types.Part(text=frame.payload)],
                            )],
                            turn_complete=True,
                        )

                    elif frame.kind == "audio":
                        await gemini_session.send_realtime_input(
                            audio=types.Blob(
                                data=frame.payload,
                                mime_type="audio/pcm;rate=16000",
                            )
                        )

                except Exception as send_err:
                    print(f"[runner {sid}] send error: {send_err}")
                    await container.broadcast(("error", str(send_err)))
                    continue

                await container.broadcast(("status", "processing"))

                # ── Drain the Gemini turn ─────────────────────────────────────
                alive = await _run_one_turn(gemini_session, container)
                if not alive:
                    print(f"[runner {sid}] Gemini session ended by server")
                    break

    except Exception as e:
        print(f"[runner {sid}] fatal error: {e}")
        await container.broadcast(("error", str(e)))
    finally:
        print(f"[runner {sid}] shutting down, notifying subscribers")
        await container.broadcast(("status", "session_ended"))
        # Signal all subscriber queues to stop
        async with container._sub_lock:
            for q in container.subscribers:
                try:
                    q.put_nowait(_STOP)
                except asyncio.QueueFull:
                    pass


async def _run_one_turn(gemini_session, container: SessionContainer) -> bool:
    """
    Drain one complete Gemini turn by iterating session.receive().

    Returns True  — turn finished normally (turn_complete received).
    Returns False — session ended / generator exhausted without turn_complete.

    Tool calls are handled inline: execute → send_tool_response → keep
    iterating so Gemini can generate its spoken confirmation in the same turn.
    Tool execution happens here (inside the runner task) so that even if the
    user's WebSocket disconnects mid-tool, the result is still returned to
    Gemini once finished.
    """
    sid = container.session_id
    try:
        async for response in gemini_session.receive():

            # ── Audio / transcription / turn_complete ─────────────────────────
            if response.server_content is not None:
                sc = response.server_content

                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            await container.broadcast(("bytes", part.inline_data.data))

                if sc.output_transcription and sc.output_transcription.text:
                    await container.broadcast(("gemini_transcript", sc.output_transcription.text))
                    print(f"[runner {sid}] gemini_transcript: {sc.output_transcription.text}")

                if sc.input_transcription and sc.input_transcription.text:
                    await container.broadcast(("user_transcript", sc.input_transcription.text))
                    print(f"[runner {sid}] user_transcript: {sc.input_transcription.text}")

                if sc.turn_complete:
                    await container.broadcast(("status", "done"))
                    print(f"[runner {sid}] turn_complete")
                    return True

            # ── Tool calls ────────────────────────────────────────────────────
            if response.tool_call:
                function_responses = []
                for fc in response.tool_call.function_calls:
                    tool_name = fc.name
                    tool_args = dict(fc.args) if fc.args else {}
                    print(f"[runner {sid}] tool_call: {tool_name}({tool_args})")

                    # Non-blocking: execute_tool is synchronous but fast;
                    # for heavy tools wrap with asyncio.to_thread()
                    result = execute_tool(tool_name, tool_args)

                    await container.broadcast(("tool_call", {
                        "tool":   tool_name,
                        "args":   tool_args,
                        "result": result,
                    }))

                    function_responses.append(
                        types.FunctionResponse(
                            id=fc.id,
                            name=tool_name,
                            response={"result": result},
                        )
                    )

                # Return results to Gemini — it continues in the same turn
                await gemini_session.send_tool_response(
                    function_responses=function_responses
                )

        # Generator exhausted without turn_complete
        return False

    except Exception as e:
        print(f"[runner {sid}] _run_one_turn error: {e}")
        await container.broadcast(("error", str(e)))
        return False


# ── LiveSessionManager ─────────────────────────────────────────────────────────

class LiveSessionManager:
    """
    Singleton registry for all active SessionContainers.

    Responsibilities:
      - Create sessions on first connect (get_or_create_session)
      - Provide existing sessions on reconnect (same session_id)
      - Periodically clean up idle sessions (cleanup_loop)
      - Gracefully close all sessions on server shutdown
    """

    def __init__(self, gemini_client: genai.Client):
        self._client   = gemini_client
        self._registry: dict[str, SessionContainer] = {}
        self._lock     = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def start(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        print("[manager] started, cleanup loop running")

    async def stop(self):
        """Called on server shutdown to gracefully close all sessions."""
        print("[manager] shutting down all sessions")
        async with self._lock:
            session_ids = list(self._registry.keys())

        for sid in session_ids:
            await self._terminate_session(sid)

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def get_or_create_session(self, session_id: str) -> SessionContainer:
        async with self._lock:
            if session_id in self._registry:
                container = self._registry[session_id]
                container.touch()
                print(f"[manager] reusing session {session_id}")
                return container

            # Create a new container and start its runner task
            container = SessionContainer(session_id=session_id)
            self._registry[session_id] = container

        # Start runner outside the lock so we don't block other coroutines
        task = asyncio.create_task(
            session_runner(container, self._client),
            name=f"runner-{session_id}",
        )
        container.worker_task = task
        # Auto-remove from registry when runner exits
        task.add_done_callback(lambda _: asyncio.create_task(self._on_runner_done(session_id)))

        print(f"[manager] created new session {session_id}")
        return container

    async def _on_runner_done(self, session_id: str):
        async with self._lock:
            self._registry.pop(session_id, None)
        print(f"[manager] session {session_id} removed from registry")

    async def _terminate_session(self, session_id: str):
        async with self._lock:
            container = self._registry.pop(session_id, None)
        if container is None:
            return
        # Signal the runner to stop cleanly
        try:
            container.inbox_queue.put_nowait(InputFrame(kind="stop", payload=None))
        except asyncio.QueueFull:
            pass
        if container.worker_task and not container.worker_task.done():
            container.worker_task.cancel()
            try:
                await container.worker_task
            except (asyncio.CancelledError, Exception):
                pass
        print(f"[manager] terminated session {session_id}")

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            now = time.time()
            async with self._lock:
                stale = [
                    sid for sid, c in self._registry.items()
                    if now - c.last_active > SESSION_GRACE_PERIOD
                ]
            for sid in stale:
                print(f"[manager] cleaning up idle session {sid}")
                await self._terminate_session(sid)


# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="MyDrive Gemini Live Proxy — Session-Centric")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
session_manager = LiveSessionManager(gemini_client)


#deprecated
# @app.on_event("startup")
# async def on_startup():
#     await session_manager.start()

    # Graceful shutdown on SIGTERM / SIGINT
    # loop = asyncio.get_event_loop()
    # for sig in (signal.SIGTERM, signal.SIGINT):
    #     loop.add_signal_handler(
    #         sig,
    #         lambda: asyncio.create_task(session_manager.stop()),
    #     )


#depricated
# @app.on_event("shutdown")
# async def on_shutdown():
#     await session_manager.stop()
    
    
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic: initialize resources before the application starts
    await session_manager.start()
    # Graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(session_manager.stop()),
        )
    print("Application startup complete. Resources initialized.")
        
    yield
    await session_manager.stop()
    print("Application shutdown complete. Resources cleared.")
    


# ── FastAPI dependency ─────────────────────────────────────────────────────────

def get_manager() -> LiveSessionManager:
    return session_manager


# ── Shared WebSocket helpers ───────────────────────────────────────────────────

async def forward_loop(ws: WebSocket, sub_queue: asyncio.Queue):
    """
    Drains the subscriber queue and writes frames to the WebSocket.
    Exits when _STOP sentinel is received or a send error occurs.
    """
    while True:
        item = await sub_queue.get()
        if item is _STOP:
            break
        kind, payload = item
        try:
            if kind == "bytes":
                await ws.send_bytes(payload)
            elif kind == "status":
                await ws.send_text(json.dumps({"status": payload}))
            elif kind == "error":
                await ws.send_text(json.dumps({"status": "error", "message": payload}))
            elif kind == "gemini_transcript":
                await ws.send_text(json.dumps({"type": "gemini_transcript", "text": payload}))
            elif kind == "user_transcript":
                await ws.send_text(json.dumps({"type": "user_transcript", "text": payload}))
            elif kind == "tool_call":
                await ws.send_text(json.dumps({"type": "tool_call", **payload}))
        except Exception as fe:
            print(f"[forward_loop] send error: {fe}")
            break


# ── /ws/text — text input endpoint ────────────────────────────────────────────

@app.websocket("/ws/text")
async def websocket_text_chat(
    ws: WebSocket,
    session_id: str | None = None,
    manager: LiveSessionManager = Depends(get_manager),
):
    await ws.accept()

    # Auto-generate a session_id if the client doesn't provide one
    if not session_id:
        session_id = str(uuid.uuid4())
        print(f"[/ws/text] new session_id generated: {session_id}")
    else:
        print(f"[/ws/text] client connected with session_id: {session_id}")

    container = await manager.get_or_create_session(session_id)
    sub_queue = await container.subscribe()

    # Inform the client which session_id to use for future reconnects
    await ws.send_text(json.dumps({"type": "session_info", "session_id": session_id}))

    async def send_loop():
        while True:
            try:
                data = await ws.receive()
            except WebSocketDisconnect:
                return
            except Exception as e:
                print(f"[/ws/text send_loop] error: {e}")
                return

            if "text" not in data or not data["text"]:
                continue

            try:
                payload = json.loads(data["text"])
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"status": "error", "message": "Invalid JSON"}))
                continue

            msg_type = payload.get("type", "message")

            if msg_type == "message":
                user_text = payload.get("text", "").strip()
                if not user_text:
                    continue
                try:
                    await container.inbox_queue.put(
                        InputFrame(kind="text", payload=user_text)
                    )
                    container.touch()
                except asyncio.QueueFull:
                    await ws.send_text(json.dumps({
                        "status": "error",
                        "message": "Server busy, please slow down.",
                    }))

            elif msg_type == "interrupt":
                await ws.send_text(json.dumps({"status": "interrupted"}))

    try:
        await asyncio.gather(
            forward_loop(ws, sub_queue),
            send_loop(),
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[/ws/text] unexpected error: {e}")
    finally:
        await container.unsubscribe(sub_queue)
        print(f"[/ws/text] session {session_id} — WebSocket disconnected (session lives on)")


# ── /ws/chat — voice input endpoint ───────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_voice_chat(
    ws: WebSocket,
    session_id: str | None = None,
    manager: LiveSessionManager = Depends(get_manager),
):
    await ws.accept()

    if not session_id:
        session_id = str(uuid.uuid4())
        print(f"[/ws/chat] new session_id generated: {session_id}")
    else:
        print(f"[/ws/chat] client connected with session_id: {session_id}")

    container = await manager.get_or_create_session(session_id)
    sub_queue = await container.subscribe()

    await ws.send_text(json.dumps({"type": "session_info", "session_id": session_id}))

    audio_buffer: list[bytes] = []

    async def send_loop():
        while True:
            try:
                data = await ws.receive()
            except WebSocketDisconnect:
                return
            except Exception as e:
                print(f"[/ws/chat send_loop] error: {e}")
                return

            if "bytes" in data and data["bytes"]:
                audio_buffer.append(data["bytes"])

            elif "text" in data and data["text"]:
                msg = data["text"].strip()

                if msg == "END_OF_SPEECH":
                    if audio_buffer:
                        combined = b"".join(audio_buffer)
                        audio_buffer.clear()
                        try:
                            await container.inbox_queue.put(
                                InputFrame(kind="audio", payload=combined)
                            )
                            container.touch()
                        except asyncio.QueueFull:
                            await ws.send_text(json.dumps({
                                "status": "error",
                                "message": "Server busy, please try again.",
                            }))

                elif msg == "INTERRUPT":
                    audio_buffer.clear()
                    await ws.send_text(json.dumps({"status": "interrupted"}))

    try:
        await asyncio.gather(
            forward_loop(ws, sub_queue),
            send_loop(),
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[/ws/chat] unexpected error: {e}")
    finally:
        await container.unsubscribe(sub_queue)
        print(f"[/ws/chat] session {session_id} — WebSocket disconnected (session lives on)")


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health(
    request: Request,
    manager: LiveSessionManager = Depends(get_manager),
):
    host = request.base_url.netloc
    async with manager._lock:
        active_sessions = len(manager._registry)

    return {
        "status": "ok",
        "model": GEMINI_MODEL,
        "active_sessions": active_sessions,
        "endpoints": {
            "voice": f"ws://{host}/ws/chat",
            "text":  f"ws://{host}/ws/text",
        },
        "tools": [t["name"] for t in TOOL_DECLARATIONS],
        "session_grace_period_seconds": SESSION_GRACE_PERIOD,
    }