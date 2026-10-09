"""
Integration connection tests — safe health checks for configured integrations.

Each test verifies connectivity without exposing secrets.
Results are returned to the UI, not persisted (no secrets in DB).
"""

import logging
import time

logger = logging.getLogger(__name__)


async def test_integration(integration: str, db) -> dict:
    """
    Run a connectivity test for the named integration.
    Returns {integration, status, message, latency_ms}.
    """
    tester = TESTERS.get(integration) or MANUAL_TESTERS.get(integration)
    if not tester:
        return {"integration": integration, "status": "error", "message": "No test available", "latency_ms": None}

    t0 = time.time()
    try:
        result = await tester(db)
        ms = round((time.time() - t0) * 1000)
        return {**result, "integration": integration, "latency_ms": ms}
    except Exception as e:
        ms = round((time.time() - t0) * 1000)
        logger.error(f"Integration test {integration} failed: {e}")
        return {"integration": integration, "status": "error", "message": str(e)[:200], "latency_ms": ms}


async def _test_librenms(db) -> dict:
    """Test LibreNMS API connectivity."""
    from app.modules.settings.repository import get_setting_value
    url = await get_setting_value(db, "librenms", "url")
    token = await get_setting_value(db, "librenms", "api_token")
    if not url or not token:
        return {"status": "error", "message": "URL or token not configured"}

    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{url.rstrip('/')}/api/v0/system", headers={"X-Auth-Token": token})
    if resp.status_code == 200:
        return {"status": "ok", "message": "Connected"}
    return {"status": "error", "message": f"HTTP {resp.status_code}"}


async def _test_paxton(db) -> dict:
    """Test Paxton Net2 API connectivity by attempting real authentication."""
    import asyncio
    import ssl
    from app.modules.settings.repository import get_setting_value
    url = await get_setting_value(db, "paxton", "url")
    username = await get_setting_value(db, "paxton", "username")
    password = await get_setting_value(db, "paxton", "password")
    client_id = await get_setting_value(db, "paxton", "client_id")
    verify_raw = (await get_setting_value(db, "paxton", "ssl_verify") or "true").lower()
    verify_ssl = verify_raw not in ("false", "0", "no", "off")

    if not url:
        return {"status": "error", "message": "URL not configured"}
    if not username or not password:
        return {"status": "error", "message": "Username/password not configured"}

    def _auth_test():
        import json
        import urllib.request
        import urllib.error
        token_url = f"{url.rstrip('/')}/api/v1/authorization/tokens"
        data = json.dumps({
            "username": username,
            "password": password,
            "grant_type": "password",
            "client_id": client_id or "",
        }).encode()
        req = urllib.request.Request(token_url, data=data, headers={"Content-Type": "application/json"})
        # Match the adapter: skip cert verification when admin set ssl_verify=false.
        # Net2's stock self-signed cert otherwise blocks every request.
        ctx = None
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            resp = urllib.request.urlopen(req, timeout=10, context=ctx)
            result = json.loads(resp.read())
            if result.get("access_token"):
                return {"status": "ok", "message": "Authenticated successfully"}
            return {"status": "error", "message": "No access_token in response"}
        except urllib.error.HTTPError as e:
            return {"status": "error", "message": f"HTTP {e.code}: {e.reason}"}
        except urllib.error.URLError as e:
            return {"status": "error", "message": f"Cannot reach server: {str(e.reason)[:100]}"}

    try:
        return await asyncio.to_thread(_auth_test)
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}


async def _test_ad(db) -> dict:
    """Test Active Directory LDAP connectivity."""
    import asyncio
    from app.modules.settings.repository import get_setting_value
    server = await get_setting_value(db, "ad", "server")
    if not server:
        return {"status": "error", "message": "Server not configured"}

    # Simple TCP check on port 389
    def _tcp_check():
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect((server, 389))
            s.close()
            return True
        except Exception:
            return False

    ok = await asyncio.to_thread(_tcp_check)
    if ok:
        return {"status": "ok", "message": f"LDAP port open on {server}"}
    return {"status": "error", "message": f"Cannot reach {server}:389"}


