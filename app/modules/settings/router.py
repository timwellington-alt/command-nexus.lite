"""
Settings module — admin-only config surface.

Grouped by integration. Raw secrets never returned.
Feature toggles managed separately.
Integration health checks via T6.3.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import get_db
from app.db.models import User
from app.policies.engine import require_action, get_user_permissions, check_permission
from app.audit.service import log_action
from app.modules.settings.schemas import SettingUpdate, FeatureToggleUpdate
from app.modules.settings.repository import (
    get_integration_settings,
    upsert_integration_setting,
    get_all_integrations,
    get_feature_toggles,
    set_feature_toggle,
    SECRET_MASK,
)
from app.policies.page_context import build_page_modules

logger = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])
templates = Jinja2Templates(directory="app/templates")

# Setting definitions per integration: (key, label, is_secret_ref)
SETTING_GROUPS = {
    # ── General (shared infrastructure) ──────────────────────────────
    "branding": {
        "label": "District Branding",
        "category": "General",
        "fields": [
            ("district_name", "District Name (e.g. Wildcat) — used for the UI header + tab title", False),
            ("district_legal_name", "District Legal Name (e.g. the district) — used on legal/compliance forms (HB 96 vendor questionnaire, etc.)", False),
            ("brand_color", "Brand Color (hex, e.g. #58a6ff)", False),
            ("timezone", "Timezone (e.g. America/New_York)", False),
            ("app_url", "Application URL (e.g. https://app.example.com)", False),
            ("building_colors", "Building Colors JSON (code → hex color)", False),
        ],
    },
    "google": {
        "label": "Google Workspace",
        "category": "General",
        "fields": [
            ("admin_email", "Admin Email", False, False),
            ("clever_gmail_user", "Clever Gmail User", False, False),
            ("domain", "Google Domain (e.g. school.org)", False, False),
            ("student_domain", "Student Domain (e.g. studentdomain.org)", False, False),
            ("ou_staff", "Staff OU Paths", False, True),
            ("ou_students", "Student OU Paths", False, True),
            ("hr_sheet_id", "HR Intake Sheet ID", False, False),
            ("deprovision_ou", "Deprovision OU (e.g. /Archived Accounts)", False),
            ("non_person_patterns", "Non-person email patterns (regex, one per tag) — filtered from staff sync", False, True),
        ],
    },
    "role_sync": {
        "label": "Google Group → Role Mapping",
        "category": "General",
        "fields": [],
    },
    # ── Staff ────────────────────────────────────────────────────────
    "ad": {
        "label": "Active Directory",
        "category": "Staff",
        "fields": [
            ("server", "AD Server IP", False),
            ("domain", "AD Domain", False),
            ("base_dn", "Base DN", False),
            ("bind_user", "Bind User", False),
            ("bind_password", "Bind Password", True),
            ("ou_prefix", "Staff OU Prefix (e.g. OU=Staff)", False),
            ("tls_mode", "TLS Mode (ldaps / starttls / none)", False),
            ("deprovision_ou", "Deprovision OU (e.g. OU=Accounts to be Deleted)", False),
            ("staff_email_template", "Staff Email Template — tags: {first} {last} {first_initial} {domain}", False),
            ("staff_default_password", "Staff Default Password Template — tags: {first} {last} {first_initial}", False),
        ],
    },
    "hr_smb": {
        "label": "HR Excel (SMB Share)",
        "category": "Staff",
        "fields": [
            ("server", "Server IP or Hostname", False),
            ("share", "Share Name (e.g. username$)", False),
            ("path", "File Path (e.g. 1-FILES/Addresses/file.xlsx)", False),
            ("username", "SMB Username", False),
            ("password", "SMB Password", True),
            ("domain", "Windows Domain (e.g. MYDISTRICT)", False),
        ],
    },
    "hr_sheets": {
        "label": "HR Google Sheets",
        "category": "Staff",
        "fields": [
            ("hr_sheet_id", "HR Master Sheet ID", False),
            ("coaches_sheet_id", "Coaches Sheet ID", False),
        ],
    },
    "room_roster": {
        "label": "Room Rosters",
        "category": "Staff",
        "fields": [
            ("buildings", "Per-building room roster sheets (managed below)", False),
            ("sheet_range", "Default sheet range (used when a building entry doesn't specify one)", False),
        ],
    },
    "staff": {
        "label": "Staff Provisioning",
        "category": "Staff",
        "fields": [
            ("staff_notifications", "Per-building notification + printer config (managed below)", False),
            ("badge_printing_enabled", "Print temp badge + credentials label on provision", False),
            ("badge_printer_model", "Brother label printer model (default: QL-820NWB — QL-820NWBc is the same protocol)", False),
            ("badge_label_media", "Label media code (default: 62 for 62mm continuous)", False),
        ],
    },
    # ── Students ─────────────────────────────────────────────────────
    "roster": {
        "label": "Import Settings",
        "category": "Students",
        "fields": [
            ("import_time", "Daily Import Time (EST)", False, False),
            (
                "clever_metasolutions_sender",
                "Roster Report Sender (authoritative)",
                False,
                False,
                "Only ingest Students/Teachers/Enrollments/etc. emails when the From: address matches this value. Prevents ambiguous subject matches from a legacy or shadow report sender leaking stale kids into the roster. Example: ReportingServices@metasolutions.net",
            ),
            (
                "clever_metasolutions_subject",
                "Roster Report Subject (optional narrow)",
                False,
                False,
                "Optional. If set, further narrows ingest to emails whose subject matches this string. Leave blank to accept any subject from the sender above.",
            ),
            ("clever_subjects", "Clever Email Subjects", False, True),
            ("import_interval_minutes", "Import Poll Interval (minutes, default 60)", False, False),
            ("sis_import_url", "SIS Email Import URL (link in UI)", False, False),
            ("student_email_domain", "Student Email Domain (e.g. school.net)", False, False),
            (
                "student_provisioning_enabled",
                "Enable student account provisioning",
                False,
                False,
                "Master switch for the student provisioning pipeline. When OFF, no student account actions happen anywhere. When ON, the guidance queue and reconciliation tools can stage provisioning work — but actual writes to Google still require the next switch.",
            ),
            (
                "student_google_writes_enabled",
                "Enable automated Google account writes for students",
                False,
                False,
                "When ON, the scheduler may create/suspend/move student Google accounts automatically based on the guidance queue and HR/SIS data. When OFF, queue entries still get built and counselors/admins can review them, but no changes are pushed to Google Workspace until an admin manually clicks Provision. Keep this OFF until the workflow has been validated end-to-end.",
            ),
            ("student_default_password", "Default Password Template (e.g. 0000{SID_LAST4})", False, False),
            ("student_deprovision_ou", "Student Archive OU (e.g. /Archived Accounts)", False, False),
            (
                "reconcile_enabled",
                "Enable nightly roster↔Google reconciliation",
                False,
                False,
                "Master switch for the reconcile_student_google_state job. When OFF the job is a no-op. When ON the job runs nightly, populating student_reconcile_candidates. Google writes still require reconcile_dry_run=false below.",
            ),
            (
                "reconcile_dry_run",
                "Reconciliation dry-run (log candidates only, no writes)",
                False,
                False,
                "When ON, the reconcile job walks both directions and records everything it WOULD do in student_reconcile_candidates but does not touch Google. Flip to OFF once you've reviewed a few nights of candidates and are comfortable with the classification.",
            ),
            (
                "reconcile_min_roster_active",
                "Sanity-gate floor (abort if roster_snapshots active count < this)",
                False,
                False,
                "Directory-diff sanity gate. If a mid-refresh window leaves roster_snapshots temporarily empty or truncated, the reconcile job would mass-archive real students. Set this above your normal steady-state count (default 1500). See feedback_dir_diff_sanity_gate memory.",
            ),
            (
                "reconcile_stale_days",
                "How long a kid can be missing from the SIS feed before archive-eligible (days)",
                False,
                False,
                "Direction B floor. Default 30. Not currently enforced beyond lastlogin_min_days but retained for future extension.",
            ),
            (
                "reconcile_lastlogin_min_days",
                "Never archive a student whose Google account logged in within N days",
                False,
                False,
                "Safety floor for direction B. Default 180. A kid may be missing from the SIS feed but if they logged into Google in the last N days they're still using the account — skip until confirmed. Increase for a more conservative posture.",
            ),
            (
                "reconcile_max_per_run",
                "Maximum direction-B archives per single run",
                False,
                False,
                "Hard cap on direction-B (archive) writes per single run. Default 50. Overflow candidates get recorded in student_reconcile_candidates with decision=queue_max_per_run_cap so you can review before bumping the cap.",
            ),
            (
                "withdraw_cascade_floor",
                "Withdrawal cascade sanity floor (kids)",
                False,
                False,
                "Max number of withdrawals the SIS import will apply in a single run. Default 100. If the district-wide diff after processing today's Students CSVs would withdraw MORE than this AND more than withdraw_cascade_max_pct, the run aborts the deprovision (audit row + skipped guidance queue). Raise temporarily for legitimate mass events (year-end).",
            ),
            (
                "withdraw_cascade_max_pct",
                "Withdrawal cascade sanity cap (% of active roster)",
                False,
                False,
                "Second axis of the withdrawal sanity gate. Default 5 (percent). Aborts alongside withdraw_cascade_floor when the would-be withdrawal set exceeds this fraction of the currently-active roster. A partial CSV usually trips both.",
            ),
            (
                "reconcile_district_email_domains",
                "District email domains for reconciliation (comma-separated)",
                False,
                True,
                "Direction A: SIS emails whose domain is not in this list are recorded as decision=non_district_email (not archived, not errored) so an operator can update the SIS record. Falls back to student_email_domain if empty. Example: yourdistrict.org",
            ),
            (
                "reconcile_ignore_emails",
                "Ignore list for shared / non-student accounts in student OUs (comma-separated)",
                False,
                True,
                "Direction B: exact emails to skip (recorded as decision=ignored_shared_account). Any local-part without a grad-year digit suffix is auto-detected as decision=non_student_shared_account. Use this list for anything else that lives in a student OU but shouldn't be archived (peslibrary@…, testphs@…, poffice@…).",
            ),
        ],
    },
    "guidance": {
        "label": "Guidance Scheduling",
        "category": "Students",
        "fields": [
            (
                "notify_enabled",
                "Email counselors when new queue entries appear",
                False,
                False,
                "When ON, counselor groups receive an email each time new enrollees/withdrawals land in the guidance queue. Turning this OFF silences emails but does NOT stop queue entries from being created — the queue still fills up in the background so counselors can review it manually. Leave OFF until counselors have been briefed on the new workflow.",
            ),
            (
                "auto_queue_recent_days",
                "Queue scan window (days)",
                False,
                False,
                "How far back to look for new enrollments and recent withdrawals when auto-populating the guidance queue. Default is 3 days. Increase temporarily after a long gap (e.g. start-of-year) to catch older entries, then drop it back down to avoid re-queuing resolved students.",
            ),
            ("counselor_groups", "Counselor group configuration (managed below)", False),
            ("notify_subject", "Email subject — {count} {school} {group}", False),
            ("notify_body", "Email body (HTML) — {count} {school} {group} {student_table}", False),
        ],
    },
    "nutrikids": {
        "label": "NutriKids",
        "category": "Students",
        "fields": [
            ("smb_server", "File Server IP", False),
            ("smb_share", "Share Name (e.g. lunchdata$)", False),
            ("smb_username", "Username", False),
            ("smb_password", "Password", True),
            ("smb_domain", "Domain (leave blank for local account)", False),
            ("mdb_path", "MDB File Path (relative to share root)", False),
        ],
    },
    "clever_custom_sections": {
        "label": "Clever Custom Sections",
        "category": "Roster",
        "fields": [
            ("sftp_host", "SFTP host (e.g. sftp.clever.com — same server as the SIS import)", False),
            ("sftp_port", "SFTP port (default 22)", False),
            ("sftp_username", "SFTP username", False),
            ("sftp_password", "SFTP password", True),
            ("sftp_remote_path", "Remote folder name (default 'customsections' — must exist under the SFTP root; Clever docs say to create it manually the first time)", False),
            ("sheet_id", "Google Sheet ID of the intervention/custom-sections spreadsheet (the string between /d/ and /edit in the URL)", False),
            ("sync_enabled", "Set to 'true' to actually SFTP-push sections.csv + enrollments.csv on the nightly run. When blank/false the job runs in DRY-RUN — it builds the CSVs to /app/docs/clever_custom_out/{date}/ for you to inspect, but never uploads.", False),
            ("min_rows_floor", "Minimum row count required across all buildings before an upload runs (safety net against wiping Clever with an empty sheet — default 1)", False),
        ],
    },
    # ── External APIs ─────────────────────────────────────────────────
    "google_maps": {
        "label": "Google Maps / Places",
        "category": "External APIs",
        "fields": [
            ("api_key", "Maps JavaScript + Places API key (HTTP-referrer restricted)", True),
        ],
    },
    "search": {
        "label": "Brave Search",
        "category": "External APIs",
        "fields": [
            ("brave_api_key", "Brave Search API Key — used by inventory replacement-parts lookup. Get one at https://api.search.brave.com/app/keys (free tier: 1 req/sec, 2000/month).", True),
        ],
    },
    # ── Access ───────────────────────────────────────────────────────
    "halo": {
        "label": "HALO 2.20 (vape detectors)",
        "category": "Security",
        "warning": (
            "HALO 2.20 is PUSH-ONLY. Configure each unit's Heartbeat and "
            "Integration URLs using the templates in docs/halo_integration.md. "
            "Auth: paste the same random token into BOTH `webhook_token` "
            "below AND the password field on every HALO's webhook config "
            "(LEAVE the HALO's username field blank — that's what makes "
            "%PSWD% substitute as a literal header value instead of triggering "
            "Basic/Digest auth). Receiver checks X-Halo-Secret in constant time."
        ),
        "fields": [
            ("webhook_token", "Webhook auth token. Paste the same value into each HALO's password field; leave its username blank. Constant-time compared against X-Halo-Secret on every heartbeat/event.", True),
            ("stale_seconds", "Mark a HALO offline if no heartbeat in this many seconds (default 300; recommended heartbeat cadence is 60s).", False),
        ],
    },
    "paxton": {
        "label": "Paxton Net2",
        "category": "Access",
        "fields": [
            ("url", "Paxton URL — e.g. https://192.0.2.1:8443 (no trailing slash, no /api/v1)", False),
            ("username", "Username", False),
            ("password", "Password", True),
            ("client_id", "Client ID", False),
            ("ssl_verify", "Verify SSL certificate (true/false). Set to false for the stock Net2 self-signed cert.", False),
            ("custom_field_email", "Custom Field ID for Email (default 9)", False),
            ("custom_field_title", "Custom Field ID for Title (default 10)", False),
            ("provision_dept_id", "Default department for newly provisioned users", False),
            ("deprovision_dept_id", "Deprovision Department ID (e.g. 34)", False),
        ],
    },
    # ── Security — Wisenet Wave VMS ─────────────────────────────────
    "wave_server_1": {
        "label": "Wisenet Wave VMS (Server 1)",
        "category": "Security",
        "fields": [
            ("url", "Server URL (e.g. https://192.0.2.1:7001)", False),
            ("username", "API Username", False),
            ("password", "API Password", True),
            ("name", "Friendly Name (e.g. Main Campus)", False),
            ("ssl_verify", "Verify SSL certificate (true/false, default true)", False),
        ],
    },
    "wave_server_2": {
        "label": "Wisenet Wave VMS (Server 2)",
        "category": "Security",
        "fields": [
            ("url", "Server URL (e.g. https://192.0.2.1:7001)", False),
            ("username", "API Username", False),
            ("password", "API Password", True),
            ("name", "Friendly Name (e.g. Annex)", False),
            ("ssl_verify", "Verify SSL certificate (true/false, default true)", False),
        ],
    },
    # ── Weather ──────────────────────────────────────────────────────
    "weather": {
        "label": "Weather & Lightning",
        "category": "Infrastructure",
        "fields": [
            ("latitude", "Campus Latitude (e.g. 36.8354)", False),
            ("longitude", "Campus Longitude (e.g. -76.2983)", False),
            ("radius_miles", "Lightning search radius in miles (default 25)", False),
        ],
    },
    "openweather": {
        "label": "OpenWeather",
        "category": "External APIs",
        "fields": [
            ("api_key", "OpenWeather API Key — free tier is enough for a 5-min per-campus poll. Get one at https://openweathermap.org/api", True),
        ],
    },
    # ── Camera Credentials ────────────────────────────────────────────
    "camera_hikvision": {
        "label": "Hikvision Camera Credentials",
        "category": "Security",
        "fields": [
            ("username", "Username", False),
            ("password", "Password", True),
        ],
    },
    "camera_hanwha": {
        "label": "Hanwha Camera Credentials",
        "category": "Security",
        "fields": [
            ("username", "Username", False),
            ("password", "Password", True),
        ],
    },
    "rei": {
        "label": "REI Bus Camera DVRs",
        "category": "Security",
        "fields": [
            ("default_password", "Fleet-wide REI account password (HMAC-MD5'd at auth time)", True),
            ("nas_path", "NAS root for HD5 output (default: /mnt/nas-footage/rei_bus)", False),
            ("pad_before_seconds", "Default seconds to pad BEFORE the operator-supplied window (default 120)", False),
            ("pad_after_seconds", "Default seconds to pad AFTER the operator-supplied window (default 300)", False),
        ],
    },
    "student_scan": {
        "label": "Student Drive Scanner",
        "category": "Security",
        "fields": [
            ("scope_ous", "Student OU paths to scan (one per line, e.g. /Students/PHS)", False),
            ("builtin_catalogues", "Active catalogues — built-in static lists + external (TBLP) refresh-daily lists (default: proxy,games,vpn,tblp_piracy,tblp_gambling,tblp_redirect)", False),
            ("custom_domains", "Additional domains to flag (one per line, no scheme/path)", False),
            ("trigger_keywords", "Keyword pre-filter triggers (one per line, blank = no pre-filter)", False),
            ("known_file_names", "Known-bad file name patterns (one per line, case-insensitive substring match — e.g. 'school sites' flags any doc whose title contains that). Auto-flags the doc even when no URL match.", False),
            ("repeat_offender_threshold", "Hit count above which a student is a repeat offender (default 3)", False),
            ("notify_emails", "Future-use: comma-list of emails to notify when threshold hit", False),
        ],
    },
    # ── Network ──────────────────────────────────────────────────────
    "librenms": {
        "label": "LibreNMS",
        "category": "External APIs",
        "fields": [
            ("url", "LibreNMS URL", False),
            ("api_token", "API Token", True),
        ],
    },
    "network": {
        "label": "Config Backup",
        "category": "Infrastructure",
        "fields": [
            ("config_backup_enabled", "Enable scheduled switch config backups", False),
            ("config_backup_dir", "Backup directory (default: data/config_backups)", False),
            ("config_backup_vendors", "Vendor rules — one entry per rule, format keyword=device_type (e.g. procurve=hp_procurve)", False, True),
            ("core_switch_ip", "Core switch IP for VLAN map (e.g. 192.0.2.1)", False),
            ("core_switch_username", "Core switch SSH username (if different from Switch SSH)", False),
            ("core_switch_password", "Core switch SSH password (if different from Switch SSH)", True),
        ],
    },
    "switch_ssh": {
        "label": "Switch SSH",
        "category": "Infrastructure",
        "fields": [
            ("username", "SSH Username", False),
            ("password", "SSH Password", True),
            ("enable_password", "Enable Password", True),
            ("port", "SSH Port (default 22)", False),
            ("test_host", "Test Host IP", False),
        ],
    },
    "switch_telnet": {
        "label": "Switch Telnet",
        "category": "Infrastructure",
        "fields": [
            ("username", "Telnet Username", False),
            ("password", "Telnet Password", True),
            ("enable_password", "Enable Password", True),
            ("port", "Telnet Port (default 23)", False),
            ("test_host", "Test Host IP", False),
        ],
    },
    "aruba_instant": {
        "label": "Aruba Instant (Wireless)",
        "category": "Infrastructure",
        "fields": [
            ("master_ip", "Aruba Instant master AP IP (e.g. 192.0.2.1)", False),
            ("username", "SSH username", False),
            ("password", "SSH password", True),
            ("poll_interval", "Client poll interval in seconds (default 120)", False),
        ],
    },
    "epson_projectors": {
        "label": "Epson Projectors (PJLink)",
        "category": "Infrastructure",
        "warning": "PJLink password is optional — Epson BrightLinks ship with auth disabled. Set this only if you've locked PJLink down via the projector's Web Control. Fleet-wide single shared password.",
        "fields": [
            ("pjlink_password", "PJLink authentication password (blank for no-auth)", True),
        ],
    },
    "spectrum_outages": {
        "label": "Spectrum Outage Tile",
        "category": "External APIs",
        "warning": "Data comes from Cloudflare Radar (free tier). Create a Cloudflare API token with Read access to Radar and paste it below. Token at: dash.cloudflare.com → My Profile → API Tokens.",
        "fields": [
            ("area", "City + state for the tile (e.g. 'the district, OH'). Only the state portion is used to filter Radar's outage list.", False),
            ("api_token", "Cloudflare API token (Radar Read permission)", True),
        ],
    },
    "google_chrome_printers": {
        "label": "Google Admin — Chrome Printers Sync",
        "category": "Infrastructure",
        "warning": "DISABLED 2026-08-27 — Google Admin-managed Chrome printers proved unreliable in practice (tray-select limits, no PPD URL exposed, per-user setup friction). Leave sync_enabled OFF unless you're actively re-piloting the flow. Even with the setting ON, you'd also have to re-add the scheduler entry in scheduler.py. Service account scope requirement below is retained for future reference.",
        "fields": [
            (
                "sync_enabled",
                "Enable daily Google Admin Chrome printer sync",
                False,
                False,
                "Master switch. When OFF (default), the sync_chrome_printers job is a no-op even if something enqueues it — protects against a manual re-trigger silently pushing more printers into Google Admin. Flip to ON only if you also re-add the scheduler entry AND intend to accept the tray/PPD/setup limitations documented above.",
            ),
            ("delegate_subject", "Domain admin email the service account impersonates (e.g. admin@yourdistrict.org)", False),
            ("customer_id", "Google customer ID (use 'my_customer' to auto-detect)", False),
            ("staff_ou_path", "Org Unit path where new printers land (e.g. '/Staff')", False),
            ("default_make_model", "Default Canon model match for new entries (e.g. 'Canon iR-ADV 6275')", False),
            ("server_fqdn", "Print server FQDN used in printer URIs (e.g. printserver.yourdistrict.local)", False),
        ],
    },
    "canon_printers": {
        "label": "Canon Printers (Remote UI)",
        "category": "Infrastructure",
        "warning": "Form-field names vary by Canon firmware revision. Leave the defaults if auto-login works; tweak if the printer's login page rejects the submitted form. iR-ADV and imageFORCE shared baseline below.",
        "fields": [
            ("admin_user", "System Manager ID / Department ID (single value used for every printer)", False),
            ("admin_password", "System Manager / Department password (same for every printer)", True),
            ("login_path", "Remote UI login form path (default '/login/login.html')", False),
            ("userid_field", "Form field name for the user/ID (default 'i0014' — Canon obfuscates these)", False),
            ("password_field", "Form field name for the password (default 'i0015')", False),
        ],
    },
    "printers": {
        "label": "Printer Alerting",
        "category": "Infrastructure",
        "fields": [
            ("offline_alert_threshold_minutes",
             "Minutes a printer must be continuously offline before a routed voice/push alert dispatches. Default 20 — printers flap on sleep/paper-jam, so a lower value creates noise. Applies to any device with device_type='printer' in the LibreNMS cache.",
             False),
        ],
    },
    "acus": {
        "label": "Door Controller (ACU) Alerting",
        "category": "Infrastructure",
        "fields": [
            ("offline_alert_threshold_minutes",
             "Minutes a Paxton door ACU must be continuously offline before a routed high-severity alert dispatches. Default 5 — ACUs are physical-security devices, so quicker is better; briefly flapping ACUs get debounced but genuine outages page quickly. Data source: paxton_acu_status (populated by probe_paxton_acus).",
             False),
        ],
    },
    # ── Chromebook ───────────────────────────────────────────────────
    # ── Tickets (work-order / repair system) ─────────────────────────
    "tickets": {
        "label": "Tickets / Work Orders",
        "category": "Operations",
        "fields": [
            ("routing_rules", "Triage owners — auto-assignee per (category, sub-category, building). When someone files a ticket, the first matching rule stamps the named person as the initial assignee. They work the ticket or reassign as needed. (managed below)", False),
            ("workers", "Worker rosters — who can be assigned a ticket in each category. Routing rules + manual assignment both reject targets not on the roster. (managed below)", False),
            ("urgent_dial_extension_too", "Urgent: also ring the assignee's internal extension (in addition to their cell). Two parallel calls — first answered wins. Set to 'true' or 'false'.", False),
            ("urgent_voice_template", "Urgent voice call summary template. Click a tag chip below to insert it.", False),
            ("urgent_push_title", "Urgent push notification title template. Click a tag chip below to insert it.", False),
            ("urgent_push_body", "Urgent push notification body template. Click a tag chip below to insert it.", False),
        ],
    },
    "chromebook": {
        "label": "Chromebook Management",
        "category": "Chromebook",
        "fields": [
            ("default_ou", "Default OU filter (e.g. /Chromebooks)", False),
            ("stale_threshold", "Stale device threshold (number of days since last sync)", False),
            ("known_wan_ips", "Known district WAN IPs (one per tag)", False, True),
            ("label_format", "Label format (e.g. avery_5160)", False),
            ("label_code_type", "Label code type (barcode or qrcode)", False),
            ("label_prefix", "Default asset tag prefix (e.g. CB-)", False),
            ("label_district_name", "District name printed on labels", False),
            ("label_printers", "Label printers (managed below — add one per physical Brother P-touch on the network)", False),
            # legacy single-printer keys — preserved for backward compat and
            # auto-migrated into the printers list above when first edited.
            ("label_printer_host", "[legacy] Single P-touch IP; replaced by 'Label printers' list above", False),
            ("label_printer_tape", "[legacy] TZe tape width for the single-printer setup", False),
            ("room_pods", "Cart pod groupings per building (managed below — group room numbers by grade so within-pod movement isn't flagged as wandering)", False),
            ("wander_alerts_enabled", "Wander alerts master switch — set to 'false' to silence all cart-wander emails without losing the per-building EOD / admin config. Default: enabled.", False),
            ("wander_after_hours_threshold", "After-hours threshold (HH:MM) — buildings without pod groupings only flag wandering carts after this time (e.g. 15:00 for 3 PM dismissal)", False),
            ("wander_eod_times", 'Per-building EOD time (JSON, e.g. {"PES":"15:30","PHS":"15:00"}) — cart wander check fires at each building\'s EOD. Editable per building.', False),
            ("wander_escalation_days", "Days a device must remain wandered before escalating to the building admin (default 2)", False),
            ("wander_admin_emails", 'Per-building admin email for wander escalations (JSON, e.g. {"PES":"admin@…"})', False),
        ],
    },
    "chromebook_repair": {
        "label": "Chromebook Repair — Model Reference Notes",
        "category": "Chromebook",
        "fields": [
            ("model_reference_notes",
             'Per-model repair reference notes for the AI troubleshooting-tips '
             'job (JSON, keyed by generation tag). Each value is a short '
             'free-text block of district-specific knowledge, known-bad parts, '
             'or service-manual URLs. Example: '
             '{"G5": "Hinge screws strip easily; use Loctite Blue on reassembly. '
             'Service manual: hp.com/…", "G6": "…", "G7": "…", "_generic": "…"}. '
             'The tag is auto-detected from chromebook_model (matches /\\bG\\d\\b/); '
             'unmatched models fall back to _generic if defined.',
             False),
        ],
    },
    "chromebook_parts": {
        "label": "Chromebook Parts — Low-stock alerts",
        "category": "Chromebook",
        "fields": [
            ("low_stock_threshold",
             "Threshold below which a (building, part-category) combination is "
             "flagged as low-stock. Default 2. Applied to the /chromebook/parts "
             "page (red highlight) and the nightly low-stock alert email.",
             False),
            ("low_stock_notify_emails",
             "Comma-separated staff emails to notify when any part falls below "
             "the threshold. Nightly digest — at most one email per address per "
             "day even if multiple parts trigger.",
             False),
        ],
    },
    # ── Phones ───────────────────────────────────────────────────────
    "grandstream": {
        "label": "Grandstream UCM",
        "category": "Phones",
        "warning": "CRITICAL: Call Control must be DISABLED on the UCM (Maintenance → HTTP API). Enabling Call Control causes the API to interfere with the PBX call processor, resulting in phone system failure for all inbound calls.",
        "fields": [
            ("url", "UCM URL (e.g. https://192.168.1.100:8089)", False),
            ("api_user", "API Username", False),
            ("api_password", "API Password", True),
            ("building_map", "Extension range → Building (e.g. 1000-1199=PHS, 3500-3899=CO, 3=EPE)", False, True),
        ],
    },
    "voice": {
        "label": "Voice Module (SIP)",
        "category": "Phones",
        "warning": (
            "AuthID on the UCM extension MUST be populated and equal to the extension number. "
            "An empty AuthID causes plain SIP REGISTER to silently return 404 Not Found even though "
            "the extension exists. Check Extensions → (your ext) → AuthID field before saving."
        ),
        "fields": [
            # ── SIP connector ───────────────────────────────────────
            ("sip_host", "UCM IP (e.g. 192.0.2.1)", False),
            ("sip_port", "SIP Port (default 5060)", False),
            ("sip_transport", "Transport (udp / tcp / tls)", False),
            ("sip_extension", "SIP Extension (e.g. 4000)", False),
            ("sip_username", "SIP Username (usually same as extension)", False),
            ("sip_password", "SIP Password", True),
            ("display_name", "Caller ID Name (shown on recipient phones)", False),
            # ── TTS voice ────────────────────────────────────────────
            ("tts_voice", "TTS Voice", False, False,
             "espeak is built-in (robotic but works). Google voices require a GCP billing account + Cloud Text-to-Speech API enabled. Piper requires a local .onnx voice model installed in the voice container."),
            ("google_tts_speed", "Google TTS Speaking Rate (0.5–2.0, default 1.0)", False, False,
             "Only used when a Google voice is selected. 1.0 is normal speed."),
            # ── IVR messages ─────────────────────────────────────────
            ("ivr_greeting", "IVR Greeting Message", False, False,
             "Played when a whitelisted caller connects. Keep it short."),
            ("ivr_pin_prompt", "IVR PIN Prompt", False, False,
             "Played once after the greeting. Caller enters PIN + pound key."),
            ("ivr_pin_fail", "IVR Incorrect PIN Message", False, False,
             "Played when the PIN is wrong. Don't repeat the full prompt — just the error."),
            ("ivr_menu", "IVR Menu Message", False, False,
             "Main menu options. Barge-in enabled — caller can press a key to skip ahead."),
            ("ivr_goodbye", "IVR Goodbye Message", False, False,
             "Played before hanging up."),
            # Per-user alert preferences (severity threshold, quiet hours,
            # priority) live on each user's /me profile page — they're not
            # global defaults anymore.
        ],
    },
    # ── Transportation ───────────────────────────────────────────────
    "transfinder": {
        "label": "Transfinder",
        "category": "Transportation",
        "fields": [
            (
                "auto_export_enabled",
                "Enable scheduled Transfinder export",
                False,
                False,
                "Master switch for the automated SFTP export. When OFF, "
                "the scheduled job is skipped entirely regardless of the "
                "time/days below. Manual exports via the Transportation "
                "page still work either way.",
            ),
            ("sheet_id", "Google Sheet ID (supplementary data)", False),
            ("sheet_range", "Sheet Range (default: A1:Z5000)", False),
            ("sftp_host", "SFTP Host", False),
            ("sftp_port", "SFTP Port (default: 22)", False),
            ("sftp_username", "SFTP Username", False),
            ("sftp_password", "SFTP Password", True),
            ("sftp_path", "SFTP Remote Path (e.g. /incoming)", False),
            ("sftp_filename", "CSV Filename (e.g. supplementary.csv)", False),
            ("auto_export_time", "Export Time (EST, e.g. 6:00)", False),
            ("auto_export_days", "Export Days", False, True),
            ("notify_emails", "Notification Emails", False, True),
            ("notify_on_export", "Email notification on scheduled export", False),
            ("notify_subject", "Email Subject Line (default: Transfinder Export Complete)", False),
        ],
    },
    # ── Operations ───────────────────────────────────────────────────
    "drive": {
        "label": "Google Drive (Documentation)",
        "category": "Operations",
        "fields": [
            ("root_folder_id", "Documentation Root Folder ID (from Drive URL)", False),
        ],
    },
    # ── Inventory ────────────────────────────────────────────────────
    "inventory": {
        "label": "Inventory",
        "category": "Operations",
        "fields": [
            ("capitalization_threshold_cents", "Capitalization threshold in cents — items at/above this get an auto-assigned asset tag (default: 50000 = $500)", False),
            ("asset_tag_prefix", "Asset tag prefix (default: NEX — tags look like NEX-00001)", False),
            ("box_tag_prefix", "Box/carton tag prefix for receiving (default: BOX)", False),
            ("loan_overdue_days", "Days past expected return before a loan is 'overdue' (default: 1)", False),
            ("label_template", "Default label template: avery_5160 / avery_5161 / avery_8160 (default: avery_5160)", False),
        ],
    },
    "order_email": {
        "label": "Order Email Parser",
        "category": "Operations",
        "fields": [
            ("order_email_inbox", "Inbox Gmail user to watch for forwarded purchase confirmations", False),
            ("order_email_subject_marker", "Subject marker — only messages with this word in the subject are processed (default: 'order'). Leave blank to disable.", False),
            ("order_email_allowed_forwarders", "Optional allowlist — only orders forwarded by these accounts are ingested. Leave blank to accept any forwarder.", False, True),
            ("order_parsers_enabled", "Enabled vendor parsers (generic, cdw, amazon, bh, dell)", False, True),
            ("order_email_poll_minutes", "How often to poll Gmail for new forwarded orders (default: 10)", False),
        ],
    },
    "treasurer_forward": {
        "label": "Treasurer PO Forwarding",
        "category": "Operations",
        "fields": [
            ("treasurer_email", "Treasurer recipient email (primary)", False),
            ("treasurer_cc", "CC list for treasurer forwards", False, True),
            ("treasurer_subject_template", "Subject template — tags: {po_number} {vendor} (default: 'PO {po_number} — {vendor} — shipping docs')", False),
            ("treasurer_forward_enabled", "Enable automatic forwarding on complete-order", False),
        ],
    },
    # ── Building Population (DHCP ingest) ───────────────────────────────
    "dhcp_ingest": {
        "label": "DHCP Lease Ingest",
        "category": "Infrastructure",
        "fields": [
            ("drop_path", "Path to leases.json inside the API container (default: /dhcp/leases.json)", False),
            ("stale_threshold_minutes", "Refuse to ingest a lease file older than this many minutes (default: 15)", False),
            ("snapshot_retention_days", "How many days of population_snapshots to retain (default: 90)", False),
            ("sftp_listen_port", "SFTP server listen port — Phase 2, default 2222", False),
            ("sftp_username", "SFTP upload username — Phase 2, default 'dhcp_push'", False),
            ("sftp_authorized_key", "Windows DHCP server SSH public key (ed25519) — Phase 2", True),
        ],
    },
    # ── AI / LLM Providers ──────────────────────────────────────────────
    "gemini": {
        "label": "Google Gemini",
        "category": "External APIs",
        "fields": [
            ("api_key", "Gemini API Key (from aistudio.google.com/apikey) — free tier is plenty for inventory lookups", True),
            ("model", "Model name — leave blank for gemini-2.5-flash (free tier + vision). Alternatives: gemini-2.5-flash-lite (cheaper, text-only), gemini-flash-latest (always-current alias, may change tier)", False),
        ],
    },
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.config import get_settings as _gs
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "local_auth_enabled": _gs().local_auth_enabled,
    })


@router.get("/api/settings")
async def get_all_settings(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Return all settings grouped by integration, with secret values masked."""
    all_settings = await get_integration_settings(db)

    # Index by integration.key
    by_key = {}
    for s in all_settings:
        by_key[f"{s['integration']}.{s['key']}"] = s

    # Build grouped response using SETTING_GROUPS for structure.
    # Field tuple format:
    #   (key, label, is_secret)                              — 3-tuple
    #   (key, label, is_secret, is_textarea)                 — 4-tuple
    #   (key, label, is_secret, is_textarea, description)    — 5-tuple
    # description is optional free-text rendered under the input as
    # muted helper copy so operators know what each knob does.
    groups = {}
    for integration, group_def in SETTING_GROUPS.items():
        fields = []
        for f in group_def["fields"]:
            key, label, is_secret = f[0], f[1], f[2]
            description = f[4] if len(f) > 4 else None
            setting = by_key.get(f"{integration}.{key}")
            fields.append({
                "key": key,
                "label": label,
                "value": setting["value"] if setting else "",
                "is_secret_ref": is_secret,
                "description": description,
            })
        groups[integration] = {
            "label": group_def["label"],
            "category": group_def.get("category", "General"),
            "warning": group_def.get("warning"),
            "fields": fields,
        }

    return groups


