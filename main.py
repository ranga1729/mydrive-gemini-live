"""
FastAPI WebSocket Proxy for Gemini Live API
==========================================
Two endpoints:

  /ws/chat   — voice input  (Flutter sends binary PCM + "END_OF_SPEECH" control)
  /ws/text   — text input   (Flutter sends plain text messages as JSON)

Both endpoints:
  • Use the same system prompt
  • Return audio responses as binary PCM frames (16-bit, 24 kHz, mono)
  • Send JSON status frames: ready | processing | done | error

Audio format:
  Input  (Flutter → /ws/chat): raw 16-bit PCM, 16 kHz, mono
  Output (Backend → Flutter) : raw 16-bit PCM, 24 kHz, mono
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

load_dotenv ()

GEMINI_API_KEY = os.environ.get("GEMINI_KEY")
GEMINI_MODEL   = "gemini-2.5-flash-native-audio-preview-12-2025"

# 🔧 Replace this with your own system prompt — applies to BOTH endpoints
SYSTEM_PROMPT = (
  "You are a helpful assistant fluent in English, Sinhala, and Tamil. "
  "If the user speaks in Sinhala, respond in Sinhala. "
  "Use a natural, conversational local dialect for both languages."
)

GEMINI_CONFIG = {
    "response_modalities": ["AUDIO"],
    "system_instruction": SYSTEM_PROMPT,
    "speech_config": {
      "voice_config": {
        "prebuilt_voice_config": {
          "voice_name": "AOEDE"  # Options include: KORE, PUCK, AOEDE, CHARON, etc.
        }
      }
    },
    "output_audio_transcription": {}  
}

# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="Gemini Live Voice Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=GEMINI_API_KEY)

# ── Shared helper ──────────────────────────────────────────────────────────────

async def _stream_gemini_audio_to_client(ws: WebSocket, gemini_session) -> None:
    """
    Continuously reads audio chunks from an active Gemini Live session
    and forwards them as binary WebSocket frames to the Flutter client.
    Sends {"status": "done"} when a turn completes.
    """
    try:
        async for response in gemini_session.receive():
            if response.data is not None:
                await ws.send_bytes(response.data)

            if (
                response.server_content is not None
                and response.server_content.turn_complete
            ):
                await ws.send_text(json.dumps({"status": "done"}))

    except Exception as e:
        print(f"[gemini->flutter] stream error: {e}")

# ── /ws/chat  — voice input endpoint ──────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_voice_chat(ws: WebSocket):
    """
    Voice input endpoint.

    Flutter -> server frames:
      binary          : raw PCM audio (16-bit, 16 kHz, mono)
      "END_OF_SPEECH" : user finished speaking, flush buffer to Gemini
      "INTERRUPT"     : discard buffered audio

    Server -> Flutter frames:
      binary  : raw PCM audio from Gemini (16-bit, 24 kHz, mono)
      JSON    : {"status": "ready"|"processing"|"done"|"interrupted"|"error"}
    """
    await ws.accept()
    print("[/ws/chat] Client connected")

    try:
        async with client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            await ws.send_text(json.dumps({"status": "ready"}))
            audio_buffer: list[bytes] = []

            async def receive_from_flutter():
                nonlocal audio_buffer
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
                                    audio_buffer = []
                                    await gemini_session.send_realtime_input(
                                        audio=types.Blob(
                                            data=combined,
                                            mime_type="audio/pcm;rate=16000",
                                        )
                                    )
                                    await ws.send_text(json.dumps({"status": "processing"}))

                            elif msg == "INTERRUPT":
                                audio_buffer = []
                                await ws.send_text(json.dumps({"status": "interrupted"}))

                    except WebSocketDisconnect:
                        break
                    except Exception as e:
                        print(f"[/ws/chat receive] error: {e}")
                        break

            await asyncio.gather(
                receive_from_flutter(),
                _stream_gemini_audio_to_client(ws, gemini_session),
            )

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

    Flutter -> server frames (JSON text only):
      {"type": "message", "text": "Hello Gemini!"}
      {"type": "interrupt"}   <- optional: acknowledge a stop request

    Server -> Flutter frames:
      binary  : raw PCM audio from Gemini (16-bit, 24 kHz, mono)
      JSON    : {"status": "ready"|"processing"|"done"|"error"|"interrupted"}

    Gemini receives the user's text and responds with AUDIO only,
    which is streamed back as binary PCM frames to Flutter.
    """
    await ws.accept()
    print("[/ws/text] Client connected")

    try:
        async with client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            await ws.send_text(json.dumps({"status": "ready"}))

            async def receive_text_from_flutter():
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

                            # Send the typed text to Gemini Live as a user turn
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

            await asyncio.gather(
                receive_text_from_flutter(),
                _stream_gemini_audio_to_client(ws, gemini_session),
            )

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