async def _test_google(db) -> dict:
    """Test Google API connectivity (OAuth endpoint)."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get("https://www.googleapis.com/oauth2/v3/tokeninfo")
    # 400 means the endpoint is reachable (we're not passing a token)
    if resp.status_code in (200, 400):
        return {"status": "ok", "message": "Google API reachable"}
    return {"status": "error", "message": f"HTTP {resp.status_code}"}


async def _test_redis(db) -> dict:
    """Test Redis connectivity."""
    import redis.asyncio as aioredis
    from app.config import get_settings
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        pong = await r.ping()
        await r.aclose()
        return {"status": "ok", "message": "PONG"} if pong else {"status": "error", "message": "No PONG"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}


async def _test_database(db) -> dict:
    """Test PostgreSQL connectivity."""
    import sqlalchemy as sa
    try:
        await db.execute(sa.text("SELECT 1"))
        return {"status": "ok", "message": "Connected"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}


async def _test_switch_ssh(db) -> dict:
    """Test switch SSH connectivity."""
    import asyncio
    from app.modules.settings.repository import get_setting_value
    host = await get_setting_value(db, "switch_ssh", "test_host")
    username = await get_setting_value(db, "switch_ssh", "username")
    if not host or not username:
        return {"status": "error", "message": "SSH test_host or username not configured"}
    port = int(await get_setting_value(db, "switch_ssh", "port") or "22")

    def _tcp_check():
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            return False

    ok = await asyncio.to_thread(_tcp_check)
    if ok:
        return {"status": "ok", "message": f"SSH port {port} open on {host}"}
    return {"status": "error", "message": f"Cannot reach {host}:{port}"}


async def _test_switch_telnet(db) -> dict:
    """Test switch Telnet connectivity."""
    import asyncio
    from app.modules.settings.repository import get_setting_value
    host = await get_setting_value(db, "switch_telnet", "test_host")
    username = await get_setting_value(db, "switch_telnet", "username")
    if not host or not username:
        return {"status": "error", "message": "Telnet test_host or username not configured"}
    port = int(await get_setting_value(db, "switch_telnet", "port") or "23")

    def _tcp_check():
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            return False

    ok = await asyncio.to_thread(_tcp_check)
    if ok:
        return {"status": "ok", "message": f"Telnet port {port} open on {host}"}
    return {"status": "error", "message": f"Cannot reach {host}:{port}"}


# ── Registry ─────────────────────────────────────────────────────────────
# Future integrations: add a tester function and register it here.
# The dashboard integration status panel and /api/settings/test-all
# will automatically pick it up.

async def _test_hr_sheets(db) -> dict:
    """Test Google Sheets API access by reading the first configured
    per-building HR Google Sheet. Lite reconfigured hr_sheets.buildings
    to a per-building JSON map (see Settings → HR Google Sheets), so
    we grab whichever is first + do a cheap spreadsheets.get() against it."""
    import asyncio, json
    from app.modules.settings.repository import get_setting_value
    from app.integrations.google.credentials import load_service_account_credentials

    admin_email = await get_setting_value(db, "google", "admin_email")
    raw = await get_setting_value(db, "hr_sheets", "buildings")
    try:
        sheets_cfg = json.loads(raw or "{}")
    except Exception:
        sheets_cfg = {}
    sheet_id = next(
        (v.get("sheet_id") for v in sheets_cfg.values() if (v or {}).get("sheet_id")),
        None,
    )

    if not sheet_id:
        return {"status": "error", "message": "No HR sheets configured — add one in Settings → HR Google Sheets"}
    if not admin_email:
        return {"status": "error", "message": "google.admin_email not configured"}

    creds = await load_service_account_credentials(
        db,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        subject=admin_email,
    )
    if not creds:
        return {"status": "error", "message": "Google service account not configured — paste JSON key in Settings → Google Workspace"}

    def _test():
        from googleapiclient.discovery import build
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        title = result.get("properties", {}).get("title", "?")
        return {"status": "ok", "message": f"Connected: {title}"}

    try:
        return await asyncio.to_thread(_test)
    except Exception as e:
        return {"status": "error", "message": str(e)[:150]}


async def _test_hr_smb(db) -> dict:
    """Test SMB share connectivity for HR Excel file."""
    from app.integrations.smb.adapter import SmbExcelAdapter
    adapter = SmbExcelAdapter(db)
    return await adapter.test_connection()


async def _test_transfinder(db) -> dict:
    """Test Transfinder SFTP connectivity."""
    import asyncio
    from app.modules.settings.repository import get_setting_value
    host = await get_setting_value(db, "transfinder", "sftp_host")
    username = await get_setting_value(db, "transfinder", "sftp_username")
    password = await get_setting_value(db, "transfinder", "sftp_password")
    if not host or not username:
        return {"status": "error", "message": "SFTP not configured"}
    port = int(await get_setting_value(db, "transfinder", "sftp_port") or "22")

    def _test():
        import paramiko
        transport = paramiko.Transport((host, port))
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.close()
        transport.close()

    try:
        await asyncio.to_thread(_test)
        return {"status": "ok", "message": f"SFTP connected to {host}"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}


async def _test_drive(db) -> dict:
    """Test Google Drive API access."""
    from app.integrations.google.drive_adapter import GoogleDriveAdapter
    adapter = GoogleDriveAdapter(db)
    return await adapter.test_connection()


async def _test_chromebook(db) -> dict:
    """Test Chrome OS Devices API access."""
    from app.integrations.chromebook.adapter import ChromebookAdapter
    adapter = ChromebookAdapter(db)
    return await adapter.test_connection()


async def _test_grandstream(db) -> dict:
    """Test Grandstream UCM API connectivity (singleton session — safe)."""
    from app.integrations.grandstream.adapter import ucm_session
    async with ucm_session(db) as ucm:
        return await ucm.test_connection()


async def _test_aruba_instant(db) -> dict:
    """Test SSH connectivity to Aruba Instant master AP."""
    from app.modules.settings.repository import get_setting_value
    ip = await get_setting_value(db, "aruba_instant", "master_ip")
    username = await get_setting_value(db, "aruba_instant", "username")
    password = await get_setting_value(db, "aruba_instant", "password")
    if not ip or not username:
        return {"status": "skip", "message": "Not configured"}
    from app.integrations.aruba_instant.adapter import test_connection
    return await test_connection(ip, username, password)


async def _test_voice(db) -> dict:
    """Test voice dispatch server connectivity (nexus-voice container)."""
    import os
    import time
    import httpx
    url = os.environ.get("VOICE_SERVER_URL", "http://127.0.0.1:8100")
    try:
        t0 = time.time()
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{url}/health")
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200 and r.json().get("ok"):
            return {"status": "ok", "latency_ms": ms}
        return {"status": "error", "message": f"Voice server returned {r.status_code}"}
    except Exception as e:
        return {"status": "error", "message": f"Voice server unreachable: {e!s:.80}"}


async def _test_wave(db, server_id: int) -> dict:
    try:
        from app.integrations.wave.adapter import WaveAdapter
        adapter = WaveAdapter(db, server_id)
        result = await adapter.test_connection()
        if result["ok"]:
            return {
                "status": "ok",
                "message": f"{result['server_name']}: {result['camera_count']} cameras",
                "latency_ms": result.get("latency_ms"),
            }
        return {"status": "error", "message": result.get("error", "Connection failed")[:100]}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}

async def _test_wave_server_1(db) -> dict:
    return await _test_wave(db, 1)

async def _test_wave_server_2(db) -> dict:
    return await _test_wave(db, 2)


async def _test_gemini(db) -> dict:
    """Verify Gemini API key with a cheap metadata call (no tokens spent)."""
    from app.modules.settings.repository import get_setting_value
    api_key = await get_setting_value(db, "gemini", "api_key")
    api_key = (api_key or "").strip()
    if not api_key:
        return {"status": "error", "message": "API key not configured"}
    if not api_key.startswith("AIza"):
        return {"status": "error", "message": "Key doesn't look like a Gemini key (should start with 'AIza')"}

    import httpx
    # ListModels endpoint — authenticates the key without consuming tokens.
    # Key goes in x-goog-api-key header, NOT query string, so it doesn't
    # end up in any request logs if the call fails.
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers={"x-goog-api-key": api_key})
    if resp.status_code == 200:
        count = len(resp.json().get("models", []))
        return {"status": "ok", "message": f"Authenticated — {count} models available"}
    if resp.status_code in (401, 403):
        return {"status": "error", "message": "Invalid API key"}
    try:
        detail = resp.json().get("error", {}).get("message", "")[:80]
    except Exception:
        detail = ""
    return {"status": "error", "message": f"HTTP {resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}"}


TESTERS = {
    "librenms": _test_librenms,
    "paxton": _test_paxton,
    "ad": _test_ad,
    "google": _test_google,
    "redis": _test_redis,
    "database": _test_database,
    "switch_ssh": _test_switch_ssh,
    "switch_telnet": _test_switch_telnet,
    "drive": _test_drive,
    "chromebook": _test_chromebook,
    "grandstream": _test_grandstream,
    "hr_sheets": _test_hr_sheets,
    "hr_smb": _test_hr_smb,
    "aruba_instant": _test_aruba_instant,
    "voice": _test_voice,
    "wave_server_1": _test_wave_server_1,
    "wave_server_2": _test_wave_server_2,
    "gemini": _test_gemini,
    # transfinder excluded from auto-test to avoid excess SFTP connections
}

# Manual-only testers (available via /api/settings/test/{name} but not in test-all)
MANUAL_TESTERS = {
    "transfinder": _test_transfinder,
}
