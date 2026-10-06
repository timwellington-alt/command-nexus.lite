"""
Staff workflow service -- orchestrates provisioning and offboarding.

Called from router endpoints, not from request handlers directly.
Each step is individually tracked with before/after state.

Idempotency:
- execute_onboard checks for existing successful runs before starting
- On partial runs, already-successful steps are skipped
- New workflow run created only when re-execution is warranted

District-agnostic:
- Email domain loaded from google.domain in Settings, not hardcoded
- Google OU and AD OU loaded from settings (google.ou_staff, ad.ou_prefix)
- Falls back to sensible defaults but warns if not configured
"""

import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.staff.models import StaffRequest, StaffWorkflowRun
from app.modules.staff import repository as repo
from app.audit.service import log_action

logger = logging.getLogger(__name__)


import re

def build_staff_email(first_name: str, last_name: str, domain: str, template: str = "{first}.{last}@{domain}") -> str:
    """
    Build a staff email from a configurable template.

    Available tags:
      {first}          — full first name, lowercased, non-alpha removed
      {last}           — full last name, lowercased, non-alpha removed
      {first_initial}  — first character of first name
      {domain}         — email domain

    Handles edge cases: hyphens removed, spaces removed, unicode stripped.
    """
    first_clean = re.sub(r"[^a-z]", "", first_name.strip().lower())
    last_clean = re.sub(r"[^a-z]", "", last_name.strip().lower())
    first_initial = first_clean[0] if first_clean else ""

    return template.format(
        first=first_clean,
        last=last_clean,
        first_initial=first_initial,
        domain=domain,
    )


def build_staff_password(first_name: str, last_name: str, template: str = "Welcome{first_initial}{last}1!") -> str:
    """Build a staff default password from a configurable template."""
    first_clean = re.sub(r"[^a-z]", "", first_name.strip().lower())
    last_clean = re.sub(r"[^a-z]", "", last_name.strip().lower())
    first_initial = first_clean[0].upper() if first_clean else ""

    return template.format(
        first=first_clean,
        last=last_clean,
        first_initial=first_initial,
    )


_PW_ADJECTIVES = (
    "Blue", "Red", "Green", "Bright", "Silver", "Golden", "Happy", "Sunny",
    "Quiet", "Brave", "Kind", "Swift", "Tall", "Tiny", "Cozy", "Fuzzy",
    "Rocky", "Sandy", "Snowy", "Foggy", "Mighty", "Lucky", "Merry", "Jolly",
    "Cool", "Warm", "Fresh", "Fancy", "Sharp", "Smooth", "Bold", "Clever",
    "Lively", "Mellow", "Nimble", "Peppy", "Sturdy", "Zesty", "Grand", "Noble",
    "Royal", "Wise", "Witty", "Breezy", "Frosty", "Sleepy", "Speedy", "Cheery",
)
_PW_NOUNS = (
    "Panda", "Tiger", "Otter", "Falcon", "Dolphin", "Rabbit", "Turtle", "Beaver",
    "Comet", "Meadow", "River", "Canyon", "Harbor", "Island", "Prairie", "Sunset",
    "Willow", "Boulder", "Cedar", "Maple", "Cactus", "Orchid", "Pebble", "Ember",
    "Wagon", "Compass", "Lantern", "Anchor", "Bridge", "Cabin", "Rocket", "Kettle",
    "Sparrow", "Robin", "Heron", "Puffin", "Badger", "Squirrel", "Moose", "Elk",
    "Piano", "Guitar", "Drum", "Trumpet", "Banjo", "Fiddle", "Harp", "Whistle",
)


def _generate_temp_password() -> str:
    """Generate a memorable but random temp password: Adjective + Noun + 2 digits.

    Format: `BlueFalcon42` — mixed case + digits (clears Google Workspace's
    default 8-char minimum) but short and speakable over the phone. Chosen
    from `_PW_ADJECTIVES × _PW_NOUNS × 10..99` for ~215K combos, which is
    fine for a one-time password immediately rotated on first login. Every
    reset generates a fresh one via secrets.SystemRandom so nothing is
    predictable.
    """
    rng = secrets.SystemRandom()
    adj = rng.choice(_PW_ADJECTIVES)
    noun = rng.choice(_PW_NOUNS)
    digits = rng.randint(10, 99)
    return f"{adj}{noun}{digits}"


async def _get_onboard_config(db) -> dict:
    """
    Load district-specific provisioning config from Settings.
    All values the district admin enters in the Settings UI -- not hardcoded.
    """
    from app.modules.settings.repository import get_setting_value
    return {
        "google_domain": await get_setting_value(db, "google", "domain") or "",
        "google_ou_staff": await get_setting_value(db, "google", "ou_staff") or "/Users-Staff",
        "ad_ou_prefix": await get_setting_value(db, "ad", "ou_prefix") or "OU=Staff",
    }


