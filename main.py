"""
FastAPI WebSocket Proxy for Gemini Live API — MyDrive Edition (Session-Centric)
================================================================================
Design principle
────────────────
  ONE Gemini session per user, shared across both WebSocket endpoints.
  The session is owned by a long-lived background task (session_runner) that is
  completely independent of any WebSocket connection lifetime.

  /ws/chat?session_id=<id>  — voice channel
      Client  → server : raw 16-bit PCM chunks (16 kHz mono) + "END_OF_SPEECH"
      Server  → client : binary PCM audio (24 kHz mono) + ALL JSON frames

  /ws/text?session_id=<id>  — text channel
      Client  → server : JSON  {"type": "message", "text": "..."}
      Server  → client : ALL JSON frames (NO binary audio frames)

  Both endpoints write to the same inbox_queue → same Gemini session → memory
  is preserved no matter which endpoint the user uses.

Broadcast rules (the key fix)
──────────────────────────────
  Audio (binary PCM) → ONLY subscribers registered with wants_audio=True (/ws/chat)
  JSON frames        → ALL subscribers regardless of type

  This means:
    /ws/chat subscriber → receives audio + all JSON frames
    /ws/text subscriber → receives JSON frames only, never binary audio

  No frame is ever processed twice by the Flutter client.
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

load_dotenv()

GEMINI_API_KEY       = os.environ.get("GEMINI_KEY")
GEMINI_MODEL         = "gemini-2.5-flash-native-audio-preview-12-2025"
SESSION_GRACE_PERIOD = 300
CLEANUP_INTERVAL     = 60
INBOX_MAX_SIZE       = 32
CATCHUP_BUFFER_SIZE  = 10

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
- Ask ONE focused follow-up question at a time if the user's intent is unclear.
- Once the intent is unambiguous, call the appropriate tool immediately. Do NOT ask for confirmation.
- If the user says something unrelated to vehicle services, politely explain that you can only help with MyDrive services.
- Keep responses concise. This is a voice interface; avoid long paragraphs.
"""

TOOL_DECLARATIONS = [
    {
        "name": "request_roadside_assistance",
        "description": "Dispatches a roadside assistance unit. Use for flat tyres, dead batteries, fuel delivery, locked-out vehicles, and any minor roadside issue that does not require towing.",
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {"type": "string", "description": "Brief description of the roadside issue."},
                "vehicle_info":      {"type": "string", "description": "Vehicle make/model if mentioned, otherwise 'unknown'."},
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "request_tow_truck",
        "description": "Dispatches a tow truck. Use for accidents, non-starting cars, major mechanical failures, overheating, or any situation where the vehicle cannot be driven safely.",
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {"type": "string", "description": "Brief description of why towing is needed."},
                "vehicle_info":      {"type": "string", "description": "Vehicle make/model if mentioned, otherwise 'unknown'."},
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "search_spare_parts",
        "description": "Searches the MyDrive spare parts marketplace. Use when the user wants to find, order, or enquire about car parts.",
        "parameters": {
            "type": "object",
            "properties": {
                "part_name":    {"type": "string", "description": "Name or description of the spare part."},
                "vehicle_info": {"type": "string", "description": "Vehicle make/model/year if mentioned, otherwise 'unknown'."},
            },
            "required": ["part_name"],
        },
    },
    {
        "name": "book_garage_service",
        "description": "Books a garage service appointment. Use for routine maintenance, unusual sounds/warning lights, or any situation needing a garage inspection.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_type": {"type": "string", "description": "Type of garage service or inspection needed."},
                "vehicle_info": {"type": "string", "description": "Vehicle make/model if mentioned, otherwise 'unknown'."},
            },
            "required": ["service_type"],
        },
    },
]

GEMINI_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=SYSTEM_PROMPT,
    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
    thinking_config=types.ThinkingConfig(thinking_budget=0, include_thoughts=False),
    output_audio_transcription={},
    input_audio_transcription={},
    tools=[{"function_declarations": TOOL_DECLARATIONS}],
    speech_config={"voice_config": {"prebuilt_voice_config": {"voice_name": "AOEDE"}}},
)


