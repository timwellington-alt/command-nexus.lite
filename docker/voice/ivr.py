"""
Nexus IVR — inbound call handler running atop a persistent linphonec process.

This module manages:
- A long-lived linphonec subprocess (stdin/stdout) registered to the UCM
- An async event parser that reads linphonec stdout line by line
- An IVR state machine: greeting → PIN → menu → status readout
- DTMF collection, audio switching, and caller whitelisting

The persistent linphonec is also used for outbound /call requests — the
server.py /call handler sends commands through the same process
instead of spawning a fresh one.

Phase 1 scope:
- Greeting → PIN entry → Menu option 1 (system status readout)
- Whitelist-only inbound calls (reject unknown callers)
- PIN verification via bcrypt
- Outbound calls via `call` command to persistent linphonec
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable

import bcrypt

log = logging.getLogger("nexus_voice.ivr")

LINPHONERC_PATH = Path("/root/.linphonerc")

# ── Event patterns (linphonec output) ────────────────────────────
# linphonec prints various messages; these regexes match the ones we care about.
# Incoming: "Receiving new incoming call from <sip:...>, assigned id N"
#           or "Incomming call from ..." (yes, typo in some linphone versions)
_RE_INCOMING = re.compile(
    r"(?:Receiving new incoming call from|Incomm?ing call from)\s*(.+)",
    re.IGNORECASE,
)
# Call connected: "Call N with <sip:...> connected." or "Media streams established"
_RE_ESTABLISHED = re.compile(
    r"(?:connected\.|Media streams established)",
    re.IGNORECASE,
)
# Call ended: "Call N with ... ended" or "Call terminated"
_RE_TERMINATED = re.compile(
    r"(?:Call .+ ended|Call ended|call terminated)",
    re.IGNORECASE,
)
# DTMF: "Receiving tone X" or "DTMF X received"
_RE_DTMF = re.compile(
    r"(?:Receiving tone\s+([0-9*#])|DTMF\s+([0-9*#]))",
    re.IGNORECASE,
)
# Audio EOF: linphonec may print when a played file ends.
# If it doesn't, we rely on a timer.  Try several patterns:
_RE_AUDIO_EOF = re.compile(
    r"(?:End of media|EOF|end of file|played\.)",
    re.IGNORECASE,
)
# Registration success: "Registration on <sip:...> successful"
_RE_REGISTERED = re.compile(
    r"Registration .+ successful",
    re.IGNORECASE,
)
# Registration failure
_RE_REG_FAILED = re.compile(
    r"Registration .+ failed",
    re.IGNORECASE,
)
# Outbound call-progress patterns
_RE_SIP_PROGRESS = re.compile(r"(?:Contacting|Early media)", re.IGNORECASE)
_RE_SIP_ERROR = re.compile(
    r"\b(404|403|486|487|503|busy|error|fail|declined)\b", re.IGNORECASE,
)


# ── Event types ───────────────────────────────────────────────────
class Event(Enum):
    REGISTERED = auto()
    INCOMING_CALL = auto()
    CALL_ESTABLISHED = auto()
    CALL_TERMINATED = auto()
    DTMF = auto()
    AUDIO_EOF = auto()
    SIP_PROGRESS = auto()
    SIP_ERROR = auto()
    LINE = auto()  # raw line for logging


# ── IVR session states ────────────────────────────────────────────
class State(Enum):
    IDLE = auto()
    RINGING = auto()
    CHECK_CALLER = auto()
    GREETING = auto()
    PIN_PROMPT = auto()
    PIN_COLLECT = auto()
    PIN_VERIFY = auto()
    MENU = auto()
    STATUS_READOUT = auto()
    HANGUP = auto()


# ── Prompt paths ──────────────────────────────────────────────────
_PROMPT_DIR = Path("/tmp/nexus_voice")
PROMPTS = {
    "greeting": _PROMPT_DIR / "ivr_greeting.wav",
    "pin_prompt": _PROMPT_DIR / "ivr_pin_prompt.wav",
    "pin_fail": _PROMPT_DIR / "ivr_pin_fail.wav",
    "lockout": _PROMPT_DIR / "ivr_lockout.wav",
    "menu": _PROMPT_DIR / "ivr_menu.wav",
    "goodbye": _PROMPT_DIR / "ivr_goodbye.wav",
}

_PROMPT_TEXTS = {
    "greeting": "Welcome to Nexus.",
    "pin_prompt": "Enter your PIN followed by the pound key.",
    "pin_fail": "Incorrect PIN. Try again.",
    "lockout": "Too many failed attempts. Goodbye.",
    "menu": (
        "Press 1 for system status. Press 2 for active alerts. "
        "Press 0 to repeat. Press star to hang up."
    ),
    "goodbye": "Goodbye.",
}

# Max PIN attempts before lockout (per caller, resets on container restart)
_MAX_PIN_ATTEMPTS = 3
_PIN_TIMEOUT = 15  # seconds


# ── Persistent linphonec manager ─────────────────────────────────
class BaresipManager:
    """Manages a single long-lived linphonec subprocess.

    All interaction (inbound IVR + outbound /call) goes through this
    process via stdin commands and stdout event parsing.

    Name kept as BaresipManager for API compatibility with server.py.
    """

    def __init__(
        self,
        render_tts: Callable[..., bool],
        voice: str = "espeak",
        nexus_api_url: str = "",
        voice_api_secret: str = "",
        outbound_confirmation_wavs: dict[str, tuple[str, float]] | None = None,
    ):
        self._render_tts = render_tts
        self._voice = voice
        self._nexus_api_url = nexus_api_url.rstrip("/")
        self._voice_api_secret = voice_api_secret
        self._proc: asyncio.subprocess.Process | None = None
        self._event_queue: asyncio.Queue[tuple[Event, str]] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._ivr_session: IVRSession | None = None
        self._registered = False
        self._whitelist: list[dict[str, Any]] = []
        self._pin_failures: dict[str, int] = {}  # number → count
        # Inbound call state
        self._pending_caller_id: str = ""
        self._pending_caller: dict[str, Any] | None = None
        # Outbound-call coordination
        self._outbound_active = False
        self._outbound_done: asyncio.Event = asyncio.Event()
        self._outbound_wav_path: str = ""
        # Mid-call DTMF response — pre-generated confirmation prompts
        # by digit ("1" → ack, "9" → escalate). Played mid-call when
        # the recipient presses the corresponding key; followed by a
        # hangup as soon as that prompt finishes.
        self._outbound_confirmation_wavs = outbound_confirmation_wavs or {}
        self._outbound_pending_hangup = False

    # ── Whitelist management ──────────────────────────────────────

    def update_whitelist(self, recipients: list[dict[str, Any]]) -> None:
        self._whitelist = list(recipients)
        log.info("ivr whitelist updated: %d entries", len(self._whitelist))

    def _lookup_caller(self, caller_id: str) -> dict[str, Any] | None:
        """Match a caller ID against the whitelist.

        linphonec presents the incoming call as something like:
          "Receiving new incoming call from sip:7408211712@10.30.40.2"
        We extract the phone number from the sip: URI first, then
        compare the last 10 digits (US numbers).
        """
        # Extract the number from "sip:<number>@" if present
        sip_match = re.search(r"sip:(\d+)@", caller_id)
        if sip_match:
            digits = sip_match.group(1)
        else:
            # Fallback: strip everything non-digit and take the first
            # contiguous digit block that looks like a phone number
            digit_blocks = re.findall(r"\d{7,}", caller_id)
            digits = digit_blocks[0] if digit_blocks else re.sub(r"[^\d]", "", caller_id)

        if len(digits) > 10:
            digits = digits[-10:]

        for entry in self._whitelist:
            entry_digits = re.sub(r"[^\d]", "", entry.get("number", ""))
            if len(entry_digits) > 10:
                entry_digits = entry_digits[-10:]
            if digits and entry_digits and digits == entry_digits:
                return entry
        return None

    # ── Process lifecycle ─────────────────────────────────────────

    async def start(self) -> None:
        """Pre-render static prompts and launch linphonec."""
        _PROMPT_DIR.mkdir(parents=True, exist_ok=True)

        # Pre-render static IVR prompts
        for key, text in _PROMPT_TEXTS.items():
            path = PROMPTS[key]
            if not path.exists():
                log.info("rendering IVR prompt: %s", key)
                ok = await asyncio.to_thread(
                    self._render_tts, text, str(path), self._voice,
                )
                if not ok:
                    log.error("failed to render IVR prompt: %s", key)

        await self._spawn()

    async def _spawn(self) -> None:
        """Spawn (or respawn) the linphonec subprocess.

        linphonec reads commands from stdin (line-delimited) and writes
        events to stdout. No PTY needed — plain pipes work perfectly.
        """
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.stdin.write(b"quit\n")
                await self._proc.stdin.drain()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

        # Ensure prompt directory exists
        Path("/tmp/nexus_voice").mkdir(parents=True, exist_ok=True)

        log.info("spawning persistent linphonec (config=%s)", LINPHONERC_PATH)
        self._proc = await asyncio.create_subprocess_exec(
            "linphonec", "-c", str(LINPHONERC_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        self._registered = False
        self._reader_task = asyncio.create_task(self._read_stdout())

        # Send initial setup commands
        # Wait a moment for linphonec to initialize
        await asyncio.sleep(2)
        await self.send_cmd("soundcard use files")
        await asyncio.sleep(0.5)

        # Register via interactive command — config-file-based
        # registration doesn't work reliably with linphonec 5.2.0.
        sip_host = os.environ.get("SIP_HOST", "10.30.40.2")
        sip_ext = os.environ.get("SIP_EXTENSION", "4000")
        sip_pw = os.environ.get("SIP_PASSWORD", "")
        await self.send_cmd(f"register sip:{sip_ext}@{sip_host} sip:{sip_host} {sip_pw}", redact_log=True)
        log.info("linphonec started, soundcard=files, register command sent")

    async def stop(self) -> None:
        """Gracefully stop linphonec."""
        if self._proc and self._proc.returncode is None:
            await self.send_cmd("quit")
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    # ── Command interface ─────────────────────────────────────────

    async def send_cmd(self, cmd: str, *, redact_log: bool = False) -> None:
        """Send a command to linphonec via stdin pipe.

        linphonec reads line-delimited commands from stdin.
        No PTY needed — plain pipes work perfectly.

        Set ``redact_log=True`` for commands that contain secrets
        (e.g. the register command with the SIP password).
        """
        if self._proc is None or self._proc.stdin is None:
            log.warning("send_cmd — no linphonec process")
            return
        if self._proc.returncode is not None:
            log.warning("send_cmd — linphonec already exited (rc=%s)",
                        self._proc.returncode)
            return
        try:
            self._proc.stdin.write(f"{cmd}\n".encode())
            await self._proc.stdin.drain()
            if redact_log:
                log.info("send_cmd(<redacted>) sent")
            else:
                log.info("send_cmd(%r) sent", cmd)
        except Exception as e:
            log.warning("send_cmd write failed: %s", e)

    async def play_audio(self, wav_path: str) -> None:
        """Play a WAV file on the current call."""
        await self.send_cmd(f"play {wav_path}")

    async def answer(self) -> None:
        """Answer the current incoming call."""
        await self.send_cmd("answer")

    async def hangup(self) -> None:
        """Hang up the current call."""
        await self.send_cmd("terminate")

    async def _hangup_after(self, delay_s: float) -> None:
        """Wait then hangup. Used for the DTMF confirmation playback so
        we don't depend on linphonec's AUDIO_EOF print (which isn't
        emitted by all builds and was causing the prompt to loop)."""
        try:
            await asyncio.sleep(max(0.1, delay_s))
            if self._outbound_active:
                log.info("delayed hangup firing after %.2fs", delay_s)
                await self.hangup()
        except asyncio.CancelledError:
            pass

    async def _report_lockout(self, number: str, label: str, attempts: int) -> None:
        """Report a PIN lockout to the Nexus API for persistent tracking."""
        if not self._nexus_api_url:
            return
        try:
            import aiohttp as _aiohttp
            url = f"{self._nexus_api_url}/api/voice/lockout"
            headers = {"X-Requested-With": "CommandNexus"}
            if self._voice_api_secret:
                headers["X-Voice-Secret"] = self._voice_api_secret
            async with _aiohttp.ClientSession() as session:
                await session.post(url, json={
                    "number": number,
                    "label": label,
                    "attempts": attempts,
                }, headers=headers, timeout=_aiohttp.ClientTimeout(total=5))
            log.warning("lockout reported to Nexus API: %s (%s)", label, number)
        except Exception as e:
            log.warning("failed to report lockout: %s", e)

    async def log_inbound_call(self, caller: str, caller_name: str, outcome: str, detail: str = "", duration: int | None = None) -> None:
        """Post an inbound call event to the Nexus API for CDR logging."""
        if not self._nexus_api_url:
            return
        try:
            import aiohttp as _aiohttp
            url = f"{self._nexus_api_url}/api/voice/cdr/inbound"
            headers = {"X-Requested-With": "CommandNexus"}
            if self._voice_api_secret:
                headers["X-Voice-Secret"] = self._voice_api_secret
            async with _aiohttp.ClientSession() as session:
                await session.post(url, json={
                    "caller": caller,
                    "callee": os.environ.get("SIP_EXTENSION", "4000"),
                    "caller_name": caller_name,
                    "outcome": outcome,
                    "detail": detail,
                    "duration": duration,
                }, headers=headers, timeout=_aiohttp.ClientTimeout(total=5))
        except Exception as e:
            log.warning("failed to log inbound call: %s", e)

    async def dial(self, number: str) -> None:
        """Place an outbound call."""
        sip_host = os.environ.get("SIP_HOST", "10.30.40.2")
        await self.send_cmd(f"call sip:{number}@{sip_host}")

    # ── stdout reader ─────────────────────────────────────────────

    async def _read_stdout(self) -> None:
        """Read linphonec stdout line by line and parse events."""
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue

                # Suppress the interactive prompt noise
                # linphonec prints "linphonec> " prompts
                if line.startswith("linphonec>"):
                    line = line[len("linphonec>"):].strip()
                    if not line:
                        continue

                # Parse known event patterns
                if _RE_INCOMING.search(line):
                    m = _RE_INCOMING.search(line)
                    caller = m.group(1) if m else ""
                    await self._event_queue.put((Event.INCOMING_CALL, caller))
                elif _RE_ESTABLISHED.search(line):
                    await self._event_queue.put((Event.CALL_ESTABLISHED, line))
                elif _RE_TERMINATED.search(line):
                    await self._event_queue.put((Event.CALL_TERMINATED, line))
                elif _RE_DTMF.search(line):
                    m = _RE_DTMF.search(line)
                    # Pattern has two groups — one for each alternate format
                    digit = (m.group(1) or m.group(2)) if m else ""
                    await self._event_queue.put((Event.DTMF, digit))
                elif _RE_AUDIO_EOF.search(line):
                    await self._event_queue.put((Event.AUDIO_EOF, line))
                elif _RE_REGISTERED.search(line):
                    if not self._registered:
                        log.info("linphonec registered: %s", line)
                        self._registered = True
                    await self._event_queue.put((Event.REGISTERED, line))
                elif _RE_REG_FAILED.search(line):
                    log.error("linphonec registration failed: %s", line)
                    await self._event_queue.put((Event.LINE, line))
                elif _RE_SIP_PROGRESS.search(line):
                    await self._event_queue.put((Event.SIP_PROGRESS, line))
                elif _RE_SIP_ERROR.search(line):
                    await self._event_queue.put((Event.SIP_ERROR, line))
                else:
                    await self._event_queue.put((Event.LINE, line))

        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("linphonec stdout reader crashed")
        finally:
            log.warning("linphonec stdout reader exited")

    # ── Event consumer (IVR loop) ─────────────────────────────────

    async def run_ivr_loop(self) -> None:
        """Main loop: consume events from linphonec and drive the IVR."""
        restart_delay = 2
        while True:
            # Ensure linphonec is running
            if self._proc is None or self._proc.returncode is not None:
                log.warning(
                    "linphonec exited (rc=%s), restarting in %ds",
                    self._proc.returncode if self._proc else "N/A",
                    restart_delay,
                )
                await asyncio.sleep(restart_delay)
                restart_delay = min(restart_delay * 2, 30)
                await self._spawn()
                continue

            try:
                event, data = await asyncio.wait_for(
                    self._event_queue.get(), timeout=5.0,
                )
            except asyncio.TimeoutError:
                # Periodic liveness check
                if self._proc.returncode is not None:
                    continue
                continue
            except asyncio.CancelledError:
                return

            restart_delay = 2  # reset on successful read

            # If an outbound call is active, forward events to the
            # outbound handler (not the IVR).
            if self._outbound_active:
                await self._handle_outbound_event(event, data)
                continue

            # Inbound IVR event handling
            try:
                await self._handle_ivr_event(event, data)
            except Exception:
                log.exception("IVR event handler crashed for event=%s data=%r", event, data[:100])

    async def _handle_ivr_event(self, event: Event, data: str) -> None:
        """Process a single event in the IVR context."""
        if event == Event.INCOMING_CALL:
            # linphonec does NOT auto-answer — we control answer ourselves.
            # Store caller ID for whitelist check and decide whether to answer.
            log.info("incoming call: %s", data)
            self._pending_caller_id = data

            # Check whitelist BEFORE answering
            caller = self._lookup_caller(data)
            if caller is None:
                log.warning("caller not in whitelist, rejecting: %s", data)
                await self.hangup()
                # Extract caller name from SIP header if present
                name_match = re.match(r'"([^"]+)"', data)
                cname = name_match.group(1) if name_match else ""
                sip_match = re.search(r'sip:(\d+)@', data)
                cnum = sip_match.group(1) if sip_match else data[:30]
                asyncio.create_task(self.log_inbound_call(cnum, cname, "rejected", "not in whitelist"))
                return

            # Check lockout — both in-memory (this session) and persistent
            # (LOCKED: prefix on pin_hash set by the Nexus API)
            caller_num = caller.get("number", "")
            pin_hash = caller.get("pin_hash", "")
            is_locked = (
                self._pin_failures.get(caller_num, 0) >= _MAX_PIN_ATTEMPTS
                or pin_hash.startswith("LOCKED:")
            )
            if is_locked:
                log.warning("caller locked out, rejecting: %s", caller_num)
                await self.hangup()
                asyncio.create_task(self.log_inbound_call(
                    caller_num[-10:], caller.get("label", ""), "locked", "PIN lockout active"
                ))
                return

            log.info("caller matched whitelist: %s — answering", caller.get("label", "?"))
            self._pending_caller = caller
            await self.answer()

        elif event == Event.CALL_ESTABLISHED:
            log.info("call established")
            caller_id = getattr(self, "_pending_caller_id", "") or data
            caller = getattr(self, "_pending_caller", None)

            if self._ivr_session is not None:
                log.info("ignoring — session already active")
                return

            if caller is None:
                # Fallback lookup if we missed the INCOMING_CALL event
                caller = self._lookup_caller(caller_id)
                if caller is None:
                    log.warning("caller not in whitelist (post-connect), hanging up: %s", caller_id)
                    await self.hangup()
                    return

            # Check lockout (belt-and-suspenders)
            caller_num = caller.get("number", "")
            if self._pin_failures.get(caller_num, 0) >= _MAX_PIN_ATTEMPTS:
                log.warning("caller locked out: %s", caller_num)
                await self.play_audio(str(PROMPTS["lockout"]))
                await asyncio.sleep(3)
                await self.hangup()
                return

            # Start IVR session — run() handles all audio playback
            log.info("starting IVR session")
            self._ivr_session = IVRSession(
                manager=self,
                caller=caller,
                raw_caller_id=caller_id,
            )
            log.info("launching session")
            asyncio.create_task(self._ivr_session.run())

        elif event == Event.DTMF:
            log.info("DTMF received: %r (session=%s)", data, "active" if self._ivr_session else "none")
            if self._ivr_session:
                self._ivr_session.push_dtmf(data)

        elif event == Event.AUDIO_EOF:
            if self._ivr_session:
                self._ivr_session.push_audio_done()

        elif event == Event.CALL_TERMINATED:
            log.info("call terminated: %s", data)
            if self._ivr_session:
                # Log the completed inbound session
                caller = self._ivr_session._caller
                cnum = caller.get("number", "")[-10:] if caller else ""
                clabel = caller.get("label", "")
                outcome = "completed" if self._ivr_session._pin_verified else "pin_failed"
                asyncio.create_task(self.log_inbound_call(
                    cnum, clabel, outcome,
                    f"pin_attempts={self._ivr_session._pin_attempts}",
                ))
                self._ivr_session.terminate()
                self._ivr_session = None
            # Clear pending caller state
            self._pending_caller_id = ""
            self._pending_caller = None

    # ── Outbound call support ─────────────────────────────────────
    # Outbound /call requests use this persistent process.

    async def place_outbound_call(
        self,
        wav_path: str,
        to_number: str,
        max_seconds: int,
        confirmation_wavs: dict[str, tuple[str, float]] | None = None,
    ) -> tuple[bool, str, int, str | None]:
        """Place an outbound call through the persistent linphonec.

        Returns (success, log_excerpt, duration_seconds, first_dtmf).

        ``confirmation_wavs`` overrides the startup-default map for
        this call only — used by the dispatch endpoint so the DTMF
        confirmation prompt is rendered in the same voice as the call
        summary (espeak vs. piper:<model>).
        """
        # If IVR session is active, drop it — outbound alerts take priority
        if self._ivr_session:
            log.info("dropping IVR session for outbound call priority")
            await self.hangup()
            # Wait briefly for termination
            await asyncio.sleep(1)
            self._ivr_session = None

        self._outbound_active = True
        self._outbound_done.clear()
        self._outbound_excerpt: list[str] = []
        self._outbound_established = False
        self._outbound_first_dtmf: str | None = None
        self._outbound_wav_path: str = wav_path
        self._outbound_call_confirmation_wavs = confirmation_wavs or self._outbound_confirmation_wavs

        started = time.time()

        # Dial first, then play audio once connected (in _handle_outbound_event)
        await self.dial(to_number)

        # Wait for call to complete or timeout
        try:
            await asyncio.wait_for(
                self._outbound_done.wait(),
                timeout=max_seconds,
            )
        except asyncio.TimeoutError:
            log.info("outbound call timeout, hanging up")
            await self.hangup()
            await asyncio.sleep(1)

        duration = int(time.time() - started)
        self._outbound_active = False

        success = self._outbound_established
        excerpt = "\n".join(self._outbound_excerpt[-50:])
        dtmf = self._outbound_first_dtmf

        return success, excerpt, duration, dtmf

    async def _handle_outbound_event(self, event: Event, data: str) -> None:
        """Process events during an outbound call."""
        if event == Event.CALL_ESTABLISHED:
            self._outbound_established = True
            self._outbound_excerpt.append(data)
            # Now that the call is connected, play the audio
            # The wav_path was passed to place_outbound_call; we need
            # to reconstruct it. Store it as an instance var.
            if hasattr(self, "_outbound_wav_path") and self._outbound_wav_path:
                await self.play_audio(self._outbound_wav_path)

        elif event == Event.DTMF:
            if self._outbound_established and self._outbound_first_dtmf is None:
                self._outbound_first_dtmf = data
                # Real-time confirmation playback. Play the matching
                # prompt and schedule an explicit hangup after the WAV's
                # known duration + a small buffer. Don't rely on
                # AUDIO_EOF — some linphonec builds don't print the EOF
                # marker, which caused the prompt to loop forever.
                entry = (getattr(self, "_outbound_call_confirmation_wavs", None)
                         or self._outbound_confirmation_wavs).get(data)
                if entry:
                    conf_wav, duration = entry
                    log.info("DTMF %s during outbound call — switching to %s (%.2fs)",
                             data, conf_wav, duration)
                    await self.play_audio(conf_wav)
                    asyncio.create_task(self._hangup_after(duration + 0.5))
                else:
                    # Unconfigured digit (e.g. 2-8) — just hang up.
                    log.info("DTMF %s during outbound call — hanging up "
                             "(no confirmation configured)", data)
                    await self.hangup()
            self._outbound_excerpt.append(f"DTMF: {data}")

        elif event == Event.CALL_TERMINATED:
            self._outbound_excerpt.append(data)
            self._outbound_done.set()
            self._outbound_pending_hangup = False

        elif event == Event.AUDIO_EOF:
            self._outbound_excerpt.append(data)

        elif event in (Event.LINE, Event.REGISTERED, Event.SIP_PROGRESS,
                       Event.SIP_ERROR):
            line_lower = data.lower()
            if any(s in line_lower for s in (
                "register", "200 ok", "progress", "connected",
                "terminated", "ended", "error", "fail", "busy",
                "404", "403", "486", "487", "503", "dtmf", "tone",
            )):
                self._outbound_excerpt.append(data)


# ── IVR session ───────────────────────────────────────────────────
class IVRSession:
    """State machine for a single inbound IVR call."""

    def __init__(
        self,
        manager: BaresipManager,
        caller: dict[str, Any],
        raw_caller_id: str,
    ):
        self._mgr = manager
        self._caller = caller
        self._raw_caller_id = raw_caller_id
        self._state = State.GREETING
        self._dtmf_queue: asyncio.Queue[str] = asyncio.Queue()
        self._audio_done: asyncio.Event = asyncio.Event()
        self._terminated = False
        self._pin_attempts = 0
        self._pin_verified = False

    def push_dtmf(self, digit: str) -> None:
        if not self._terminated:
            self._dtmf_queue.put_nowait(digit)

    def push_audio_done(self) -> None:
        self._audio_done.set()

    def terminate(self) -> None:
        self._terminated = True
        # Unblock any waiting coroutines
        self._dtmf_queue.put_nowait("")
        self._audio_done.set()

    async def run(self) -> None:
        """Drive the IVR flow from greeting through hangup."""
        label = self._caller.get("label", "unknown")
        number = self._caller.get("number", self._raw_caller_id)
        log.info("IVR session started: %s (%s)", label, number)

        try:
            # ── Greeting ──────────────────────────────────────────
            self._state = State.GREETING
            await self._play_and_wait("greeting")
            if self._terminated:
                return

            # ── PIN ───────────────────────────────────────────────
            # Play PIN prompt ONCE. On failure, say "incorrect" and
            # wait again — don't replay the full prompt each time.
            self._state = State.PIN_PROMPT
            self._drain_dtmf_queue()
            await self._play_and_wait("pin_prompt")
            if self._terminated:
                return

            while self._pin_attempts < _MAX_PIN_ATTEMPTS:
                if self._terminated:
                    return

                self._state = State.PIN_COLLECT
                digits = await self._collect_digits(timeout=_PIN_TIMEOUT)
                if self._terminated:
                    return

                if digits == "*":
                    await self._hangup_graceful()
                    return

                self._state = State.PIN_VERIFY
                pin_hash = self._caller.get("pin_hash", "")
                if digits and pin_hash and self._verify_pin(digits, pin_hash):
                    log.info("PIN verified for %s", label)
                    self._pin_verified = True
                    self._mgr._pin_failures[number] = 0
                    break
                else:
                    self._pin_attempts += 1
                    self._mgr._pin_failures[number] = (
                        self._mgr._pin_failures.get(number, 0) + 1
                    )
                    log.warning(
                        "PIN failed for %s (attempt %d/%d)",
                        label, self._pin_attempts, _MAX_PIN_ATTEMPTS,
                    )
                    if self._pin_attempts >= _MAX_PIN_ATTEMPTS:
                        await self._play_and_wait("lockout")
                        # Report lockout to Nexus API for persistent tracking
                        asyncio.create_task(self._mgr._report_lockout(
                            number=number, label=label, attempts=self._pin_attempts,
                        ))
                        await self._hangup_graceful()
                        return
                    # Don't replay the full prompt — just say "incorrect"
                    await self._play_and_wait("pin_fail")
                    self._drain_dtmf_queue()
            else:
                await self._hangup_graceful()
                return

            # ── Menu loop (barge-in enabled) ──────────────────────
            # The caller can press a key DURING the menu audio to
            # skip ahead immediately, like a bank phone system.
            self._drain_dtmf_queue()
            menu_timeout_count = 0

            while not self._terminated:
                self._state = State.MENU

                # Play menu with barge-in — if a digit arrives during
                # playback, skip the rest of the audio and process it.
                choice = await self._play_with_bargein("menu")
                if self._terminated:
                    return

                if choice is None:
                    # Audio finished without barge-in — wait for digit
                    choice = await self._collect_single_digit(timeout=10)

                log.info("menu: choice=%r", choice)
                if self._terminated:
                    return

                if choice == "1":
                    self._state = State.STATUS_READOUT
                    await self._do_status_readout()
                    self._drain_dtmf_queue()
                    menu_timeout_count = 0
                elif choice == "2":
                    self._state = State.STATUS_READOUT
                    await self._do_alerts_readout()
                    self._drain_dtmf_queue()
                    menu_timeout_count = 0
                elif choice == "0":
                    menu_timeout_count = 0
                    continue
                elif choice == "*":
                    await self._hangup_graceful()
                    return
                elif choice == "":
                    menu_timeout_count += 1
                    if menu_timeout_count >= 2:
                        await self._hangup_graceful()
                        return
                    # Replay menu once on timeout
                else:
                    # Unrecognized — replay
                    continue

        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("IVR session error for %s", label)
            await self._hangup_graceful()

    # ── Helpers ───────────────────────────────────────────────────

    async def _play_and_wait(self, prompt_key: str, wait_seconds: float = 0.5) -> None:
        """Play a prompt WAV and wait for it to finish.

        After the estimated duration, sends a silence file to stop
        any looping that linphonec might do with the play command.
        """
        path = PROMPTS.get(prompt_key)
        if not path or not path.exists():
            log.warning("prompt not found: %s", prompt_key)
            await asyncio.sleep(1)
            return
        await self._mgr.play_audio(str(path))
        # Estimate duration from file size: 8kHz 16-bit mono = 16000 bytes/sec
        try:
            size = path.stat().st_size
            duration = max(size / 16000, 1.5)
        except Exception:
            duration = 3.0
        await asyncio.sleep(duration)
        # Stop any audio looping by playing silence
        silence = Path("/tmp/nexus_voice/silence.wav")
        if silence.exists():
            await self._mgr.play_audio(str(silence))
        await asyncio.sleep(wait_seconds)

    async def _collect_digits(self, timeout: float = 15) -> str:
        """Collect DTMF digits until # is pressed or timeout."""
        digits = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                digit = await asyncio.wait_for(
                    self._dtmf_queue.get(), timeout=remaining,
                )
            except asyncio.TimeoutError:
                break
            if self._terminated:
                return digits
            if digit == "":
                # Termination sentinel
                return digits
            if digit == "#":
                return digits
            if digit == "*":
                return "*"
            digits += digit
        return digits

    async def _play_with_bargein(self, prompt_key: str) -> str | None:
        """Play a prompt WAV but return immediately if a DTMF digit
        arrives during playback (barge-in).

        Returns the digit if barged-in, or None if the audio played
        to completion without any key press.
        """
        path = PROMPTS.get(prompt_key)
        if not path or not path.exists():
            await asyncio.sleep(1)
            return None
        await self._mgr.play_audio(str(path))
        try:
            size = path.stat().st_size
            duration = max(size / 16000, 1.5)
        except Exception:
            duration = 5.0
        # Wait for EITHER the audio duration OR a DTMF digit
        try:
            digit = await asyncio.wait_for(
                self._dtmf_queue.get(), timeout=duration + 0.5,
            )
            log.info("barge-in: digit=%r during %s", digit, prompt_key)
            return digit
        except asyncio.TimeoutError:
            return None

    def _drain_dtmf_queue(self) -> int:
        """Drain all stale DTMF digits from the queue. Returns count drained."""
        count = 0
        while not self._dtmf_queue.empty():
            try:
                self._dtmf_queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        if count:
            log.info("drained %d stale DTMF digits", count)
        return count

    async def _collect_single_digit(self, timeout: float = 10) -> str:
        """Collect a single DTMF digit."""
        try:
            digit = await asyncio.wait_for(
                self._dtmf_queue.get(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ""
        if self._terminated or digit == "":
            return ""
        return digit

    def _verify_pin(self, pin: str, pin_hash: str) -> bool:
        """Check PIN against bcrypt hash."""
        try:
            return bcrypt.checkpw(
                pin.encode("utf-8"),
                pin_hash.encode("utf-8"),
            )
        except Exception:
            log.exception("bcrypt verification error")
            return False

    async def _do_status_readout(self) -> None:
        """Fetch system status from the Nexus API and read it aloud."""
        nexus_url = self._mgr._nexus_api_url
        speech_text = ""

        if nexus_url:
            try:
                import aiohttp as _aiohttp

                # Try the speech endpoint — auth via voice secret
                url = f"{nexus_url}/api/network/system-status/speech"
                headers = {"X-Requested-With": "CommandNexus"}
                if self._mgr._voice_api_secret:
                    headers["X-Voice-Secret"] = self._mgr._voice_api_secret
                async with _aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers=headers,
                        timeout=_aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            speech_text = data.get("text", data.get("speech", ""))
                        else:
                            log.warning("system-status/speech returned %d", resp.status)
            except Exception:
                log.exception("failed to fetch system status")

        if not speech_text:
            speech_text = "System status is currently unavailable. Please try the Nexus web interface."

        log.info("status readout: %s", speech_text[:80])

        # Render to WAV and play
        status_wav = str(_PROMPT_DIR / "ivr_status_dynamic.wav")
        ok = await asyncio.to_thread(
            self._mgr._render_tts, speech_text, status_wav, self._mgr._voice,
        )
        if ok:
            await self._mgr.play_audio(status_wav)
            # Duration-based wait (same as _play_and_wait)
            try:
                size = Path(status_wav).stat().st_size
                duration = max(size / 16000, 2.0)
            except Exception:
                duration = 10.0
            await asyncio.sleep(duration + 1.0)
        else:
            log.error("failed to render status readout TTS")

    async def _do_alerts_readout(self) -> None:
        """Fetch active alerts from the Nexus API and read them aloud."""
        nexus_url = self._mgr._nexus_api_url
        speech_text = ""

        if nexus_url:
            try:
                import aiohttp as _aiohttp
                url = f"{nexus_url}/api/network/system-status"
                headers = {"X-Requested-With": "CommandNexus"}
                if self._mgr._voice_api_secret:
                    headers["X-Voice-Secret"] = self._mgr._voice_api_secret
                async with _aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Build alerts-focused readout
                            parts = []
                            sw = data.get("switches", {})
                            if sw.get("down", 0) > 0:
                                parts.append(f"{sw['down']} switches down.")
                                for msg in sw.get("down_summary", []):
                                    # Spell out hostnames inline — can't import
                                    # from app.modules (voice container has no
                                    # app package). Same regex as _spell_hostname.
                                    spelled = re.sub(
                                        r'\b([a-z]{2,5}(?:-[a-z0-9]+){2,})\b',
                                        lambda m: ", ".join(
                                            " ".join(p.upper()) if (len(p) <= 5 and any(c.isdigit() for c in p))
                                            else p
                                            for p in m.group(1).split("-")
                                        ),
                                        msg, flags=re.IGNORECASE,
                                    )
                                    parts.append(spelled)
                            wl = data.get("wireless", {})
                            if wl.get("aps_down", 0) > 0:
                                parts.append(f"{wl['aps_down']} access points down.")
                            sv = data.get("servers", {})
                            if sv.get("down", 0) > 0:
                                parts.append(f"{sv['down']} servers down.")
                            inet = data.get("internet", {})
                            if inet.get("status") == "down":
                                parts.append("Internet is down.")
                            px = data.get("paxton", {})
                            if px.get("status") not in ("online", "not_configured"):
                                parts.append("Paxton is unreachable.")
                            if parts:
                                speech_text = "Active alerts. " + " ".join(parts)
                            else:
                                speech_text = "No active alerts. All systems are operational."
            except Exception:
                log.exception("failed to fetch alerts")

        if not speech_text:
            speech_text = "Alert information is currently unavailable."

        log.info("alerts readout: %s", speech_text[:80])
        wav = str(_PROMPT_DIR / "ivr_alerts_dynamic.wav")
        ok = await asyncio.to_thread(self._mgr._render_tts, speech_text, wav, self._mgr._voice)
        if ok:
            await self._mgr.play_audio(wav)
            try:
                size = Path(wav).stat().st_size
                duration = max(size / 16000, 2.0)
            except Exception:
                duration = 10.0
            await asyncio.sleep(duration + 1.0)

    async def _hangup_graceful(self) -> None:
        """Play goodbye and hang up."""
        if not self._terminated:
            await self._play_and_wait("goodbye", wait_seconds=1.0)
            await self._mgr.hangup()
