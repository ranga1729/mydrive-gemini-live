"""
FastAPI WebSocket Proxy for Gemini Live API — MyDrive Edition
=============================================================
Two endpoints:

  /ws/chat   — voice input  (client sends binary PCM + "END_OF_SPEECH" control)
  /ws/text   — text input   (client sends plain text messages as JSON)

Both endpoints:
  • Use the MyDrive system prompt with function calling tools
  • Return audio responses as binary PCM frames (16-bit, 24 kHz, mono)
  • Return transcription frames:
      {"type": "user_transcript",   "text": "..."}
      {"type": "gemini_transcript", "text": "..."}
  • Return tool call frames:
      {"type": "tool_call", "tool": "...", "args": {...}, "result": {...}}
  • Send JSON status frames: ready | processing | done | error

Audio format:
  Input  (client → /ws/chat): raw 16-bit PCM, 16 kHz, mono
  Output (backend → client) : raw 16-bit PCM, 24 kHz, mono

Architecture — why a Queue bridge:
  session.receive() is a PER-TURN async generator. It exhausts (exits) the
  moment turn_complete arrives. You cannot keep iterating it across turns.
  The correct pattern (per Google docs) is:
      while True:
          send(user_input)
          async for response in session.receive():   # one turn
              handle(response)

  We model this with a Queue:
    • receive_loop()  — outer while True, calls session.receive() fresh each turn,
                        handles tool calls inline, pushes responses to outbox.
    • send_loop()     — reads from frontend WebSocket, pushes sends to Gemini.
    • forward_loop()  — reads from outbox Queue, writes to frontend WebSocket.
  All three run concurrently via asyncio.gather(). Any one exiting (e.g. on
  disconnect) cancels the others via the CancelledError propagation in gather.
"""

import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from dotenv import load_dotenv

# ── Configuration ─────────────────────────────────────────────────────────────

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_KEY")
GEMINI_MODEL   = "gemini-2.5-flash-native-audio-preview-12-2025"

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

# ── Tool / Function declarations ───────────────────────────────────────────────

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

GEMINI_CONFIG = {
    "response_modalities": ["AUDIO"],
    "system_instruction": SYSTEM_PROMPT,
    "speech_config": {
        "voice_config": {
            "prebuilt_voice_config": {
                "voice_name": "AOEDE"
            }
        }
    },
    "output_audio_transcription": {},
    "input_audio_transcription":  {},
    "tools": [{"function_declarations": TOOL_DECLARATIONS}],
}

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

# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="MyDrive Gemini Live Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=GEMINI_API_KEY)

# ── Sentinel for signalling the receive loop to exit ──────────────────────────
_STOP = object()

# ── Core receive loop — handles ONE turn, called in a while-True outer loop ───