def request_roadside_assistance(issue_description: str, vehicle_info: str = "unknown") -> dict:
    print(f"[TOOL] roadside | issue='{issue_description}' vehicle='{vehicle_info}'")
    return {"status": "dispatched", "service": "roadside_assistance", "eta_minutes": 20}

def request_tow_truck(issue_description: str, vehicle_info: str = "unknown") -> dict:
    print(f"[TOOL] tow_truck | issue='{issue_description}' vehicle='{vehicle_info}'")
    return {"status": "dispatched", "service": "tow_truck", "eta_minutes": 35}

def search_spare_parts(part_name: str, vehicle_info: str = "unknown") -> dict:
    print(f"[TOOL] spare_parts | part='{part_name}' vehicle='{vehicle_info}'")
    return {"status": "search_initiated", "part": part_name, "results_count": 12}

def book_garage_service(service_type: str, vehicle_info: str = "unknown") -> dict:
    print(f"[TOOL] garage | service='{service_type}' vehicle='{vehicle_info}'")
    return {"status": "booking_initiated", "service_type": service_type, "next_available": "tomorrow 10:00 AM"}

TOOL_REGISTRY = {
    "request_roadside_assistance": request_roadside_assistance,
    "request_tow_truck":           request_tow_truck,
    "search_spare_parts":          search_spare_parts,
    "book_garage_service":         book_garage_service,
}

def execute_tool(name: str, args: dict) -> dict:
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except Exception as e:
        return {"error": str(e)}


@dataclass
class InputFrame:
    kind: str     # "text" | "audio" | "stop"
    payload: Any  # str | bytes | None

_STOP = object()


@dataclass
class Subscriber:
    """
    One active WebSocket viewport.
    wants_audio=True  → /ws/chat  (receives binary PCM + all JSON)
    wants_audio=False → /ws/text  (receives JSON only)
    """
    queue:       asyncio.Queue
    wants_audio: bool


@dataclass
class SessionContainer:
    session_id:     str
    inbox_queue:    asyncio.Queue    = field(default_factory=lambda: asyncio.Queue(maxsize=INBOX_MAX_SIZE))
    subscribers:    list             = field(default_factory=list)   # list[Subscriber]
    catchup_buffer: list             = field(default_factory=list)   # recent JSON frames only
    last_active:    float            = field(default_factory=time.time)
    worker_task:    asyncio.Task | None = None
    _sub_lock:      asyncio.Lock     = field(default_factory=asyncio.Lock)

    def touch(self):
        self.last_active = time.time()

    async def subscribe(self, wants_audio: bool) -> "Subscriber":
        sub = Subscriber(queue=asyncio.Queue(), wants_audio=wants_audio)
        async with self._sub_lock:
            for frame in self.catchup_buffer:
                await sub.queue.put(frame)
            self.subscribers.append(sub)
        return sub

    async def unsubscribe(self, sub: "Subscriber"):
        async with self._sub_lock:
            try:
                self.subscribers.remove(sub)
            except ValueError:
                pass

    async def broadcast_audio(self, pcm: bytes):
        """Send binary PCM ONLY to voice subscribers (wants_audio=True)."""
        async with self._sub_lock:
            for sub in list(self.subscribers):
                if sub.wants_audio:
                    try:
                        sub.queue.put_nowait(("bytes", pcm))
                    except asyncio.QueueFull:
                        print(f"[session {self.session_id}] voice queue full, dropping audio chunk")

    async def broadcast_json(self, frame: tuple):
        """Send a JSON frame to ALL subscribers and update catch-up buffer."""
        async with self._sub_lock:
            self.catchup_buffer.append(frame)
            if len(self.catchup_buffer) > CATCHUP_BUFFER_SIZE:
                self.catchup_buffer.pop(0)
            for sub in list(self.subscribers):
                try:
                    sub.queue.put_nowait(frame)
                except asyncio.QueueFull:
                    print(f"[session {self.session_id}] subscriber queue full, dropping JSON frame")


