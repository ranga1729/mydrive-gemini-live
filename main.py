"""
FastAPI WebSocket Proxy for Gemini Live API
==========================================
Architecture:
  Flutter App <--WebSocket--> FastAPI (this file) <--WebSocket--> Gemini Live API

Audio format:
  Input  (Flutter → Backend): raw 16-bit PCM, 16 kHz, mono  (sent as binary frames)
  Output (Backend → Flutter): raw 16-bit PCM, 24 kHz, mono  (sent as binary frames)

Flutter just needs to:
  1. Connect to ws://<host>:8000/ws/chat
  2. Stream raw PCM audio bytes as binary WebSocket frames
  3. Send a text frame "END_OF_SPEECH" when the user stops talking
  4. Receive binary PCM audio frames back and play them
"""

import asyncio
import base64
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

# ── Configuration ────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
GEMINI_MODEL   = "gemini-2.5-flash-native-audio-preview-12-2025"

# 🔧 Replace this with your own system prompt
SYSTEM_PROMPT = (
    "You are a friendly and helpful voice assistant. "
    "Keep your answers concise and conversational. "
    "Respond naturally as if speaking out loud."
)

GEMINI_CONFIG = {
    "response_modalities": ["AUDIO"],
    "system_instruction": SYSTEM_PROMPT,
}

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Gemini Live Voice Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=GEMINI_API_KEY)

# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket):
    """
    Bidirectional WebSocket endpoint.

    Incoming frames (Flutter → server):
      • binary  → raw PCM audio chunk (16-bit, 16 kHz, mono)
      • text    → control messages:
                    "END_OF_SPEECH"  – user finished speaking, flush to Gemini
                    "INTERRUPT"      – cancel current response

    Outgoing frames (server → Flutter):
      • binary  → raw PCM audio chunk from Gemini (16-bit, 24 kHz, mono)
      • text    → JSON status messages, e.g. {"status": "listening"} / {"status": "done"}
    """
    await ws.accept()
    print("[WS] Client connected")

    try:
        async with client.aio.live.connect(
            model=GEMINI_MODEL, config=GEMINI_CONFIG
        ) as gemini_session:

            await ws.send_text(json.dumps({"status": "ready"}))

            # Buffer to accumulate PCM chunks from Flutter before sending to Gemini
            audio_buffer: list[bytes] = []

            async def receive_from_flutter():
                """Read frames from Flutter and forward to Gemini."""
                nonlocal audio_buffer
                while True:
                    try:
                        # Receive either bytes (audio) or str (control)
                        data = await ws.receive()

                        if "bytes" in data and data["bytes"]:
                            # Accumulate raw PCM chunk
                            audio_buffer.append(data["bytes"])

                        elif "text" in data and data["text"]:
                            msg = data["text"].strip()

                            if msg == "END_OF_SPEECH":
                                # Concatenate all buffered audio and send to Gemini
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
                                # Gemini Live handles interruption automatically;
                                # just clear our buffer.
                                await ws.send_text(json.dumps({"status": "interrupted"}))

                    except WebSocketDisconnect:
                        break
                    except Exception as e:
                        print(f"[receive_from_flutter] error: {e}")
                        break

            async def receive_from_gemini():
                """Stream audio chunks from Gemini back to Flutter."""
                try:
                    async for response in gemini_session.receive():
                        # response.data is raw bytes when response modality is AUDIO
                        if response.data is not None:
                            await ws.send_bytes(response.data)

                        # Detect turn completion
                        if (
                            response.server_content is not None
                            and response.server_content.turn_complete
                        ):
                            await ws.send_text(json.dumps({"status": "done"}))

                except Exception as e:
                    print(f"[receive_from_gemini] error: {e}")

            # Run both directions concurrently
            await asyncio.gather(
                receive_from_flutter(),
                receive_from_gemini(),
            )

    except WebSocketDisconnect:
        print("[WS] Client disconnected normally")
    except Exception as e:
        print(f"[WS] Session error: {e}")
        try:
            await ws.send_text(json.dumps({"status": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        print("[WS] Connection closed")


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "model": GEMINI_MODEL}