@router.post("/api/settings")
async def save_settings(
    body: SettingUpdate,
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Save settings for a single integration."""
    # Validate integration exists
    if body.integration not in SETTING_GROUPS:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unknown integration: {body.integration}")

    group_def = SETTING_GROUPS[body.integration]
    field_map = {f[0]: f[2] for f in group_def["fields"]}  # key → is_secret_ref

    changed = []
    try:
        for key, value in body.settings.items():
            if key not in field_map:
                continue
            is_secret = field_map[key]
            # Skip masked secrets (user didn't change them)
            if is_secret and value == SECRET_MASK:
                continue
            # Only count as changed if the value actually differs from stored
            actually_changed = await upsert_integration_setting(
                db,
                integration=body.integration,
                key=key,
                value=value,
                is_secret_ref=is_secret,
                updated_by=user.email,
            )
            if actually_changed:
                changed.append(key)

        if changed:
            await log_action(
                db,
                actor=user.email,
                action="settings.update",
                module="settings",
                target=body.integration,
                details=f"Changed: {', '.join(changed)}",
                ip_address=request.client.host if request.client else None,
            )
            await db.commit()
            # Invalidate in-process branding cache so a rename takes effect
            # on the next page load instead of waiting for the 5-minute TTL.
            if body.integration == "branding" and any(k in ("district_name", "brand_color") for k in changed):
                from app import branding as _branding
                _branding.invalidate()
                try:
                    await _branding.refresh(db)
                except Exception:
                    pass
    except Exception:
        await db.rollback()
        raise

    return {"status": "ok", "changed": changed}


# ── Feature Toggles ──────────────────────────────────────────────────────

@router.get("/api/settings/toggles")
async def list_toggles(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    return await get_feature_toggles(db)


@router.post("/api/settings/toggles")
async def update_toggle(
    body: FeatureToggleUpdate,
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    try:
        await set_feature_toggle(db, key=body.key, enabled=body.enabled, updated_by=user.email)
        await log_action(
            db,
            actor=user.email,
            action="settings.toggle",
            module="settings",
            target=body.key,
            details=f"{'enabled' if body.enabled else 'disabled'}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {"status": "ok"}


# ── Integration definitions for UI ────────────────────────────────────────

# ── Integration Connection Tests ──────────────────────────────────────────

@router.post("/api/settings/test/{integration}")
async def test_connection(
    integration: str,
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Test connectivity to a configured integration. No secrets exposed in response."""
    from app.modules.settings.connection_tests import test_integration
    result = await test_integration(integration, db)
    await log_action(
        db,
        actor=user.email,
        action="settings.test_connection",
        module="settings",
        target=integration,
        details=f"{result.get('status')}: {result.get('message', result.get('detail', ''))}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return result


@router.post("/api/settings/test-all")
async def test_all_connections(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Test all configured integrations. Returns results per integration."""
    from app.modules.settings.connection_tests import test_integration, TESTERS
    results = []
    for name in TESTERS:
        result = await test_integration(name, db)
        results.append(result)

    # Cache results for the dashboard integration status panel
    from app.modules.dashboard.service import cache_integration_status
    settings = get_settings()
    await cache_integration_status(settings.redis_url, results)

    return {"results": results}


# ── Guidance Counselor Groups ────────────────────────────────────────────

@router.get("/api/settings/guidance-groups")
async def get_guidance_groups(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Get counselor group configuration."""
    import json
    raw = await get_setting_value(db, "guidance", "counselor_groups")
    try:
        groups = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        groups = []
    return {"groups": groups}


@router.put("/api/settings/guidance-groups")
async def save_guidance_groups(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Save counselor group configuration."""
    import json
    body = await request.json()
    groups = body.get("groups", [])

    # Validate
    for g in groups:
        if not g.get("name") or not g.get("building"):
            raise HTTPException(status_code=400, detail="Each group needs a name and building")

    await upsert_integration_setting(db, "guidance", "counselor_groups", json.dumps(groups), is_secret_ref=False, updated_by=user.email)
    await log_action(db, actor=user.email, action="settings.guidance_groups.save",
                     module="settings", target=f"{len(groups)} groups",
                     ip_address=request.client.host if request.client else None)
    await db.commit()
    return {"ok": True, "count": len(groups)}


@router.get("/api/settings/staff-notifications")
async def get_staff_notifications(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Get per-building staff notification config."""
    import json
    raw = await get_setting_value(db, "staff", "staff_notifications")
    try:
        config = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        config = {}
    return {"config": config}


@router.put("/api/settings/staff-notifications")
async def save_staff_notifications(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Save per-building staff notification config.

    Expected format:
    {
        "PES": {"principal": ["email1@..."], "reception": ["email2@..."]},
        "PHS": {"principal": [...], "reception": [...]},
        ...
    }
    """
    import json
    body = await request.json()
    config = body.get("config", {})

    await upsert_integration_setting(
        db, "staff", "staff_notifications", json.dumps(config),
        is_secret_ref=False, updated_by=user.email,
    )
    await log_action(
        db, actor=user.email, action="settings.staff_notifications.save",
        module="settings", target=f"{len(config)} buildings configured",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"ok": True, "buildings": list(config.keys())}


@router.get("/api/buildings")
async def get_buildings(
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint — returns building config for all templates."""
    import json as _json
    from app.modules.settings.repository import get_setting_value
    names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    colors_raw = await get_setting_value(db, "branding", "building_colors") or "{}"
    bmap_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        names = _json.loads(names_raw)
    except Exception:
        names = {}
    try:
        colors = _json.loads(colors_raw)
    except Exception:
        colors = {}
    try:
        bmap = _json.loads(bmap_raw)
    except Exception:
        bmap = {}

    # Default color palette for buildings without configured colors
    palette = ["#58a6ff", "#3fb950", "#d29922", "#a371f7", "#f85149", "#8b949e", "#da3633", "#56d4dd"]

    # Build reverse map: internal code → list of all aliases (SIS codes that map to it)
    reverse_map = {}
    for sis_code, internal_code in bmap.items():
        reverse_map.setdefault(internal_code, []).append(sis_code)

    # Build internal_code → name mapping
    # school_names may be keyed by SIS codes (SIS_B) or internal codes (PHS)
    # Normalize: always use internal codes via school_building_map
    internal_names = {}
    for code, name in names.items():
        # Check if this code is a SIS code that maps to an internal code
        internal = bmap.get(code, code)
        internal_names[internal] = name

    buildings = []
    for i, (code, name) in enumerate(sorted(internal_names.items(), key=lambda x: x[1])):
        aliases = [code, name.lower()]
        # Add SIS codes that map to this building
        for sis_code, internal in bmap.items():
            if internal == code:
                aliases.append(sis_code)
        buildings.append({
            "code": code,
            "name": name,
            "color": colors.get(code) or next((colors[k] for k, v in bmap.items() if v == code and k in colors), palette[i % len(palette)]),
            "aliases": list(set(a.lower() for a in aliases)),
        })

    return {"buildings": buildings, "sis_map": bmap}


@router.get("/api/branding")
async def get_branding(
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint — returns district branding for the UI header."""
    from app.modules.settings.repository import get_setting_value
    name = await get_setting_value(db, "branding", "district_name") or "Command"
    color = await get_setting_value(db, "branding", "brand_color") or "#58a6ff"
    return {"district_name": name, "brand_color": color}


@router.get("/api/settings/staff-search")
async def search_staff(
    q: str = "",
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Search staff directory by name or email for settings dropdowns."""
    if not q or len(q) < 2:
        return {"users": []}
    from sqlalchemy import text
    term = f"%{q}%"
    result = await db.execute(text(
        "SELECT full_name, email FROM staff_directory WHERE full_name ILIKE :q OR email ILIKE :q ORDER BY last_name LIMIT 10"
    ).bindparams(q=term))
    return {"users": [{"name": r[0] or "", "email": r[1] or ""} for r in result.all()]}


@router.get("/api/settings/google-ous")
async def get_google_ous(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Fetch all OUs from Google Workspace for autofill."""
    try:
        from app.integrations.google.adapter import GoogleWorkspaceAdapter
        adapter = GoogleWorkspaceAdapter(db)
        ous = await adapter.list_ous()
        return {"ous": [o["path"] for o in ous]}
    except Exception as e:
        return {"ous": [], "error": str(e)[:150]}


@router.get("/api/settings/building-map")
async def get_building_map(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Return school code → building code mappings."""
    import json as _json
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    return {"mappings": [{"school_code": k, "building_code": v} for k, v in data.items()]}


@router.post("/api/settings/building-map")
async def save_building_mapping(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Add or update a school → building mapping."""
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    body = await request.json()
    school = body.get("school_code", "").strip().upper()
    building = body.get("building_code", "").strip().upper()
    if not school or not building:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Both codes required")
    raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    data[school] = building
    await upsert_integration_setting(db, integration="branding", key="school_building_map", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
    await db.commit()
    return {"status": "ok"}


@router.delete("/api/settings/building-map/{school_code}")
async def delete_building_mapping(
    school_code: str,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a school → building mapping."""
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    school_upper = school_code.strip().upper()
    if school_upper in data:
        del data[school_upper]
        await upsert_integration_setting(db, integration="branding", key="school_building_map", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
        await db.commit()
    return {"status": "ok"}


@router.get("/api/settings/homeroom-periods")
async def get_homeroom_periods(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    import json as _json
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "roster", "homeroom_periods") or "{}"
    names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    try:
        names = _json.loads(names_raw)
    except Exception:
        names = {}
    return {
        "mappings": [{"school_code": k, "name": names.get(k, k), "period": v} for k, v in data.items()],
        "school_names": names,
    }


@router.post("/api/settings/homeroom-periods")
async def save_homeroom_period(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    body = await request.json()
    school = body.get("school_code", "").strip().upper()
    period = body.get("period", "").strip()
    if not school or not period:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Both fields required")
    raw = await get_setting_value(db, "roster", "homeroom_periods") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    data[school] = period
    await upsert_integration_setting(db, integration="roster", key="homeroom_periods", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
    await db.commit()
    return {"status": "ok"}


@router.delete("/api/settings/homeroom-periods/{school_code}")
async def delete_homeroom_period(
    school_code: str,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    raw = await get_setting_value(db, "roster", "homeroom_periods") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    sc = school_code.strip().upper()
    if sc in data:
        del data[sc]
        await upsert_integration_setting(db, integration="roster", key="homeroom_periods", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
        await db.commit()
    return {"status": "ok"}


@router.get("/api/settings/bell-schedules")
async def get_bell_schedules(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Return all building bell schedules."""
    import json as _json
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "roster", "bell_schedules") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    # Also get school names for display
    names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        names = _json.loads(names_raw)
    except Exception:
        names = {}
    buildings = []
    for code, periods in data.items():
        sorted_periods = sorted(periods.items(), key=lambda x: (x[0].isdigit(), int(x[0]) if x[0].isdigit() else 0, x[0]))
        buildings.append({
            "code": code,
            "name": names.get(code, code),
            "periods": [{"period": k, "start": v["start"], "end": v["end"]} for k, v in sorted_periods],
        })
    return {"buildings": buildings, "school_names": names}


@router.post("/api/settings/bell-schedules")
async def save_bell_period(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Add or update a period in a building's bell schedule."""
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    body = await request.json()
    building = body.get("building", "").strip().upper()
    period = body.get("period", "").strip()
    start = body.get("start", "").strip()
    end = body.get("end", "").strip()
    if not building or not period or not start or not end:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Building, period, start, and end are required")
    raw = await get_setting_value(db, "roster", "bell_schedules") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    if building not in data:
        data[building] = {}
    data[building][period] = {"start": start, "end": end}
    await upsert_integration_setting(db, integration="roster", key="bell_schedules", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
    await db.commit()
    return {"status": "ok"}


@router.delete("/api/settings/bell-schedules/{building}/{period}")
async def delete_bell_period(
    building: str,
    period: str,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a period from a building's bell schedule."""
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    raw = await get_setting_value(db, "roster", "bell_schedules") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    b = building.strip().upper()
    if b in data and period in data[b]:
        del data[b][period]
        if not data[b]:
            del data[b]
        await upsert_integration_setting(db, integration="roster", key="bell_schedules", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
        await db.commit()
    return {"status": "ok"}


@router.get("/api/settings/student-ou-map")
async def get_student_ou_map(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Return school code → student Google OU mappings."""
    import json as _json
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "roster", "student_ou_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    return {"mappings": [{"school_code": k, "ou_path": v} for k, v in data.items()]}


@router.post("/api/settings/student-ou-map")
async def save_student_ou_mapping(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    body = await request.json()
    school = body.get("school_code", "").strip().upper()
    ou = body.get("ou_path", "").strip()
    if not school or not ou:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Both school code and OU path required")
    raw = await get_setting_value(db, "roster", "student_ou_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    data[school] = ou
    await upsert_integration_setting(db, integration="roster", key="student_ou_map", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
    await db.commit()
    return {"status": "ok"}


@router.delete("/api/settings/student-ou-map/{school_code}")
async def delete_student_ou_mapping(
    school_code: str,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    raw = await get_setting_value(db, "roster", "student_ou_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    sc = school_code.strip().upper()
    if sc in data:
        del data[sc]
        await upsert_integration_setting(db, integration="roster", key="student_ou_map", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
        await db.commit()
    return {"status": "ok"}


@router.get("/api/settings/role-mappings")
async def get_role_mappings(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Return all Google Group → Role mappings as a list."""
    import json as _json
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "role_sync", "group_role_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    # Get available roles
    from sqlalchemy import text
    roles = await db.execute(text("SELECT name FROM roles ORDER BY name"))
    # Normalize: old format has "role" (string), new format has "roles" (list)
    mappings = []
    for k, v in data.items():
        roles_list = v.get("roles") or ([v["role"]] if v.get("role") else [])
        mappings.append({
            "group_email": k,
            "roles": roles_list,
            "scope": v.get("scope", "district"),
            "scope_value": v.get("scope_value", "*"),
        })
    return {
        "mappings": mappings,
        "roles": [r[0] for r in roles.all()],
    }


@router.post("/api/settings/role-mappings")
async def save_role_mapping(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Add or update a single group → role mapping.

    If the Google Group doesn't exist, auto-creates it with standard
    Nexus security settings before saving the mapping.
    """
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    body = await request.json()
    group_email = body.get("group_email", "").strip().lower()
    # Accept "role" (single string) or "roles" (list)
    roles_input = body.get("roles") or ([body["role"]] if body.get("role") else [])
    scope = body.get("scope", "district").strip()
    scope_value = body.get("scope_value", "*").strip()
    if not group_email or not roles_input:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="group_email and at least one role are required")

    # Auto-create Google Group if it doesn't exist
    group_created = False
    try:
        from app.integrations.google.adapter import GoogleWorkspaceAdapter
        google = GoogleWorkspaceAdapter(db)
        if not await google.group_exists(group_email):
            await google.create_group(group_email)
            group_created = True
            logger.info(f"Auto-created Google Group: {group_email}")
    except Exception as e:
        logger.warning(f"Could not verify/create Google Group {group_email}: {e}")

    raw = await get_setting_value(db, "role_sync", "group_role_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    data[group_email] = {"roles": roles_input, "scope": scope, "scope_value": scope_value}
    await upsert_integration_setting(db, integration="role_sync", key="group_role_map", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
    action_detail = f"{group_email} → {', '.join(roles_input)}"
    if group_created:
        action_detail += " (group created)"
    await log_action(db, actor=user.email, action="settings.role_mapping.update", module="settings", target=action_detail, ip_address=request.client.host if request.client else None)
    await db.commit()
    return {"status": "ok", "group_created": group_created}


@router.delete("/api/settings/role-mappings/{group_email:path}")
async def delete_role_mapping(
    group_email: str,
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a group → role mapping."""
    import json as _json
    from app.modules.settings.repository import get_setting_value, upsert_integration_setting
    raw = await get_setting_value(db, "role_sync", "group_role_map") or "{}"
    try:
        data = _json.loads(raw)
    except Exception:
        data = {}
    if group_email in data:
        del data[group_email]
        await upsert_integration_setting(db, integration="role_sync", key="group_role_map", value=_json.dumps(data), is_secret_ref=False, updated_by=user.email)
        await log_action(db, actor=user.email, action="settings.role_mapping.delete", module="settings", target=group_email, ip_address=request.client.host if request.client else None)
        await db.commit()
    return {"status": "ok"}


@router.get("/api/settings/groups")
async def setting_groups(
    user: User = Depends(require_action("settings.manage")),
):
    """Return setting group definitions for the UI to build the accordion."""
    return {
        k: {"label": v["label"], "category": v.get("category", "General"), "warning": v.get("warning", ""), "fields": [
            {"key": f[0], "label": f[1], "is_secret": f[2], "is_multi": f[3] if len(f) > 3 else False}
            for f in v["fields"]
        ]}
        for k, v in SETTING_GROUPS.items()
    }