async def _run_one_turn(gemini_session, outbox: asyncio.Queue) -> bool:
    """
    Drain one complete Gemini turn by iterating session.receive().

    Returns True  — turn finished normally (turn_complete).
    Returns False — session ended / error; caller should stop.

    Tool calls are handled inline: execute → send_tool_response → continue
    iterating so Gemini can generate its spoken confirmation in the same turn.
    """
    try:
        async for response in gemini_session.receive():

            # ── Audio via model_turn parts (safe path — avoids mixed-type crash) ──
            if response.server_content is not None:
                sc = response.server_content

                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            await outbox.put(("bytes", part.inline_data.data))

                if sc.output_transcription and sc.output_transcription.text:
                    await outbox.put(("gemini_transcript", sc.output_transcription.text))

                if sc.input_transcription and sc.input_transcription.text:
                    await outbox.put(("user_transcript", sc.input_transcription.text))

                if sc.turn_complete:
                    await outbox.put(("status", "done"))
                    return True   # ← this turn is finished; outer loop can wait for next send

            # ── Tool call — handle inline and keep iterating for confirmation ──
            if response.tool_call:
                function_responses = []
                for fc in response.tool_call.function_calls:
                    tool_name = fc.name
                    tool_args = dict(fc.args) if fc.args else {}
                    print(f"[tool_call] {tool_name}({tool_args})")
                    result = execute_tool(tool_name, tool_args)

                    await outbox.put(("tool_call", {
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

                # Return results → Gemini continues with spoken confirmation
                # in the *same* turn (still inside this async for loop)
                await gemini_session.send_tool_response(
                    function_responses=function_responses
                )

        # Generator exhausted without turn_complete — session closed by server
        return False

    except Exception as e:
        print(f"[receive_turn] error: {e}")
        await outbox.put(("error", str(e)))
        return False


# ── /ws/text — text input endpoint ────────────────────────────────────────────

@app.websocket("/ws/text")
async def websocket_text_chat(ws: WebSocket):
    await ws.accept()
    print("[/ws/text] Client connected")

    outbox: asyncio.Queue = asyncio.Queue()

    try:
        async with client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            await ws.send_text(json.dumps({"status": "ready"}))

            # ── forward_loop: drains outbox → frontend WebSocket ───────────────
            async def forward_loop():
                while True:
                    item = await outbox.get()
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

            # ── receive_loop: outer while-True, re-calls receive() each turn ──
            async def receive_loop():
                """
                Gemini's session.receive() is a PER-TURN generator — it ends at
                turn_complete. We wrap it in a while-True so after each turn we
                simply wait for the next send from send_loop before calling it
                again. The 'ready_event' coordinates this handshake.
                """
                while True:
                    # Wait until send_loop has sent a message to Gemini
                    await send_event.wait()
                    send_event.clear()

                    alive = await _run_one_turn(gemini_session, outbox)
                    if not alive:
                        await outbox.put(_STOP)
                        break

            # ── send_loop: frontend → Gemini ──────────────────────────────────
            async def send_loop():
                while True:
                    try:
                        data = await ws.receive()
                    except WebSocketDisconnect:
                        await outbox.put(_STOP)
                        break
                    except Exception as e:
                        print(f"[/ws/text send_loop] error: {e}")
                        await outbox.put(_STOP)
                        break

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
                        await gemini_session.send_client_content(
                            turns=[types.Content(role="user", parts=[types.Part(text=user_text)])],
                            turn_complete=True,
                        )
                        await ws.send_text(json.dumps({"status": "processing"}))
                        send_event.set()   # signal receive_loop to start listening

                    elif msg_type == "interrupt":
                        await ws.send_text(json.dumps({"status": "interrupted"}))

            send_event = asyncio.Event()
            await asyncio.gather(forward_loop(), receive_loop(), send_loop())

    except WebSocketDisconnect:
        print("[/ws/text] Client disconnected normally")
    except Exception as e:
        print(f"[/ws/text] Session error: {e}")
        try:
            await ws.send_text(json.dumps({"status": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        print("[/ws/text] Connection closed")


# ── /ws/chat — voice input endpoint ───────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_voice_chat(ws: WebSocket):
    await ws.accept()
    print("[/ws/chat] Client connected")

    outbox: asyncio.Queue = asyncio.Queue()

    try:
        async with client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            await ws.send_text(json.dumps({"status": "ready"}))
            audio_buffer: list[bytes] = []

            async def forward_loop():
                while True:
                    item = await outbox.get()
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
                        print(f"[forward_loop/chat] send error: {fe}")
                        break

            async def receive_loop():
                while True:
                    await send_event.wait()
                    send_event.clear()
                    alive = await _run_one_turn(gemini_session, outbox)
                    if not alive:
                        await outbox.put(_STOP)
                        break

            async def send_loop():
                while True:
                    try:
                        data = await ws.receive()
                    except WebSocketDisconnect:
                        await outbox.put(_STOP)
                        break
                    except Exception as e:
                        print(f"[/ws/chat send_loop] error: {e}")
                        await outbox.put(_STOP)
                        break

                    if "bytes" in data and data["bytes"]:
                        audio_buffer.append(data["bytes"])

                    elif "text" in data and data["text"]:
                        msg = data["text"].strip()

                        if msg == "END_OF_SPEECH":
                            if audio_buffer:
                                combined = b"".join(audio_buffer)
                                audio_buffer.clear()
                                await gemini_session.send_realtime_input(
                                    audio=types.Blob(
                                        data=combined,
                                        mime_type="audio/pcm;rate=16000",
                                    )
                                )
                                await ws.send_text(json.dumps({"status": "processing"}))
                                send_event.set()

                        elif msg == "INTERRUPT":
                            audio_buffer.clear()
                            await ws.send_text(json.dumps({"status": "interrupted"}))

            send_event = asyncio.Event()
            await asyncio.gather(forward_loop(), receive_loop(), send_loop())

    except WebSocketDisconnect:
        print("[/ws/chat] Client disconnected normally")
    except Exception as e:
        print(f"[/ws/chat] Session error: {e}")
        try:
            await ws.send_text(json.dumps({"status": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        print("[/ws/chat] Connection closed")


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": GEMINI_MODEL,
        "endpoints": {
            "voice": "ws://<host>/ws/chat",
            "text":  "ws://<host>/ws/text",
        },
        "tools": [t["name"] for t in TOOL_DECLARATIONS],
    }