async def _get_paxton_id_from_onboard_steps(db, request_id: int) -> int | None:
    """
    Scan prior onboard workflow step records for a Paxton result.
    Used as a last-resort fallback in offboarding when neither AD username
    nor Google email resolves a staff_links row.
    """
    runs = await repo.get_workflow_runs(db, request_id)
    for run in runs:
        if getattr(run, "run_type", None) != "onboard":
            continue
        steps = await repo.get_workflow_steps(db, run.id)
        for step in steps:
            if step.step_name == "paxton" and step.status == "success" and step.result_data:
                import json
                try:
                    data = json.loads(step.result_data)
                    pid = data.get("paxton_id")
                    if pid:
                        return int(pid)
                except Exception:
                    pass
    return None


async def _get_existing_step_results(db, request_id: int) -> dict:
    """
    For idempotency: find the most recent partial run and return
    which steps already succeeded, so they can be skipped on re-run.
    Returns {step_name: result_data_dict} for successful steps.
    """
    runs = await repo.get_workflow_runs(db, request_id)
    if not runs:
        return {}

    # Most recent run first
    latest = runs[0]
    if latest.status not in ("partial", "running"):
        return {}

    steps = await repo.get_workflow_steps(db, latest.run_id if hasattr(latest, 'run_id') else latest.id)
    completed = {}
    for step in steps:
        if step.status == "success" and step.result_data:
            import json
            try:
                completed[step.step_name] = json.loads(step.result_data)
            except Exception:
                completed[step.step_name] = {}
    return completed