async def session_runner(container: SessionContainer, gemini_client: genai.Client):
    sid = container.session_id
    print(f"[runner {sid}] starting")
    try:
        async with gemini_client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            print(f"[runner {sid}] Gemini session open")
            await container.broadcast_json(("status", "ready"))

            while True:
                frame: InputFrame = await container.inbox_queue.get()
                container.touch()

                if frame.kind == "stop":
                    print(f"[runner {sid}] stop signal")
                    break

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
                            audio=types.Blob(data=frame.payload, mime_type="audio/pcm;rate=16000")
                        )
                except Exception as send_err:
                    print(f"[runner {sid}] send error: {send_err}")
                    await container.broadcast_json(("error", str(send_err)))
                    continue

                await container.broadcast_json(("status", "processing"))

                alive = await _run_one_turn(gemini_session, container)
                if not alive:
                    print(f"[runner {sid}] Gemini session ended")
                    break

    except Exception as e:
        print(f"[runner {sid}] fatal error: {e}")
        await container.broadcast_json(("error", str(e)))
    finally:
        print(f"[runner {sid}] shutting down")
        await container.broadcast_json(("status", "session_ended"))
        async with container._sub_lock:
            for sub in container.subscribers:
                try:
                    sub.queue.put_nowait(_STOP)
                except asyncio.QueueFull:
                    pass


async def _run_one_turn(gemini_session, container: SessionContainer) -> bool:
    sid = container.session_id
    try:
        async for response in gemini_session.receive():

            if response.server_content is not None:
                sc = response.server_content

                # Audio → voice subscribers only
                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            await container.broadcast_audio(part.inline_data.data)

                # Gemini voice transcript → all subscribers
                if sc.output_transcription and sc.output_transcription.text:
                    await container.broadcast_json(("gemini_transcript", sc.output_transcription.text))
                    print(f"[runner {sid}] gemini_transcript: {sc.output_transcription.text}")

                # User voice transcript → all subscribers (only present for audio turns)
                if sc.input_transcription and sc.input_transcription.text:
                    await container.broadcast_json(("user_transcript", sc.input_transcription.text))
                    print(f"[runner {sid}] user_transcript: {sc.input_transcription.text}")

                if sc.turn_complete:
                    await container.broadcast_json(("status", "done"))
                    print(f"[runner {sid}] turn_complete")
                    return True

            if response.tool_call:
                function_responses = []
                for fc in response.tool_call.function_calls:
                    tool_name = fc.name
                    tool_args = dict(fc.args) if fc.args else {}
                    print(f"[runner {sid}] tool_call: {tool_name}({tool_args})")
                    result = execute_tool(tool_name, tool_args)
                    await container.broadcast_json(("tool_call", {
                        "tool":   tool_name,
                        "args":   tool_args,
                        "result": result,
                    }))
                    function_responses.append(
                        types.FunctionResponse(id=fc.id, name=tool_name, response={"result": result})
                    )
                await gemini_session.send_tool_response(function_responses=function_responses)

        return False

    except Exception as e:
        print(f"[runner {sid}] _run_one_turn error: {e}")
        await container.broadcast_json(("error", str(e)))
        return False


