# Nexus STT sidecar — faster-whisper small.en int8 on CPU.
#
# Lazy model load on first /transcribe; kept resident afterward.
# ~250MB model, ~600MB RSS once warm.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models \
    HF_HUB_DISABLE_TELEMETRY=1

# ffmpeg is needed because the browser sends WebM/Opus blobs that
# faster-whisper cannot decode directly — we shell out to convert.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

# Pre-download the model into the image so first request isn't slow.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small.en', device='cpu', compute_type='int8', download_root='/models')"

EXPOSE 8200
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8200"]
