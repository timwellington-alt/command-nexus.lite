FROM python:3.12-slim

WORKDIR /app

# Install system dependencies:
#   build-essential   — cffi/cryptography native builds
#   fonts-dejavu-core — Pillow needs TrueType fonts for the Brother QL
#                       label renderer. Without these PIL falls back to
#                       its 10-pixel bitmap default font and every text
#                       size parameter is silently ignored, producing
#                       microscopic labels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    fonts-dejavu-core \
    cups-client \
    ffmpeg \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    nmap \
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && setcap cap_net_raw,cap_net_admin,cap_net_bind_service=+eip /usr/bin/nmap

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir paramiko>=3.4

# Create non-root user
RUN groupadd -r nexus && useradd -r -g nexus -d /app -s /sbin/nologin nexus

# Copy application
COPY . .

# Create data directories
RUN mkdir -p /app/data/photos /app/data/imports \
    && chown -R nexus:nexus /app

# Run as non-root
USER nexus

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# API entrypoint (override in compose for worker/scheduler)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
