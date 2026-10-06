# Nexus voice connector — baresip + espeak + piper + sox + tiny Python dispatch server
#
# Runs in host networking mode so SIP + RTP flow cleanly without
# Docker NAT. The proven recipe from Phase 12A milestones 1-2 is
# baked in — config comes from env vars populated by the start
# script, which reads them from the Nexus settings DB via a small
# bootstrap step on startup.
#
# Does NOT have database write access. The API container is
# responsible for writing voice_cdr rows; this container is a thin
# "dial + play WAV" primitive.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    baresip \
    linphone-cli \
    espeak \
    sox \
    python3 \
    python3-aiohttp \
    python3-bcrypt \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Piper TTS (standalone binary + voice models) ────────────────────
# Self-contained binary, no Python deps needed. ~20MB binary + ~60MB
# per high-quality voice model. Models stored in /opt/piper/models/.
RUN mkdir -p /opt/piper/models \
    && curl -sSL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz \
       | tar xz -C /opt/piper --strip-components=1 \
    && chmod +x /opt/piper/piper \
    && /opt/piper/piper --help > /dev/null 2>&1 || true

# Download voice models (each is .onnx + .onnx.json pair)
# Ryan High (~63MB) — clear male, good for alert messages
RUN curl -sSL -o /opt/piper/models/en_US-ryan-high.onnx \
      "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/ryan/high/en_US-ryan-high.onnx" \
    && curl -sSL -o /opt/piper/models/en_US-ryan-high.onnx.json \
      "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/ryan/high/en_US-ryan-high.onnx.json"

# Amy Medium (~30MB) — female voice
RUN curl -sSL -o /opt/piper/models/en_US-amy-medium.onnx \
      "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx" \
    && curl -sSL -o /opt/piper/models/en_US-amy-medium.onnx.json \
      "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx.json"

# Lessac Medium (~30MB) — another clear voice option
RUN curl -sSL -o /opt/piper/models/en_US-lessac-medium.onnx \
      "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx" \
    && curl -sSL -o /opt/piper/models/en_US-lessac-medium.onnx.json \
      "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json"

ENV PATH="/opt/piper:${PATH}"
# ─────────────────────────────────────────────────────────────────────

ENV HOME=/root
RUN mkdir -p /root/.baresip /tmp/nexus_voice

# Pre-generate the default baresip config so the server can just overwrite it on startup.
# NOTE: baresip 1.0.0 cannot find its config dir without an explicit -f flag, even with HOME set.
RUN ( printf 'q\n' ) | timeout 4 baresip -f /root/.baresip >/dev/null 2>&1 || true \
    && test -s /root/.baresip/config

COPY server.py /app/server.py
COPY ivr.py /app/ivr.py
RUN chmod +x /app/server.py

EXPOSE 8100

CMD ["python3", "/app/server.py"]
