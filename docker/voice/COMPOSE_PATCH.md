# Proposed docker-compose.yml addition for nexus-voice

**Not applied yet.** Review, then paste into `docker-compose.yml` below the `operations` / `dev` block. Requires a local build (no public image).

## The service block

```yaml
  # ── Voice Connector ────────────────────────────────────────
  voice:
    build:
      context: ./docker/voice
    container_name: nexus-v2-voice
    restart: unless-stopped
    # Host networking so SIP + RTP flow without Docker NAT.
    # This container does NOT use nexus-internal — it talks to
    # the UCM via host network and exposes HTTP on 127.0.0.1:8100
    # to the API container.
    network_mode: host
    environment:
      SIP_HOST: "10.30.40.2"
      SIP_PORT: "5060"
      SIP_TRANSPORT: "udp"
      SIP_EXTENSION: "4000"
      # SIP_PASSWORD comes from .env — do NOT hardcode
      SIP_PASSWORD: "${VOICE_SIP_PASSWORD}"
      LISTEN_PORT: "5070"
      HTTP_PORT: "8100"
    volumes:
      - voice-tmp:/tmp/nexus_voice
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://127.0.0.1:8100/health"]
      interval: 30s
      timeout: 3s
      retries: 3
```

## Add at the bottom of the `volumes:` block

```yaml
  voice-tmp:
```

## .env addition

```
VOICE_SIP_PASSWORD=*MQ5zkS@t1
```

⚠️ The password is already encrypted in the settings DB from the earlier test. We can ALSO read it from settings on startup instead of passing through .env — pick your preference. `.env` is simpler for Phase 12A; settings DB is cleaner long-term and the spec prefers it.

## API container addition

Add one line to `app` / `api` service environment so the API knows where to post dispatch requests:

```yaml
    environment:
      # ... existing ...
      VOICE_SERVER_URL: "http://10.30.1.106:8100"
```

Note: the IP (`10.30.1.106`) is the appserver host. Because the voice container uses `network_mode: host` it binds directly to the host IP, and because the API container is on `nexus-internal` (not host network), it has to reach the voice container via the host IP, not `127.0.0.1` or `localhost`. If your appserver's LAN IP changes, update this.

Alternative: put the voice container on `nexus-internal` AND host network (not possible), or run a tiny reverse proxy on the host. For Phase 12A the direct LAN IP is fine.

## Running it

```bash
# First time build
docker compose build voice

# Start (after a040 + a041 migrations are applied)
docker compose up -d voice

# Verify it booted cleanly and registered
docker compose logs -f voice

# Health check
curl http://127.0.0.1:8100/health

# Smoke test — dial Tim's mobile directly via the HTTP API
curl -X POST http://127.0.0.1:8100/call \
  -H "Content-Type: application/json" \
  -d '{"to":"817408211712","text":"Nexus voice service is live.","max_seconds":20}'
```

## Stopping it safely

```bash
docker compose stop voice
```

The container unregisters cleanly when it exits — the UCM will see ext 4000 drop out of the registered state within its qualify interval (60 seconds).
