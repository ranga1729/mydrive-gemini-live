"""
FastAPI WebSocket Proxy for Gemini Live API — MyDrive Edition
=============================================================
Two endpoints:

  /ws/chat   — voice input  (client sends binary PCM + "END_OF_SPEECH" control)
  /ws/text   — text input   (client sends plain text messages as JSON)

Both endpoints:
  • Use the MyDrive intent-classification system prompt
  • Return audio responses as binary PCM frames (16-bit, 24 kHz, mono)
  • Return a JSON intent card frame after each turn completes:
      {"type": "intent", "query": "...", "intent": "...", "confidence": 0.0,
       "is_final": bool, "reply_to_user": "...", "entities": {...}}
  • Send JSON status frames: ready | processing | done | error

Audio format:
  Input  (client → /ws/chat): raw 16-bit PCM, 16 kHz, mono
  Output (backend → client) : raw 16-bit PCM, 24 kHz, mono
"""

import asyncio
import json
import os
import re

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
You are a helpful AI assistant for a automobile service app called 'MyDrive'.
Your goal is to engage  in conversation with the user and ask follow up questions to 
classify the user's input into a specific actionable intent at the end of the conversation.

AVAILABLE INTENTS:
1. ROADSIDE_ASSISTANCE (flat tires, dead battery, minor roadside help)
2. TOW_TRUCK_REQUIRED (accidents, non-starting cars, major failures, smoke)
3. SPARE_PARTS_SEARCH (looking for specific parts like glass, mirrors, engine components)
4. GARAGE_BOOKING (routine service, unusual sounds needing inspection, appointments)
5. GREETING (hello, hi, hey, good morning)
6. UNCLEAR (vague requests, off-topic, or not enough info to classify)

Respond ONLY in this JSON format:
{
  "query": "Exact transcription of user input here",
  "intent": "ONE_OF_THE_LABELS_ABOVE",
  "confidence": 0.00,
  "is_final": false,
  "reply_to_user": "Your conversational response here",
  "entities": {"part_name": null}
}
Do not include any introductory text, reasoning, markdown fences, or explanations. Just the JSON object.

INSTRUCTIONS:
- If the user's intent is clear and fits categories 1-4, set "is_final" to true, unless it must be false.
- "reply_to_user" should be a natural, human-like response. This will also be spoken aloud, so write it in a conversational, spoken style.
    - If the intent is clear: Confirm the action (e.g., "I see you need a tow truck. I can help with that.").
    - If the intent is unclear: Ask a clarifying question (e.g., "Could you tell me more? Is the car completely stopped or just having a flat tyre?").
- "query" must be a clean transcription of exactly what the user asked, with no added interpretation.

