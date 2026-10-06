#!/usr/bin/env python3
"""
Nexus voice dispatch server — minimal HTTP API that sits in front of
linphonec (primary) / baresip (legacy fallback) and takes `{to, text}`
dial requests from the Nexus API container.

Endpoints:
    GET  /health                -- aliveness check
    POST /call                  -- place an outbound call:
                                    body = {
                                        "to": "817408211712",   # full dialed number
                                        "text": "Nexus alert...", # TTS text
                                        "call_id": "uuid-or-int", # external ref for CDR
                                        "max_seconds": 35         # overall timeout
                                    }
                                    returns {
                                        "ok": bool,
                                        "call_outcome": "answered"|"no_answer"|"failed",
                                        "duration_seconds": int,
                                        "error": str|None,
                                        "log_excerpt": str
                                    }
    POST /ivr/whitelist         -- push IVR caller whitelist:
                                    body = {
                                        "recipients": [
                                            {"number": "+17408211712",
                                             "label": "Tim",
                                             "pin_hash": "$2b$12$..."},
                                            ...
                                        ]
                                    }

The server does NOT touch the Postgres database. The Nexus API
container owns CDR rows and uses POST /call as a pure dial
primitive.

A persistent linphonec subprocess runs alongside the HTTP server,
registered to the UCM for inbound IVR calls. Outbound /call
requests use the same persistent process (commands via stdin).

Configuration via environment variables at startup (typically
populated from the Nexus settings DB by an entrypoint script, or
passed through docker-compose `environment:`):

    SIP_HOST           UCM IP (e.g. 10.30.40.2)
    SIP_PORT           default 5060
    SIP_TRANSPORT      default udp
    SIP_EXTENSION      registered extension (e.g. 4000)
    SIP_PASSWORD       SIP auth password
    LISTEN_PORT        baresip local listen port (default 5070)
    HTTP_PORT          this server's HTTP port (default 8100)
    NEXUS_API_URL      URL of Nexus API for status queries (e.g. http://host.docker.internal:8080)
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import web

import ivr


@web.middleware
async def strip_server_header(request, handler):
    """Strip the default aiohttp 'Server' header to avoid leaking version info."""
    resp = await handler(request)
    resp.headers["Server"] = "Python"
    return resp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nexus_voice")


SIP_HOST = os.environ.get("SIP_HOST", "10.30.40.2")
SIP_PORT = int(os.environ.get("SIP_PORT", "5060"))
SIP_TRANSPORT = os.environ.get("SIP_TRANSPORT", "udp").lower()
SIP_EXTENSION = os.environ.get("SIP_EXTENSION", "4000")
SIP_PASSWORD = os.environ.get("SIP_PASSWORD", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "5070"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8100"))

# Shared secret required in X-Voice-Secret header on every /call request.
# Without this, the voice container's /call endpoint is an unauthenticated
# phone-dial-anywhere primitive reachable from anywhere on the host's
# network. REQUIRED at startup — we refuse to run without it so a misconfig
# can never leave the endpoint open.
VOICE_API_SECRET = os.environ.get("VOICE_API_SECRET", "")

NEXUS_API_URL = os.environ.get("NEXUS_API_URL", "")

BARESIP_CONFIG_DIR = Path("/root/.baresip")


# Single mutex — one call at a time, matches UCM singleton lesson.
_CALL_LOCK = asyncio.Lock()

# Persistent baresip manager — initialised in main() / on_startup
_baresip: "ivr.BaresipManager | None" = None


def _write_baresip_config():
    """Write configs for both baresip (legacy fallback) and linphonec (primary).

    baresip config is kept for the legacy _run_baresip_call() outbound path.
    linphonec config at /root/.linphonerc is the primary IVR engine.
    """
    # ── linphonec config (primary) ────────────────────────────────
    # Minimal config — just SIP port + IPv6. Registration and
    # soundcard are set via interactive commands at startup because
    # config-file-based registration doesn't work reliably with
    # linphonec 5.2.0 when combined with interactive commands.
    linphonerc = Path("/root/.linphonerc")
    linphonerc.write_text(
        "[sip]\n"
        f"sip_port={LISTEN_PORT}\n"
        "use_ipv6=0\n"
    )
    linphonerc.chmod(0o600)
    # Create the linphone data directory to suppress DB errors
    Path("/root/.local/share/linphone").mkdir(parents=True, exist_ok=True)
    log.info("wrote linphonec config at %s", linphonerc)

    # ── baresip config (legacy fallback for _run_baresip_call) ────
    BARESIP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    accounts = BARESIP_CONFIG_DIR / "accounts"
    config = BARESIP_CONFIG_DIR / "config"

    # accounts — answermode=auto so baresip auto-answers inbound calls
    accounts.write_text(
        f'<sip:{SIP_EXTENSION}@{SIP_HOST}>'
        f';auth_pass="{SIP_PASSWORD}"'
        f';regint=60'
        f';answermode=auto'
        f';outbound="sip:{SIP_HOST}:{SIP_PORT};transport={SIP_TRANSPORT}"\n'
    )
    accounts.chmod(0o600)

    # If baresip has never run in this container, trigger its default config gen
    if not config.exists():
        subprocess.run(
            ["bash", "-c",
             f"( printf 'q\\n' ) | timeout 4 baresip -f {BARESIP_CONFIG_DIR}"],
            capture_output=True, timeout=10,
        )
    if not config.exists():
        # Non-fatal — linphonec is primary now, baresip is just fallback
        log.warning("baresip failed to generate default config at %s (non-fatal, linphonec is primary)", config)
        return

    # Override the listen port + audio modules (for legacy outbound path)
    if config.exists():
        txt = config.read_text()
        if "\n#sip_listen" in txt:
            txt = txt.replace(
                "\n#sip_listen		0.0.0.0:5060",
                f"\nsip_listen 0.0.0.0:{LISTEN_PORT}",
            )
        elif "\nsip_listen" not in txt:
            txt += f"\nsip_listen 0.0.0.0:{LISTEN_PORT}\n"
        txt = txt.replace(
            "audio_source		alsa,default",
            "audio_source aufile,/tmp/nexus_voice/silence.wav",
        )
        txt = txt.replace(
            "audio_player		alsa,default",
            "audio_player aufile,/tmp/nexus_voice/discard.wav",
        )
        silence_path = Path("/tmp/nexus_voice/silence.wav")
        if not silence_path.exists():
            silence_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["sox", "-n", "-r", "8000", "-c", "1", "-b", "16",
                 str(silence_path), "trim", "0", "1"],
                capture_output=True, timeout=5,
            )
        txt = txt.replace(
            "audio_alert		alsa,default",
            "#audio_alert disabled",
        )
        txt = txt.replace("\nmodule			alsa.so", "\n#module alsa.so")
        txt = txt.replace("\n#module			aufile.so", "\nmodule			aufile.so")
        txt = txt.replace("\n#module_app		ctrl_tcp.so", "\nmodule_app		ctrl_tcp.so")
        txt = txt.replace("\n#module			ctrl_tcp.so", "\nmodule			ctrl_tcp.so")
        txt = re.sub(
            r"ctrl_tcp_listen\s+.*",
            "ctrl_tcp_listen 127.0.0.1:4444",
            txt,
        )
        if "ctrl_tcp_listen" not in txt:
            txt += "\nctrl_tcp_listen 127.0.0.1:4444\n"
        if "answermode" not in txt:
            txt += "\nanswermode auto\n"
        else:
            import re as _re
            txt = _re.sub(r"answermode\s+\w+", "answermode auto", txt)
        config.write_text(txt)


_PIPER_BIN = "/opt/piper/piper"
_PIPER_MODELS_DIR = "/opt/piper/models"


def _render_tts(text: str, out_wav: str, voice: str = "espeak") -> bool:
    """Render text to an 8kHz mono 16-bit WAV.

    ``voice`` is the tts_voice setting value:
      - ``"espeak"``                 → espeak engine
      - ``"piper:en_US-ryan-high"``  → Piper with named model
      - ``"google:en-US-Neural2-D"`` → Google (not implemented yet)

    Returns True on success. On failure, False and an error is logged.
    """
    engine = voice.split(":")[0] if ":" in voice else voice
    model_name = voice.split(":", 1)[1] if ":" in voice else ""

    if engine == "piper":
        return _render_piper(text, out_wav, model_name)
    if engine == "google":
        log.warning("Google TTS not implemented yet — falling back to espeak")
        return _render_espeak(text, out_wav)
    return _render_espeak(text, out_wav)


def _render_espeak(text: str, out_wav: str) -> bool:
    """espeak → sox resample to 8kHz mono 16-bit."""
    try:
        espeak = subprocess.Popen(
            ["espeak", "-v", "en-us", "-s", "140", "--stdout", text],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        sox = subprocess.run(
            ["sox", "-", "-r", "8000", "-c", "1", "-b", "16", out_wav],
            stdin=espeak.stdout,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if espeak.stdout:
            espeak.stdout.close()
        espeak.wait(timeout=5)
        if sox.returncode != 0:
            log.error("sox failed: %s", sox.stderr.decode(errors="replace")[:500])
            return False
        return os.path.exists(out_wav) and os.path.getsize(out_wav) > 0
    except Exception as e:
        log.exception("espeak TTS render failed: %s", e)
        return False


def _render_piper_native(text: str, out_wav: str, model_name: str) -> bool:
    """Render Piper to its native sample rate (typically 22050Hz) WAV.

    Used by the chat /tts endpoint where we want full quality, not
    the 8kHz SIP-grade output _render_piper produces.
    """
    model_path = os.path.join(_PIPER_MODELS_DIR, f"{model_name}.onnx")
    if not os.path.exists(model_path):
        log.error("Piper model not found: %s", model_path)
        return False
    if not os.path.exists(_PIPER_BIN):
        log.error("Piper binary not found: %s", _PIPER_BIN)
        return False
    try:
        proc = subprocess.run(
            [_PIPER_BIN, "--model", model_path, "--output_file", out_wav],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if proc.returncode != 0:
            log.error("piper failed: %s", proc.stderr.decode(errors="replace")[:500])
            return False
        return os.path.exists(out_wav) and os.path.getsize(out_wav) > 0
    except Exception as e:
        log.exception("Piper native TTS render failed: %s", e)
        return False


def _render_piper(text: str, out_wav: str, model_name: str) -> bool:
    """Piper TTS → sox resample to 8kHz mono 16-bit.

    Piper outputs 22050Hz by default (depends on model). We pipe
    through sox to get the 8kHz mono that baresip needs, same as espeak.
    """
    model_path = os.path.join(_PIPER_MODELS_DIR, f"{model_name}.onnx")
    if not os.path.exists(model_path):
        log.error("Piper model not found: %s", model_path)
        return False
    if not os.path.exists(_PIPER_BIN):
        log.error("Piper binary not found: %s", _PIPER_BIN)
        return False
    try:
        # Piper reads text from stdin, outputs raw WAV to stdout
        piper = subprocess.Popen(
            [_PIPER_BIN, "--model", model_path, "--output-raw"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # Piper outputs raw PCM — need to tell sox the input format
        # Piper default: 16-bit signed LE, mono, sample rate from model
        # (typically 22050Hz). Use -t raw -r 22050 -e signed -b 16 -c 1
        sox = subprocess.run(
            ["sox", "-t", "raw", "-r", "22050", "-e", "signed", "-b", "16", "-c", "1", "-",
             "-r", "8000", "-c", "1", "-b", "16", out_wav],
            input=piper.communicate(input=text.encode("utf-8"), timeout=15)[0],
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if sox.returncode != 0:
            log.error("sox (piper) failed: %s", sox.stderr.decode(errors="replace")[:500])
            return False
        return os.path.exists(out_wav) and os.path.getsize(out_wav) > 0
    except Exception as e:
        log.exception("Piper TTS render failed: %s", e)
        return False


# Regex for baresip 1.0.0 DTMF event log lines. Confirmed format on
# 2026-04-12: "received event: '1' (end=0)" / "(end=1)" — each key
# press produces BOTH a start (end=0) and an end (end=1) line. We
# only count (end=1) to avoid double-counting.
_DTMF_RE = re.compile(r"received event: '([0-9*#])' \(end=1\)")


def _run_baresip_call(wav_path: str, to_number: str, max_seconds: int) -> tuple[bool, str, int, str | None]:
    """Run baresip once to place a single outbound call playing wav_path.

    Returns (success, log_excerpt, duration_seconds, first_dtmf_key).
    first_dtmf_key is the first DTMF key (0-9, *, #) pressed AFTER
    the call was established, or None if no DTMF was received.
    """
    config = BARESIP_CONFIG_DIR / "config"
    if not config.exists():
        return False, "config missing", 0, None

    # Override audio_source for THIS call
    txt = config.read_text()
    # Remove any existing audio_source line and add our aufile source
    new_lines = []
    for line in txt.split("\n"):
        if line.strip().startswith("audio_source"):
            continue
        if line.strip().startswith("#audio_source"):
            continue
        new_lines.append(line)
    new_lines.append(f"audio_source aufile,{wav_path}")
    config.write_text("\n".join(new_lines))

    # Ensure discard target exists (baresip aufile player needs a writable path)
    Path("/tmp/nexus_voice/discard.wav").parent.mkdir(parents=True, exist_ok=True)

    # Spawn baresip with /dial command
    started = time.time()
    try:
        cmd = [
            "bash", "-c",
            "( sleep {m} ; printf 'q\\n' ) | timeout --foreground {t} "
            "baresip -f {d} -e {e} 2>&1".format(
                m=max(max_seconds - 3, 5),
                t=max_seconds,
                d=shlex.quote(str(BARESIP_CONFIG_DIR)),
                e=shlex.quote(f"/dial {to_number}"),
            ),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=max_seconds + 5)
        duration = int(time.time() - started)
        output = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return False, "baresip subprocess timeout", int(time.time() - started), None
    except Exception as e:
        return False, f"baresip subprocess error: {e}", int(time.time() - started), None

    # Analyze output — walk lines in order so we can correlate DTMF
    # events with the call-established state.
    success = False
    after_establish = False
    first_dtmf: str | None = None
    excerpt_lines = []
    for line in output.split("\n"):
        stripped = line.strip()
        if "Call established:" in stripped:
            success = True
            after_establish = True
        if after_establish and first_dtmf is None:
            m = _DTMF_RE.search(stripped)
            if m:
                first_dtmf = m.group(1)
        if any(s in stripped.lower() for s in (
            "register", "200 ok", "sip progress", "call established",
            "terminated", "aufile", "error", "fail", "busy", "reg:",
            "404", "403", "486", "487", "503", "received event",
        )):
            excerpt_lines.append(stripped)
    return success, "\n".join(excerpt_lines[-50:]), duration, first_dtmf


def _check_secret(request: web.Request) -> bool:
    """Constant-time compare the request's X-Voice-Secret header against
    the configured VOICE_API_SECRET. Returns True if the header is present
    and matches. Uses hmac.compare_digest to avoid timing leaks on the
    comparison.
    """
    if not VOICE_API_SECRET:
        return False
    provided = request.headers.get("X-Voice-Secret", "")
    return hmac.compare_digest(provided, VOICE_API_SECRET)


async def handle_health(request: web.Request) -> web.Response:
    # Health check is intentionally unauthenticated — it's used by
    # docker-compose healthcheck which doesn't have the secret. It
    # returns NOTHING that leaks config (no sip_host, no extension,
    # no listen_port) — just a liveness beacon.
    return web.json_response({"ok": True})


async def handle_call(request: web.Request) -> web.Response:
    if not _check_secret(request):
        remote = request.remote or "?"
        log.warning("auth_fail: /call from %s missing/wrong X-Voice-Secret", remote)
        return web.json_response(
            {"ok": False, "error": "unauthorized"},
            status=401,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    to_number = body.get("to", "").strip()
    text = body.get("text", "").strip()
    call_id = body.get("call_id") or str(uuid.uuid4())
    max_seconds = int(body.get("max_seconds", 35))

    if not to_number or not text:
        return web.json_response({"ok": False, "error": "'to' and 'text' are required"}, status=400)
    if not to_number.isdigit() and not (to_number.startswith("+") and to_number[1:].isdigit()):
        return web.json_response({"ok": False, "error": "'to' must be a digit string"}, status=400)

    log.info("call request: id=%s to=%s text=%r", call_id, to_number, text[:80])

    # Mutex — one call at a time. Blocking wait is fine; callers can queue.
    async with _CALL_LOCK:
        with tempfile.NamedTemporaryFile(
            suffix=".wav", dir="/tmp/nexus_voice", delete=False
        ) as tf:
            wav_path = tf.name
        try:
            voice = body.get("voice", "espeak")
            if not await asyncio.to_thread(_render_tts, text, wav_path, voice):
                return web.json_response(
                    {"ok": False, "call_outcome": "failed",
                     "error": "TTS render failed", "log_excerpt": "",
                     "ack_dtmf": None},
                    status=500,
                )

            # Lazy-render the DTMF confirmation prompts in the same
            # voice as the call summary. Files are cached on disk by
            # voice-hash, so the second call with the same voice is
            # near-instant.
            confirmation_wavs = await asyncio.to_thread(
                _ensure_confirmation_wavs, voice,
            )

            # Route through persistent baresip if available,
            # fall back to legacy per-call spawn.
            if _baresip is not None:
                success, log_excerpt, duration, ack_dtmf = (
                    await _baresip.place_outbound_call(
                        wav_path, to_number, max_seconds,
                        confirmation_wavs=confirmation_wavs,
                    )
                )
            else:
                success, log_excerpt, duration, ack_dtmf = await asyncio.to_thread(
                    _run_baresip_call, wav_path, to_number, max_seconds,
                )

            # Classification:
            #   acked       — call established AND recipient pressed 1
            #   escalated   — call established AND recipient pressed 9
            #   answered    — call established, no DTMF received (could
            #                 be a human that didn't press, OR voicemail
            #                 which picked up and silently held)
            #   no_answer   — call never established (timeout, declined,
            #                 network error, etc.)
            if success and ack_dtmf == "1":
                outcome = "acked"
            elif success and ack_dtmf == "9":
                outcome = "escalated"
            elif success:
                outcome = "answered"
            else:
                outcome = "no_answer"
            result = {
                "ok": outcome in ("acked", "answered"),
                "call_id": call_id,
                "call_outcome": outcome,
                "duration_seconds": duration,
                "ack_dtmf": ack_dtmf,
                "error": None if success else "call did not establish",
                "log_excerpt": log_excerpt,
            }
            log.info("call result: id=%s outcome=%s dtmf=%s dur=%ds",
                     call_id, outcome, ack_dtmf, duration)
            return web.json_response(result)
        finally:
            try:
                os.unlink(wav_path)
            except Exception:
                pass


async def handle_preview(request: web.Request) -> web.Response:
    """Render TTS to WAV and return the audio bytes. No phone call.

    Auth required — the voice container's LAN-facing port would
    otherwise be an open TTS render farm for anyone on the network.
    The Nexus API proxy at /api/voice/tts-preview passes the secret.
    """
    if not _check_secret(request):
        remote = request.remote or "?"
        log.warning("auth_fail: /preview from %s", remote)
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    text = (body.get("text") or "").strip()
    if not text:
        text = "This is a preview of the Nexus voice alert system."

    with tempfile.NamedTemporaryFile(suffix=".wav", dir="/tmp/nexus_voice", delete=False) as tf:
        wav_8k = tf.name
    wav_48k = wav_8k.replace(".wav", "_48k.wav")
    try:
        voice = (body.get("voice") or "espeak")
        ok = await asyncio.to_thread(_render_tts, text, wav_8k, voice)
        if not ok or not os.path.exists(wav_8k):
            return web.json_response({"ok": False, "error": "TTS render failed"}, status=500)
        # Upsample 8kHz → 48kHz for browser playback
        proc = await asyncio.to_thread(
            subprocess.run,
            ["sox", wav_8k, "-r", "48000", wav_48k],
            capture_output=True, timeout=10,
        )
        out_path = wav_48k if proc.returncode == 0 and os.path.exists(wav_48k) else wav_8k
        with open(out_path, "rb") as f:
            audio_bytes = f.read()
        return web.Response(body=audio_bytes, content_type="audio/wav")
    finally:
        for p in (wav_8k, wav_48k):
            try:
                os.unlink(p)
            except Exception:
                pass


async def handle_tts(request: web.Request) -> web.Response:
    """Render text to a native-rate WAV for the chat module.

    Body: {"text": "...", "voice": "piper:en_US-ryan-high"}
    Returns: audio/wav bytes (typically 22050Hz, the Piper native rate)
    """
    if not _check_secret(request):
        log.warning("auth_fail: /tts from %s", request.remote or "?")
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "text required"}, status=400)
    # Cap at 2500 chars — about 2 minutes of spoken audio at Piper's
    # ~0.4 real-time factor on this CPU. Anything longer should be
    # streamed sentence-by-sentence (Phase 2b).
    if len(text) > 2500:
        text = text[:2500]

    voice = (body.get("voice") or "piper:en_US-ryan-high")
    if not voice.startswith("piper:"):
        return web.json_response(
            {"ok": False, "error": "chat /tts requires a piper:<model> voice"},
            status=400,
        )
    model_name = voice.split(":", 1)[1]

    with tempfile.NamedTemporaryFile(suffix=".wav", dir="/tmp/nexus_voice", delete=False) as tf:
        out_wav = tf.name
    try:
        ok = await asyncio.to_thread(_render_piper_native, text, out_wav, model_name)
        if not ok or not os.path.exists(out_wav):
            return web.json_response({"ok": False, "error": "TTS render failed"}, status=500)
        with open(out_wav, "rb") as f:
            audio_bytes = f.read()
        return web.Response(body=audio_bytes, content_type="audio/wav")
    finally:
        try:
            os.unlink(out_wav)
        except Exception:
            pass


async def handle_ivr_whitelist(request: web.Request) -> web.Response:
    """Accept a whitelist push from the Nexus API.

    Body: {"recipients": [{"number": "+17408211712", "label": "Tim",
                            "pin_hash": "$2b$12$..."}, ...]}
    """
    if not _check_secret(request):
        remote = request.remote or "?"
        log.warning("auth_fail: /ivr/whitelist from %s", remote)
        return web.json_response(
            {"ok": False, "error": "unauthorized"}, status=401,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "invalid JSON body"}, status=400,
        )
    recipients = body.get("recipients")
    if not isinstance(recipients, list):
        return web.json_response(
            {"ok": False, "error": "'recipients' must be a list"}, status=400,
        )
    new_voice = body.get("voice", "")
    new_messages = body.get("ivr_messages", {})

    if _baresip is not None:
        _baresip.update_whitelist(recipients)

        # Apply custom IVR messages from settings
        messages_changed = False
        for key, text in new_messages.items():
            short_key = key.replace("ivr_", "")
            if short_key in ivr._PROMPT_TEXTS and text != ivr._PROMPT_TEXTS[short_key]:
                ivr._PROMPT_TEXTS[short_key] = text
                messages_changed = True

        # Re-render prompts if voice or messages changed
        need_rerender = messages_changed
        if new_voice and new_voice != _baresip._voice:
            _baresip._voice = new_voice
            need_rerender = True

        if need_rerender:
            log.info("re-rendering IVR prompts (voice=%s)", new_voice or _baresip._voice)
            for key, text in ivr._PROMPT_TEXTS.items():
                path = ivr.PROMPTS[key]
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                _render_tts(text, str(path), _baresip._voice)
            log.info("IVR prompts re-rendered")
    else:
        log.warning("/ivr/whitelist called but IVR manager not initialised")
    return web.json_response({"ok": True, "count": len(recipients), "voice": new_voice or _baresip._voice if _baresip else "?"})


def _wav_duration_seconds(path: str) -> float:
    """Read a WAV header and return its duration in seconds. Falls back
    to a conservative 3.0s if the file isn't readable as WAV."""
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception as e:
        log.warning("wav duration probe failed for %s: %s", path, e)
        return 3.0


def _ensure_confirmation_wavs(voice: str) -> dict[str, tuple[str, float]]:
    """Pre-generate the DTMF-acknowledgment audio prompts.

    Played mid-call when the recipient presses 1 (ack) or 9 (escalate)
    so they hear an explicit confirmation instead of dead air. Uses
    the configured ``voice`` (espeak / piper:<model> / google:<voice>)
    so the confirmation matches the voice the recipient just heard for
    the urgent summary. Generated once at container startup; the file
    name embeds a digest of the voice so a TTS_VOICE change forces a
    regeneration without manual cleanup.

    Returns ``{digit: (wav_path, duration_seconds)}`` — duration is
    used by the IVR manager to schedule a hangup after playback,
    since linphonec's AUDIO_EOF print is unreliable.
    """
    import hashlib
    out_dir = Path("/tmp/nexus_voice")
    out_dir.mkdir(parents=True, exist_ok=True)
    voice_tag = hashlib.sha1(voice.encode("utf-8")).hexdigest()[:8]
    mapping = {
        "1": (out_dir / f"_dtmf_ack_{voice_tag}.wav",
              "Acknowledged. Goodbye."),
        "9": (out_dir / f"_dtmf_escalate_{voice_tag}.wav",
              "Escalating to the next on-call. Goodbye."),
    }
    out: dict[str, tuple[str, float]] = {}
    for digit, (path, text) in mapping.items():
        if path.exists() and path.stat().st_size > 100:
            out[digit] = (str(path), _wav_duration_seconds(str(path)))
            continue
        if _render_tts(text, str(path), voice):
            dur = _wav_duration_seconds(str(path))
            out[digit] = (str(path), dur)
            log.info("confirmation wav ready: digit=%s voice=%s path=%s duration=%.2fs",
                     digit, voice, path, dur)
        else:
            log.warning("confirmation wav render failed: digit=%s voice=%s",
                        digit, voice)
    return out


async def _on_startup(app: web.Application) -> None:
    """Start the persistent linphonec + IVR event loop."""
    global _baresip
    tts_voice = os.environ.get("TTS_VOICE", "espeak")
    confirmation_wavs = _ensure_confirmation_wavs(tts_voice)
    mgr = ivr.BaresipManager(
        render_tts=_render_tts,
        voice=tts_voice,
        nexus_api_url=NEXUS_API_URL,
        voice_api_secret=VOICE_API_SECRET,
        outbound_confirmation_wavs=confirmation_wavs,
    )
    _baresip = mgr
    await mgr.start()
    app["ivr_task"] = asyncio.create_task(mgr.run_ivr_loop())
    log.info("IVR manager started — persistent linphonec running")


async def _on_shutdown(app: web.Application) -> None:
    """Gracefully stop the persistent linphonec."""
    task = app.get("ivr_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if _baresip is not None:
        await _baresip.stop()
    log.info("IVR manager stopped")


def main() -> None:
    if not SIP_PASSWORD:
        log.error("SIP_PASSWORD env var is required")
        raise SystemExit(2)
    if not VOICE_API_SECRET or len(VOICE_API_SECRET) < 16:
        log.error(
            "VOICE_API_SECRET env var is required and must be >= 16 chars. "
            "Without this, /call is an unauthenticated phone-dial-anywhere "
            "primitive. Refusing to start."
        )
        raise SystemExit(4)
    log.info("writing linphonec + baresip config for ext %s at %s:%s/%s",
             SIP_EXTENSION, SIP_HOST, SIP_PORT, SIP_TRANSPORT)
    _write_baresip_config()

    app = web.Application(middlewares=[strip_server_header])
    app.router.add_get("/health", handle_health)
    app.router.add_post("/call", handle_call)
    app.router.add_post("/preview", handle_preview)
    app.router.add_post("/tts", handle_tts)
    app.router.add_post("/ivr/whitelist", handle_ivr_whitelist)

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)

    log.info("nexus-voice dispatch server listening on :%d", HTTP_PORT)
    web.run_app(app, host="0.0.0.0", port=HTTP_PORT, access_log=None)


if __name__ == "__main__":
    main()