async def execute_onboard(db: AsyncSession, request_id: int, actor: str) -> dict:
    """
    Execute onboard workflow for a staff request.

    Steps: Google -> AD -> Paxton -> Notification
    Each step tracked independently. Failed steps don't block others.
    Temp password is never persisted in DB -- in-memory only.

    Idempotency: if a prior run completed successfully, returns immediately.
    On partial runs, previously successful steps are skipped.
    """
    req = await repo.get_request(db, request_id)
    if not req:
        return {"status": "error", "message": "Request not found"}

    # Load district config from Settings -- not hardcoded
    config = await _get_onboard_config(db)
    if not config["google_domain"]:
        return {"status": "error", "message": "google.domain not configured in Settings"}

    # Idempotency check: if already fully provisioned, skip
    existing_runs = await repo.get_workflow_runs(db, request_id)
    for existing in existing_runs:
        if existing.status == "complete" and existing.run_type == "onboard":
            return {"status": "complete", "message": "Already provisioned", "errors": []}

    # Find prior partial run results so we can skip completed steps
    prior_successes = await _get_existing_step_results(db, request_id)

    # Create new workflow run for this execution attempt
    run = await repo.create_workflow_run(db, request_id, "onboard", actor)
    results = {}
    errors = []

    # Load provisioning profile for group assignments
    from app.modules.staff.provisioning_profiles import get_profile
    profile = await get_profile(db, req.building, req.role_type)

    # Build identity values from request data + district config
    first = req.first_name.lower().replace(" ", "")
    last = req.last_name.lower().replace(" ", "")
    email = f"{first}.{last}@{config['google_domain']}"
    username = f"{first}.{last}"

    # Password consistency rule:
    # A single temp_password is generated per full onboard attempt.
    # On retry, if Google already succeeded in a prior run, the account
    # already has a password set -- we must NOT generate a new one for AD,
    # as the credentials would be inconsistent across systems.
    # If Google was already done, AD is also skipped (it either already ran
    # or will need manual intervention). The password can only be set
    # consistently when both Google and AD run in the same execution.
    google_already_done = "google" in prior_successes
    ad_already_done = "ad" in prior_successes

    # Only generate a temp password when at least one identity step will actually run.
    # If both are already done, no password is needed and returning a fresh one
    # would be misleading — it doesn't correspond to any account change.
    password_needed = not google_already_done or not ad_already_done
    temp_password = _generate_temp_password() if password_needed else None

    if google_already_done and not ad_already_done:
        # Google account exists with an unknown password -- AD cannot be safely
        # provisioned with a new password. Mark AD as requiring manual intervention.
        logger.warning(
            f"Onboard retry for request {request_id}: Google already done but AD not. "
            "Cannot set a consistent password -- AD step will be skipped. "
            "Admin must manually set the AD password to match the existing Google account."
        )

    # ── Step: Google ──
    if google_already_done:
        # Already succeeded in a prior partial run -- carry result forward
        results["google_email"] = prior_successes["google"].get("email")
        logger.info(f"Skipping Google step for {request_id} -- already completed")
    else:
        google_step = await repo.create_workflow_step(db, run.id, "google")
        google_step.started_at = datetime.now(timezone.utc)
        try:
            from app.integrations.google.adapter import GoogleWorkspaceAdapter
            google = GoogleWorkspaceAdapter(db)
            # Use profile OU if available, otherwise fall back to config-based
            google_ou = profile.get("google_ou") or _get_google_ou(req.building, req.role_type, config)
            result = await google.create_account(
                email=email,
                first_name=req.first_name,
                last_name=req.last_name,
                org_unit=google_ou,
                temp_password=temp_password,
            )
            if result.success:
                results["google_email"] = email
                # Add to Google groups from provisioning profile
                group_results = []
                for group_email in profile.get("google_groups", []):
                    if "@" not in group_email:
                        group_email = f"{group_email}@{config['google_domain']}"
                    gr = await google.add_to_group(email, group_email)
                    group_results.append({"group": group_email, "added": gr.added})
                await repo.update_step(db, google_step,
                    status="success",
                    before_state={"existed": False},
                    after_state={"email": email, "ou": google_ou},
                    result_data={"email": email, "groups": group_results},
                )
            else:
                results["google_email"] = None
                await repo.update_step(db, google_step,
                    status="failed",
                    error_message=result.error,
                )
                errors.append(f"Google: {result.error}")
        except Exception as e:
            await repo.update_step(db, google_step, status="failed", error_message=str(e)[:200])
            errors.append(f"Google: {e}")

    # ── Step: AD ──
    if ad_already_done:
        results["ad_username"] = prior_successes["ad"].get("username")
        logger.info(f"Skipping AD step for {request_id} -- already completed")
    elif google_already_done and not ad_already_done:
        # Password mismatch risk -- skip AD and flag for manual intervention
        ad_step = await repo.create_workflow_step(db, run.id, "ad")
        await repo.update_step(db, ad_step, status="skipped",
            result_data={"reason": "Google already provisioned with unknown password -- AD requires manual setup to ensure consistent credentials"})
        errors.append("AD: skipped — Google already provisioned; passwords would be inconsistent. Admin must set AD password manually.")
    else:
        ad_step = await repo.create_workflow_step(db, run.id, "ad")
        ad_step.started_at = datetime.now(timezone.utc)
        try:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad = ActiveDirectoryAdapter(db)
            # Use profile OU if available, otherwise fall back to config-based
            ad_ou = profile.get("ad_ou") or _get_ad_ou(req.building, req.role_type, config)
            result = await ad.create_account(
                username=username,
                first_name=req.first_name,
                last_name=req.last_name,
                ou_dn=ad_ou,
                password=temp_password,
            )
            if result.success:
                # Add to AD groups from provisioning profile
                ad_group_results = []
                for group_name in profile.get("ad_groups", []):
                    gr = await ad.add_to_group(username, group_name)
                    ad_group_results.append({"group": group_name, "added": gr.success})
            await repo.update_step(db, ad_step,
                status="success" if result.success else "failed",
                before_state={"existed": False},
                after_state={"username": username, "dn": result.dn, "ou": ad_ou} if result.success else None,
                result_data={"username": username, "groups": ad_group_results if result.success else []} if result.success else None,
                error_message=result.error,
            )
            results["ad_username"] = username if result.success else None
            if not result.success:
                errors.append(f"AD: {result.error}")
        except Exception as e:
            await repo.update_step(db, ad_step, status="failed", error_message=str(e)[:200])
            errors.append(f"AD: {e}")

    # ── Step: Paxton ──
    if "paxton" in prior_successes:
        logger.info(f"Skipping Paxton step for {request_id} -- already completed")
    else:
        paxton_step = await repo.create_workflow_step(db, run.id, "paxton")
        paxton_step.started_at = datetime.now(timezone.utc)
        try:
            from app.integrations.paxton.adapter import PaxtonAdapter
            paxton = PaxtonAdapter(db)
            # Look up access level from provisioning profile
            access_level_id = await paxton.get_access_level_for_profile(req.building, req.role_type)

            result = await paxton.create_cardholder(
                first_name=req.first_name,
                last_name=req.last_name,
                email=email if results.get("google_email") else None,
                access_level_id=access_level_id,
            )
            if result.success and access_level_id and result.paxton_id:
                # Assign the access level after creation
                await paxton.update_user_access(result.paxton_id, access_level_id)

            await repo.update_step(db, paxton_step,
                status="success" if result.success else "failed",
                after_state={"paxton_id": result.paxton_id, "access_level_id": access_level_id} if result.success else None,
                result_data={"paxton_id": result.paxton_id, "access_level": access_level_id} if result.success else None,
                error_message=result.error,
            )
            if result.success:
                results["paxton_id"] = result.paxton_id
            else:
                errors.append(f"Paxton: {result.error}")
        except Exception as e:
            await repo.update_step(db, paxton_step, status="failed", error_message=str(e)[:200])
            errors.append(f"Paxton: {e}")

    # ── Step: Notification ──
    notif_step = await repo.create_workflow_step(db, run.id, "notification")
    notif_step.started_at = datetime.now(timezone.utc)
    if results.get("google_email"):
        try:
            await repo.update_step(db, notif_step, status="skipped",
                result_data={"reason": "principal email routing not yet configured"})
        except Exception as e:
            await repo.update_step(db, notif_step, status="failed", error_message=str(e)[:200])
    else:
        await repo.update_step(db, notif_step, status="skipped",
            result_data={"reason": "no Google email to notify about"})

    # ── Finalize run ──
    run.completed_at = datetime.now(timezone.utc)
    run.status = "complete" if not errors else ("partial" if results else "failed")
    run.error_summary = "; ".join(errors) if errors else None

    req.status = run.status
    req.provisioned_by = actor
    req.provisioned_at = datetime.now(timezone.utc)

    # Persist identity link whenever we have any anchor — AD username, Google email,
    # or Paxton ID. Gating on ad_username alone leaves Paxton IDs orphaned when AD
    # is skipped, which breaks offboard physical-access removal.
    if any([results.get("ad_username"), results.get("google_email"), results.get("paxton_id")]):
        await repo.upsert_link(db,
            ad_username=results.get("ad_username"),
            google_email=results.get("google_email"),
            paxton_id=results.get("paxton_id"),
        )

    await log_action(db,
        actor=actor,
        action="staff.provision.onboard",
        module="staff",
        target=f"{req.first_name} {req.last_name}",
        details=f"status={run.status}, errors={len(errors)}",
    )

    # Return temp_password only if it was generated AND at least one identity
    # step succeeded with it. Never return a password that wasn't used.
    password_was_used = (
        temp_password is not None
        and (results.get("google_email") or results.get("ad_username"))
        and not (google_already_done and not ad_already_done)
    )
    return {
        "status": run.status,
        "google_email": results.get("google_email"),
        "ad_username": results.get("ad_username"),
        "temp_password": temp_password if password_was_used else None,
        "errors": errors,
    }