CRITICAL INSTRUCTIONS:
- Ensure all string values are properly escaped.
- If you repeat user input containing quotes (like "It's"), ensure the JSON remains valid.
- DO NOT include any text other than the JSON object.
- The response you speak aloud must match "reply_to_user" exactly.
"""

GEMINI_CONFIG = {
    "response_modalities": ["AUDIO"],
    "system_instruction": SYSTEM_PROMPT,
    "speech_config": {
        "voice_config": {
            "prebuilt_voice_config": {
                "voice_name": "AOEDE"  # Options: KORE, PUCK, AOEDE, CHARON, etc.
            }
        }
    },
    "output_audio_transcription": {}   # Receive spoken-text transcript from Gemini
}

# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="MyDrive Gemini Live Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=GEMINI_API_KEY)

# ── JSON extraction helper ─────────────────────────────────────────────────────

def extract_json_payload(text: str) -> dict | None:
    clean = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    match = re.search(r"\{[\s\S]*\}", clean)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None

# ── Shared audio+transcript streamer ──────────────────────────────────────────

async def _stream_gemini_to_client(
    ws: WebSocket,
    gemini_session,
    user_query: str = "",
) -> None:
    """
    Reads audio chunks and transcription from an active Gemini Live session.
    - Binary PCM frames are forwarded immediately as binary WebSocket frames.
    - When a turn completes, the accumulated transcript is parsed as JSON,
      the "query" field is set to user_query, and the intent card is sent
      as a JSON text frame with type="intent".
    - A {"status": "done"} frame is sent last so the frontend knows to
      stop the thinking indicator.
    """
    transcript_buffer = []

    try:
        async for response in gemini_session.receive():

            # ── Audio chunks ──────────────────────────────────────────────────
            if response.data is not None:
                await ws.send_bytes(response.data)

            # ── Output audio transcription (spoken text from Gemini) ──────────
            if response.server_content is not None:
                sc = response.server_content

                # Accumulate transcript segments
                if sc.output_transcription and sc.output_transcription.text:
                    transcript_buffer.append(sc.output_transcription.text)

                # Turn complete — parse + forward the intent card
                if sc.turn_complete:
                    full_transcript = "".join(transcript_buffer).strip()
                    transcript_buffer.clear()

                    intent_payload = extract_json_payload(full_transcript) if full_transcript else None

                    if intent_payload:
                        # Overwrite query with what the user actually sent
                        if user_query:
                            intent_payload["query"] = user_query
                        intent_payload["type"] = "intent"
                        await ws.send_text(json.dumps(intent_payload))
                    else:
                        # Fallback: Gemini didn't return clean JSON — surface raw text
                        fallback = {
                            "type": "intent",
                            "query": user_query,
                            "intent": "UNCLEAR",
                            "confidence": 0.0,
                            "is_final": False,
                            "reply_to_user": full_transcript or "I didn't quite catch that.",
                            "entities": {},
                        }
                        await ws.send_text(json.dumps(fallback))

                    await ws.send_text(json.dumps({"status": "done"}))

    except Exception as e:
        print(f"[gemini->client] stream error: {e}")


# ── /ws/chat  — voice input endpoint ──────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_voice_chat(ws: WebSocket):
    """
    Voice input endpoint.

    Client → server frames:
      binary          : raw PCM audio (16-bit, 16 kHz, mono)
      "END_OF_SPEECH" : user finished speaking, flush buffer to Gemini
      "INTERRUPT"     : discard buffered audio

    Server → client frames:
      binary  : raw PCM audio from Gemini (16-bit, 24 kHz, mono)
      JSON    : {"status": "ready"|"processing"|"done"|"interrupted"|"error"}
      JSON    : {"type": "intent", "query": "...", "intent": "...", ...}
    """
    await ws.accept()
    print("[/ws/chat] Client connected")

    try:
        async with client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            await ws.send_text(json.dumps({"status": "ready"}))
            audio_buffer: list[bytes] = []
            current_query: list[str] = [""]  # mutable container for closure

            async def receive_from_client():
                while True:
                    try:
                        data = await ws.receive()

                        if "bytes" in data and data["bytes"]:
                            audio_buffer.append(data["bytes"])

                        elif "text" in data and data["text"]:
                            msg = data["text"].strip()

                            if msg == "END_OF_SPEECH":
                                if audio_buffer:
                                    combined = b"".join(audio_buffer)
                                    audio_buffer.clear()
                                    # We don't have a text transcription of the voice input here,
                                    # so set a placeholder; Gemini's "query" field fills it in.
                                    current_query[0] = "[voice input]"
                                    await gemini_session.send_realtime_input(
                                        audio=types.Blob(
                                            data=combined,
                                            mime_type="audio/pcm;rate=16000",
                                        )
                                    )
                                    await ws.send_text(json.dumps({"status": "processing"}))

                            elif msg == "INTERRUPT":
                                audio_buffer.clear()
                                await ws.send_text(json.dumps({"status": "interrupted"}))

                    except WebSocketDisconnect:
                        break
                    except Exception as e:
                        print(f"[/ws/chat receive] error: {e}")
                        break

            async def stream_wrapper():
                # Pass a lambda so the streamer always reads the latest query value
                await _stream_gemini_to_client(ws, gemini_session,
                                               user_query=current_query[0])

            await asyncio.gather(receive_from_client(), stream_wrapper())

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


# ── /ws/text  — text input endpoint ───────────────────────────────────────────

@app.websocket("/ws/text")
async def websocket_text_chat(ws: WebSocket):
    """
    Text input endpoint.

    Client → server frames (JSON text only):
      {"type": "message", "text": "Hello Gemini!"}
      {"type": "interrupt"}

    Server → client frames:
      binary  : raw PCM audio from Gemini (16-bit, 24 kHz, mono)
      JSON    : {"status": "ready"|"processing"|"done"|"error"|"interrupted"}
      JSON    : {"type": "intent", "query": "...", "intent": "...", ...}
    """
    await ws.accept()
    print("[/ws/text] Client connected")

    try:
        async with client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            await ws.send_text(json.dumps({"status": "ready"}))
            current_query: list[str] = [""]

            async def receive_text_from_client():
                while True:
                    try:
                        data = await ws.receive()
                        if "text" not in data or not data["text"]:
                            continue

                        payload = json.loads(data["text"])
                        msg_type = payload.get("type", "message")

                        if msg_type == "message":
                            user_text = payload.get("text", "").strip()
                            if not user_text:
                                continue

                            current_query[0] = user_text

                            await gemini_session.send_client_content(
                                turns=[
                                    types.Content(
                                        role="user",
                                        parts=[types.Part(text=user_text)],
                                    )
                                ],
                                turn_complete=True,
                            )
                            await ws.send_text(json.dumps({"status": "processing"}))

                        elif msg_type == "interrupt":
                            await ws.send_text(json.dumps({"status": "interrupted"}))

                    except WebSocketDisconnect:
                        break
                    except json.JSONDecodeError:
                        await ws.send_text(
                            json.dumps({"status": "error", "message": "Invalid JSON payload"})
                        )
                    except Exception as e:
                        print(f"[/ws/text receive] error: {e}")
                        break

            async def stream_wrapper():
                await _stream_gemini_to_client(ws, gemini_session,
                                               user_query=current_query[0])

            await asyncio.gather(receive_text_from_client(), stream_wrapper())

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
    }