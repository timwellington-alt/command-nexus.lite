"""Nexus STT sidecar — faster-whisper transcription service.

Single endpoint: POST /transcribe (multipart audio file) → JSON
{
    "text": "...",
    "language": "en",
    "duration_ms": 1234,
    "stt_ms": 567
}

The model loads lazily on the first request, then stays resident.
Concurrent requests are serialized via an asyncio lock — small.en
int8 on CPU isn't fast enough to parallelize across requests on
this hardware.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import time
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nexus_stt")

MODEL_NAME = os.environ.get("STT_MODEL", "small.en")
COMPUTE_TYPE = os.environ.get("STT_COMPUTE", "int8")
MODEL_DIR = os.environ.get("HF_HOME", "/models")
MAX_UPLOAD_MB = 25

app = FastAPI(title="Nexus STT")
_model = None
_model_lock = asyncio.Lock()
_inflight = asyncio.Lock()


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("Loading faster-whisper model %s (%s)…", MODEL_NAME, COMPUTE_TYPE)
        t0 = time.monotonic()
        _model = WhisperModel(
            MODEL_NAME, device="cpu", compute_type=COMPUTE_TYPE,
            download_root=MODEL_DIR,
        )
        log.info("Model loaded in %.1fs", time.monotonic() - t0)
    return _model


def _to_wav(src_path: str) -> str:
    """Convert any browser-supplied audio to 16kHz mono WAV via ffmpeg."""
    dst_path = src_path + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path,
         "-ar", "16000", "-ac", "1", "-f", "wav", dst_path],
        check=True,
    )
    return dst_path


@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL_NAME, "loaded": _model is not None}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_MB}MB")

    t_total = time.monotonic()

    suffix = ""
    fname = (audio.filename or "").lower()
    for ext in (".webm", ".ogg", ".mp4", ".m4a", ".wav", ".mp3"):
        if fname.endswith(ext):
            suffix = ext
            break

    with tempfile.NamedTemporaryFile(suffix=suffix or ".bin", delete=False) as tf:
        tf.write(raw)
        src_path = tf.name

    wav_path: Optional[str] = None
    try:
        try:
            wav_path = _to_wav(src_path)
        except subprocess.CalledProcessError as exc:
            raise HTTPException(status_code=400, detail=f"audio decode failed: {exc}")

        async with _model_lock:
            model = _get_model()

        async with _inflight:
            t_stt = time.monotonic()
            segments_iter, info = await asyncio.to_thread(
                model.transcribe,
                wav_path, beam_size=1, vad_filter=True,
                condition_on_previous_text=False,
            )
            text_parts = [seg.text for seg in segments_iter]
            stt_ms = int((time.monotonic() - t_stt) * 1000)

        text = " ".join(p.strip() for p in text_parts).strip()
        return {
            "text": text,
            "language": info.language,
            "duration_ms": int(info.duration * 1000),
            "stt_ms": stt_ms,
            "total_ms": int((time.monotonic() - t_total) * 1000),
        }
    finally:
        for p in (src_path, wav_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