async def execute_offboard(db: AsyncSession, request_id: int, actor: str) -> dict:
    """
    Execute offboard workflow. Steps: Google suspend -> AD disable -> Paxton remove.

    All three systems must be addressed. Physical access (Paxton) removal
    is as critical as account suspension.
    """
    req = await repo.get_request(db, request_id)
    if not req:
        return {"status": "error", "message": "Request not found"}

    run = await repo.create_workflow_run(db, request_id, "offboard", actor)
    errors = []

    # ── Google suspend ──
    if req.existing_email:
        step = await repo.create_workflow_step(db, run.id, "google")
        step.started_at = datetime.now(timezone.utc)
        try:
            from app.integrations.google.adapter import GoogleWorkspaceAdapter
            google = GoogleWorkspaceAdapter(db)
            result = await google.suspend_account(req.existing_email)
            await repo.update_step(db, step,
                status="success" if result.success else "failed",
                before_state={"suspended": False},
                after_state={"suspended": True} if result.success else None,
                error_message=result.error,
            )
            if not result.success:
                errors.append(f"Google: {result.error}")
        except Exception as e:
            await repo.update_step(db, step, status="failed", error_message=str(e)[:200])
            errors.append(f"Google: {e}")
    else:
        step = await repo.create_workflow_step(db, run.id, "google")
        await repo.update_step(db, step, status="skipped",
            result_data={"reason": "no existing_email provided"})

    # ── AD disable ──
    if req.existing_ad_username:
        step = await repo.create_workflow_step(db, run.id, "ad")
        step.started_at = datetime.now(timezone.utc)
        try:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad = ActiveDirectoryAdapter(db)
            result = await ad.disable_account(req.existing_ad_username)
            await repo.update_step(db, step,
                status="success" if result.success else "failed",
                before_state={"enabled": True},
                after_state={"enabled": False} if result.success else None,
                error_message=result.error,
            )
            if not result.success:
                errors.append(f"AD: {result.error}")
        except Exception as e:
            await repo.update_step(db, step, status="failed", error_message=str(e)[:200])
            errors.append(f"AD: {e}")
    else:
        step = await repo.create_workflow_step(db, run.id, "ad")
        await repo.update_step(db, step, status="skipped",
            result_data={"reason": "no existing_ad_username provided"})

    # ── Paxton remove ──
    # Resolve Paxton ID using the best available anchor, in priority order:
    # 1. AD username  →  get_link_by_username
    # 2. Google email →  get_link_by_email
    # 3. Prior onboard step result  →  get_link_by_paxton_id
    #
    # Path 3 covers the edge case where a link was written with only a Paxton
    # anchor (AD and Google both skipped), which would be invisible to paths 1/2.
    paxton_id = None
    link_source = None
    if req.existing_ad_username:
        link = await repo.get_link_by_username(db, req.existing_ad_username)
        if link and link.paxton_id:
            paxton_id = link.paxton_id
            link_source = f"ad_username={req.existing_ad_username}"
    if paxton_id is None and req.existing_email:
        link = await repo.get_link_by_email(db, req.existing_email)
        if link and link.paxton_id:
            paxton_id = link.paxton_id
            link_source = f"email={req.existing_email}"
    if paxton_id is None:
        # Last resort: scan prior onboard step records for a Paxton result
        prior_paxton_id = await _get_paxton_id_from_onboard_steps(db, request_id)
        if prior_paxton_id:
            link = await repo.get_link_by_paxton_id(db, prior_paxton_id)
            if link:
                paxton_id = link.paxton_id
                link_source = f"paxton_step_record={prior_paxton_id}"
            else:
                # The step result exists but no link row — use the raw ID directly
                paxton_id = prior_paxton_id
                link_source = f"paxton_step_record_no_link={prior_paxton_id}"

    step = await repo.create_workflow_step(db, run.id, "paxton")
    step.started_at = datetime.now(timezone.utc)
    if paxton_id:
        try:
            from app.integrations.paxton.adapter import PaxtonAdapter
            paxton = PaxtonAdapter(db)
            result = await paxton.remove_access(paxton_id)
            await repo.update_step(db, step,
                status="success" if result.success else "failed",
                before_state={"paxton_id": paxton_id, "has_access": True},
                after_state={"has_access": False} if result.success else None,
                error_message=result.error,
            )
            if not result.success:
                errors.append(f"Paxton: {result.error}")
        except Exception as e:
            await repo.update_step(db, step, status="failed", error_message=str(e)[:200])
            errors.append(f"Paxton: {e}")
    else:
        await repo.update_step(db, step, status="skipped",
            result_data={"reason": "no Paxton ID found in staff_links via AD username or email — manual check required"})
        logger.warning(
            f"Offboard for {req.first_name} {req.last_name}: no Paxton link found "
            f"(tried ad_username={req.existing_ad_username!r}, email={req.existing_email!r}). "
            "Physical access may remain active. Manual verification required."
        )

    # Finalize
    run.completed_at = datetime.now(timezone.utc)
    run.status = "complete" if not errors else "partial"
    run.error_summary = "; ".join(errors) if errors else None
    req.status = run.status
    req.provisioned_by = actor
    req.provisioned_at = datetime.now(timezone.utc)

    await log_action(db,
        actor=actor,
        action="staff.provision.offboard",
        module="staff",
        target=f"{req.first_name} {req.last_name}",
        details=f"status={run.status}, errors={len(errors)}, paxton_id={paxton_id}, link_source={link_source}",
    )

    return {"status": run.status, "errors": errors}


# ── OU mappings (district-configurable) ───────────────────────────────────

def _get_google_ou(building: str, role_type: str, config: dict) -> str:
    """
    Build Google OU path. Uses ou_staff from Settings as the base.
    Districts configure their own OU structure -- not hardcoded here.
    Example: config["google_ou_staff"] = "/Users-Staff"
    """
    base = config.get("google_ou_staff", "/Users-Staff").rstrip("/")
    return f"{base}/{building}" if building else base


def _get_ad_ou(building: str, role_type: str, config: dict) -> str:
    """
    Build AD OU distinguished name. Uses ou_prefix from Settings as the base.
    Districts configure their own OU structure -- not hardcoded here.
    Example: config["ad_ou_prefix"] = "OU=Staff"
    """
    from app.integrations.ad.adapter import _escape_dn_component
    prefix = config.get("ad_ou_prefix", "OU=Staff")
    if building:
        safe_building = _escape_dn_component(building)
        safe_role = _escape_dn_component(role_type.capitalize())
        return f"OU={safe_building}-{safe_role},{prefix}"
    return prefix