class LiveSessionManager:
    def __init__(self, gemini_client: genai.Client):
        self._client   = gemini_client
        self._registry: dict[str, SessionContainer] = {}
        self._lock     = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def start(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        print("[manager] started")

    async def stop(self):
        print("[manager] shutting down")
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
            container = SessionContainer(session_id=session_id)
            self._registry[session_id] = container

        task = asyncio.create_task(
            session_runner(container, self._client),
            name=f"runner-{session_id}",
        )
        container.worker_task = task
        task.add_done_callback(lambda _: asyncio.create_task(self._on_runner_done(session_id)))
        print(f"[manager] created session {session_id}")
        return container

    async def _on_runner_done(self, session_id: str):
        async with self._lock:
            self._registry.pop(session_id, None)
        print(f"[manager] session {session_id} removed")

    async def _terminate_session(self, session_id: str):
        async with self._lock:
            container = self._registry.pop(session_id, None)
        if container is None:
            return
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


gemini_client   = genai.Client(api_key=GEMINI_API_KEY)
session_manager = LiveSessionManager(gemini_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await session_manager.start()
    
    # loop = asyncio.get_event_loop()
    # for sig in (signal.SIGTERM, signal.SIGINT):
    #     loop.add_signal_handler(sig, lambda: asyncio.create_task(session_manager.stop()))
    print("Application startup complete.")
    
    yield
    
    print("Shutting down session manager...")
    await session_manager.stop()
    print("Application shutdown complete.")


app = FastAPI(title="MyDrive Gemini Live Proxy", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def get_manager() -> LiveSessionManager:
    return session_manager


async def forward_loop(ws: WebSocket, sub: Subscriber):
    """Drain subscriber queue → WebSocket. Audio only reaches voice subs by design."""
    while True:
        item = await sub.queue.get()
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


@app.websocket("/ws/chat")
async def websocket_voice_chat(
    ws: WebSocket,
    session_id: str | None = None,
    manager: LiveSessionManager = Depends(get_manager),
):
    await ws.accept()
    if not session_id:
        session_id = str(uuid.uuid4())
    print(f"[/ws/chat] session_id={session_id}")

    container = await manager.get_or_create_session(session_id)
    sub = await container.subscribe(wants_audio=True)
    await ws.send_text(json.dumps({"type": "session_info", "session_id": session_id}))

    audio_buffer: list[bytes] = []

    async def send_loop():
        while True:
            try:
                data = await ws.receive()
            except WebSocketDisconnect:
                return
            except Exception as e:
                print(f"[/ws/chat send_loop] {e}")
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
                            await container.inbox_queue.put(InputFrame(kind="audio", payload=combined))
                            container.touch()
                        except asyncio.QueueFull:
                            await ws.send_text(json.dumps({"status": "error", "message": "Server busy."}))
                elif msg == "INTERRUPT":
                    audio_buffer.clear()
                    await ws.send_text(json.dumps({"status": "interrupted"}))

    try:
        await asyncio.gather(forward_loop(ws, sub), send_loop())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[/ws/chat] {e}")
    finally:
        await container.unsubscribe(sub)
        print(f"[/ws/chat] session {session_id} disconnected (session lives on)")


@app.websocket("/ws/text")
async def websocket_text_chat(
    ws: WebSocket,
    session_id: str | None = None,
    manager: LiveSessionManager = Depends(get_manager),
):
    await ws.accept()
    if not session_id:
        session_id = str(uuid.uuid4())
    print(f"[/ws/text] session_id={session_id}")

    container = await manager.get_or_create_session(session_id)
    sub = await container.subscribe(wants_audio=False)
    await ws.send_text(json.dumps({"type": "session_info", "session_id": session_id}))

    async def send_loop():
        while True:
            try:
                data = await ws.receive()
            except WebSocketDisconnect:
                return
            except Exception as e:
                print(f"[/ws/text send_loop] {e}")
                return

            if "text" not in data or not data["text"]:
                continue
            try:
                payload = json.loads(data["text"])
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"status": "error", "message": "Invalid JSON"}))
                continue

            if payload.get("type") == "message":
                user_text = payload.get("text", "").strip()
                if user_text:
                    try:
                        await container.inbox_queue.put(InputFrame(kind="text", payload=user_text))
                        container.touch()
                    except asyncio.QueueFull:
                        await ws.send_text(json.dumps({"status": "error", "message": "Server busy."}))
            elif payload.get("type") == "interrupt":
                await ws.send_text(json.dumps({"status": "interrupted"}))

    try:
        await asyncio.gather(forward_loop(ws, sub), send_loop())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[/ws/text] {e}")
    finally:
        await container.unsubscribe(sub)
        print(f"[/ws/text] session {session_id} disconnected (session lives on)")


@app.get("/health")
async def health(request: Request, manager: LiveSessionManager = Depends(get_manager)):
    host = request.base_url.netloc
    async with manager._lock:
        active_sessions = len(manager._registry)
    return {
        "status": "ok",
        "model": GEMINI_MODEL,
        "active_sessions": active_sessions,
        "endpoints": {
            "voice": f"wss://{host}/ws/chat",
            "text":  f"wss://{host}/ws/text",
        },
        "tools": [t["name"] for t in TOOL_DECLARATIONS],
    }