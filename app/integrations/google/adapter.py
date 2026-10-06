"""
Google Workspace Admin SDK adapter.

All credentials loaded via get_setting_value() — Fernet-decrypted transparently.
Never logs credential values. Returns typed results or raises typed exceptions.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class GoogleAccountResult:
    email: str
    success: bool
    error: str | None = None
    before_state: dict | None = None


@dataclass
class GoogleGroupResult:
    group: str
    added: bool
    error: str | None = None


class GoogleWorkspaceAdapter:
    """
    Google Workspace operations for staff identity lifecycle.

    Credentials loaded from integration config on each call —
    supports runtime credential changes without restart.
    """

    def __init__(self, db):
        self.db = db

    async def _get_service(self):
        """Build Admin SDK service from stored credentials."""
        from app.modules.settings.repository import get_setting_value
        import os

        admin_email = await get_setting_value(self.db, "google", "admin_email")
        cred_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/run/secrets/google_service_account.json")

        if not admin_email or not os.path.exists(cred_file):
            raise RuntimeError("Google Workspace not configured")

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = (
            service_account.Credentials
            .from_service_account_file(
                cred_file,
                scopes=["https://www.googleapis.com/auth/admin.directory.user",
                         "https://www.googleapis.com/auth/admin.directory.group",
                         "https://www.googleapis.com/auth/admin.directory.group.member",
                         "https://www.googleapis.com/auth/admin.directory.orgunit.readonly",
                         "https://www.googleapis.com/auth/apps.groups.settings",
                         "https://www.googleapis.com/auth/apps.licensing"],
            )
            .with_subject(admin_email)
        )
        return build("admin", "directory_v1", credentials=creds, cache_discovery=False)

    async def _get_licensing_service(self):
        """Build Licensing API service. Uses the nexus SA — Tim added
        apps.licensing to its DWD entry in Admin Console during the
        earlier licensing-integration attempt. Falls back to GAM's SA
        (mounted at /run/secrets/gam_service_account.json) if nexus SA
        happens to lose the scope. The subject admin (google.admin_email
        setting) must hold a License Management-capable role in Admin
        Console — a plain "User Management Admin" role is NOT enough."""
        from app.modules.settings.repository import get_setting_value
        import os

        admin_email = await get_setting_value(self.db, "google", "admin_email")
        nexus_cred = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/run/secrets/google_service_account.json")
        gam_cred = "/run/secrets/gam_service_account.json"
        cred_file = nexus_cred if os.path.exists(nexus_cred) else gam_cred

        if not admin_email or not os.path.exists(cred_file):
            raise RuntimeError("Google Workspace not configured")

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = (
            service_account.Credentials
            .from_service_account_file(
                cred_file,
                scopes=["https://www.googleapis.com/auth/apps.licensing"],
            )
            .with_subject(admin_email)
        )
        return build("licensing", "v1", credentials=creds, cache_discovery=False)

    async def remove_license(
        self, email: str, product_id: str, sku_id: str,
    ) -> GoogleAccountResult:
        """Revoke a license SKU from a user by shelling out to GAM.

        Why GAM instead of the licensing API directly:
        Google's Licensing API requires the impersonation subject to hold
        a specific "License Management" admin role — the SA subject
        (nexus.svc@) doesn't reliably have it, so direct API calls
        error with 403 "Unauthorized operation for the given domain."
        GAM uses OAuth (via ~/.gam/oauth2.txt) which is authorized under
        a different admin identity that DOES have licensing perms. This
        is the same auth Tim's manual license cleanups have used all
        along and reliably works (2,222 revokes 2026-09-03).

        Idempotent — GAM's "User does not have a license" response is
        mapped to success so the automated deprovision loop can iterate
        cleanly.

        Retries with exponential backoff (5s, 10s, 20s, 40s, 60s → ~2 min
        total) on the transient "Auto License un-assignment is not
        allowed" error. That fires when a preceding OU move into
        /Archived Accounts hasn't propagated yet on Google's side and
        the paid-tier auto-license policy still thinks the user should
        hold the license.

        For the district's paid Ed Plus revocation:
            product_id = "101031"
            sku_id     = "1010310008"

        Requires GAM bind-mount (docker-compose):
            /home/tim/bin/gam7 → GAM binary directory (ro)
            /home/tim/.gam     → GAM OAuth + gam.cfg (ro)
        Paths match the host layout so GAM's config-embedded paths
        Just Work without patching.
        """
        import asyncio
        import os
        gam_bin = "/home/tim/bin/gam7/gam"
        gam_cfg = "/home/tim/.gam"
        if not os.path.exists(gam_bin):
            # Fall-through hint — dev environments without the mount get
            # a clear error instead of a cryptic FileNotFoundError.
            return GoogleAccountResult(
                email=email, success=False,
                error="GAM not available (missing /home/tim/bin/gam7 bind-mount)",
            )

        env = {**os.environ, "GAMCFGDIR": gam_cfg, "HOME": gam_cfg}
        cmd = [gam_bin, "user", email, "delete", "license", sku_id]

        # Preflight: nuke any stale OAuth lock file left by the OTHER
        # uid (host tim uid 1000 vs container nexus uid 999). GAM's
        # Python filelock hardcodes mode 644 on this file, and neither
        # side can chmod a file owned by the other — so whichever side
        # ran GAM last locks the other side out. Deleting is safe:
        # the lock is a per-run semaphore, not persistent state, and
        # GAM recreates it on its own the moment it needs it.
        # If the delete fails (owner is other uid AND file is being
        # held by a concurrent run), that's rare and GAM will error
        # explicitly with the same "Permission denied" — no worse than
        # the pre-preflight state.
        lock_path = os.path.join(gam_cfg, "oauth2.txt.lock")
        try:
            if os.path.exists(lock_path):
                os.unlink(lock_path)
        except OSError:
            pass

        def _run():
            import subprocess
            # umask 0 so any new files GAM creates (updated oauth2.txt
            # after token refresh) start out mode 666 — writable by
            # both container uid 999 AND host uid 1000.
            old_umask = os.umask(0)
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, env=env, timeout=60,
                )
            finally:
                os.umask(old_umask)
            # Postflight: delete the lock file we just wrote. Prevents
            # the "next caller has different uid → can't rewrite" trap.
            # See preflight comment for details.
            try:
                if os.path.exists(lock_path):
                    os.unlink(lock_path)
            except OSError:
                pass
            return r

        last_output: str = ""
        backoffs = [5, 10, 20, 40, 60]
        for attempt, delay in enumerate(backoffs, start=1):
            try:
                r = await asyncio.to_thread(_run)
            except Exception as e:
                logger.error(f"GAM license revoke crashed for {email}: {e}")
                return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

            out = (r.stdout or "") + (r.stderr or "")
            last_output = out.strip()[:400]

            # Success — "Deleted" in GAM output
            if ", Deleted" in out and "Delete Failed" not in out:
                if attempt > 1:
                    logger.info(
                        f"License revoked after {attempt} attempts via GAM: {email} sku={sku_id}"
                    )
                else:
                    logger.info(f"License revoked via GAM: {email} sku={sku_id}")
                return GoogleAccountResult(email=email, success=True)

            # Idempotent — already gone counts as success
            if "does not have a license" in out.lower():
                logger.info(f"License not held (skip): {email} sku={sku_id}")
                return GoogleAccountResult(email=email, success=True)

            # Only retry the propagation race — other errors fail fast
            if "Auto License un-assignment is not allowed" in out:
                if attempt >= len(backoffs):
                    logger.warning(
                        f"License revoke gave up after {attempt} attempts "
                        f"(auto-license lock persisted): {email} sku={sku_id}"
                    )
                    return GoogleAccountResult(
                        email=email, success=False,
                        error=f"auto-license lock after {attempt} retries",
                    )
                logger.info(
                    f"License revoke attempt {attempt}/{len(backoffs)} "
                    f"blocked by auto-license lock, sleeping {delay}s: {email}"
                )
                await asyncio.sleep(delay)
                continue

            # Any other failure — return with the GAM output for triage
            logger.error(
                f"GAM license revoke failed for {email} sku={sku_id}: {last_output}"
            )
            return GoogleAccountResult(email=email, success=False, error=last_output[:200])

        return GoogleAccountResult(email=email, success=False, error=last_output[:200] or "unknown")

    async def create_account(
        self,
        email: str,
        first_name: str,
        last_name: str,
        org_unit: str,
        temp_password: str,
    ) -> GoogleAccountResult:
        """Create a Google Workspace account."""
        try:
            import asyncio
            service = await self._get_service()

            def _create():
                return service.users().insert(body={
                    "primaryEmail": email,
                    "name": {"givenName": first_name, "familyName": last_name},
                    "password": temp_password,
                    "changePasswordAtNextLogin": True,
                    "orgUnitPath": org_unit,
                }).execute()

            await asyncio.to_thread(_create)
            logger.info(f"Google account created: {email}")
            return GoogleAccountResult(email=email, success=True)

        except Exception as e:
            logger.error(f"Google account creation failed for {email}: {e}")
            return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

    async def set_password(
        self,
        email: str,
        new_password: str,
        force_change: bool = True,
    ) -> GoogleAccountResult:
        """Reset a user's Google Workspace password.

        When ``force_change`` is True (default, matches provisioning) the
        user must pick a new password on next login. Same Directory API
        surface as ``create_account`` — just an update instead of insert.
        """
        try:
            import asyncio
            service = await self._get_service()

            def _set():
                service.users().update(userKey=email, body={
                    "password": new_password,
                    "changePasswordAtNextLogin": force_change,
                }).execute()

            await asyncio.to_thread(_set)
            logger.info(
                f"Google password reset: {email} (force_change={force_change})"
            )
            return GoogleAccountResult(email=email, success=True)

        except Exception as e:
            logger.error(f"Google password reset failed for {email}: {e}")
            return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

    async def suspend_account(self, email: str) -> GoogleAccountResult:
        """Suspend a Google account."""
        try:
            import asyncio
            service = await self._get_service()

            def _suspend():
                service.users().update(userKey=email, body={"suspended": True}).execute()

            await asyncio.to_thread(_suspend)
            logger.info(f"Google account suspended: {email}")
            return GoogleAccountResult(email=email, success=True)

        except Exception as e:
            logger.error(f"Google account suspension failed for {email}: {e}")
            return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

    async def move_user_ou(self, email: str, ou_path: str) -> GoogleAccountResult:
        """Move a Google account to a different OU."""
        try:
            import asyncio
            service = await self._get_service()

            def _move():
                service.users().update(userKey=email, body={"orgUnitPath": ou_path}).execute()

            await asyncio.to_thread(_move)
            logger.info(f"Google account moved: {email} → {ou_path}")
            return GoogleAccountResult(email=email, success=True)

        except Exception as e:
            logger.error(f"Google OU move failed for {email}: {e}")
            return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

    async def suspend_user(self, email: str) -> GoogleAccountResult:
        """Alias for suspend_account — used by deprovision pipeline."""
        return await self.suspend_account(email)

    async def group_exists(self, group_email: str) -> bool:
        """Check if a Google Group exists."""
        try:
            import asyncio
            service = await self._get_service()
            await asyncio.to_thread(
                lambda: service.groups().get(groupKey=group_email).execute()
            )
            return True
        except Exception:
            return False

    async def _get_groups_settings_service(self):
        """Build Groups Settings API service."""
        from app.modules.settings.repository import get_setting_value
        import os
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        admin_email = await get_setting_value(self.db, "google", "admin_email")
        cred_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/run/secrets/google_service_account.json")
        creds = (
            service_account.Credentials
            .from_service_account_file(
                cred_file,
                scopes=["https://www.googleapis.com/auth/apps.groups.settings"],
            )
            .with_subject(admin_email)
        )
        return build("groupssettings", "v1", credentials=creds, cache_discovery=False)

    async def create_group(self, group_email: str) -> dict:
        """Create a Google Group with standard Nexus security settings.

        Group name derived from email prefix.
        Access: owners/managers full, members all except invite,
        org/external disabled, only invited users can join,
        no external members.
        """
        import asyncio

        name = group_email.split("@")[0].replace("-", " ").replace("_", " ").title()
        service = await self._get_service()
        gs = await self._get_groups_settings_service()

        def _create():
            body = {
                "email": group_email,
                "name": name,
                "description": f"Nexus role group — {name}",
            }
            result = service.groups().insert(body=body).execute()
            logger.info(f"Created Google Group: {group_email} (id={result.get('id')})")

            # Apply access settings — Groups Settings API needs a short
            # delay after creation for the group to propagate.
            import time
            time.sleep(2)

            settings = {
                "whoCanPostMessage": "ALL_IN_DOMAIN_CAN_POST",
                "whoCanViewGroup": "ALL_MEMBERS_CAN_VIEW",
                "whoCanViewMembership": "ALL_MEMBERS_CAN_VIEW",
                "whoCanJoin": "INVITED_CAN_JOIN",
                "allowExternalMembers": "false",
                "whoCanModerateMembers": "OWNERS_AND_MANAGERS",
                "whoCanInvite": "ALL_MANAGERS_CAN_INVITE",
                "whoCanModifyMembers": "OWNERS_AND_MANAGERS",
                "whoCanContactOwner": "ALL_MEMBERS_CAN_CONTACT",
                "isArchived": "true",
                "includeInGlobalAddressList": "false",
            }
            gs.groups().update(groupUniqueId=group_email, body=settings).execute()
            logger.info(f"Applied Nexus settings to group: {group_email}")
            return result

        return await asyncio.to_thread(_create)

    async def add_to_group(self, email: str, group_email: str) -> GoogleGroupResult:
        """Add user to a Google Group."""
        try:
            import asyncio
            service = await self._get_service()

            def _add():
                service.members().insert(
                    groupKey=group_email,
                    body={"email": email, "role": "MEMBER"},
                ).execute()

            await asyncio.to_thread(_add)
            logger.info(f"Added {email} to group {group_email}")
            return GoogleGroupResult(group=group_email, added=True)

        except Exception as e:
            if "Member already exists" in str(e):
                return GoogleGroupResult(group=group_email, added=True)
            logger.error(f"Failed to add {email} to {group_email}: {e}")
            return GoogleGroupResult(group=group_email, added=False, error=str(e)[:200])

    async def reactivate_account(self, email: str) -> GoogleAccountResult:
        """Reactivate a suspended Google account."""
        try:
            import asyncio
            service = await self._get_service()

            def _reactivate():
                service.users().update(userKey=email, body={"suspended": False}).execute()

            await asyncio.to_thread(_reactivate)
            logger.info(f"Google account reactivated: {email}")
            return GoogleAccountResult(email=email, success=True)

        except Exception as e:
            logger.error(f"Google account reactivation failed for {email}: {e}")
            return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

    async def rename_account(self, old_email: str, new_email: str) -> GoogleAccountResult:
        """Change the primary email of an account."""
        try:
            import asyncio
            service = await self._get_service()

            def _rename():
                service.users().update(
                    userKey=old_email, body={"primaryEmail": new_email},
                ).execute()

            await asyncio.to_thread(_rename)
            logger.info(f"Google account renamed: {old_email} -> {new_email}")
            return GoogleAccountResult(email=new_email, success=True)

        except Exception as e:
            logger.error(f"Google account rename failed {old_email} -> {new_email}: {e}")
            return GoogleAccountResult(email=old_email, success=False, error=str(e)[:200])

    async def update_names(
        self,
        email: str,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> GoogleAccountResult:
        """
        Update name.givenName / name.familyName on a Google account.

        Only the fields passed are changed. The full name is
        recomputed server-side from given + family so we don't need
        to send fullName explicitly.
        """
        try:
            import asyncio
            service = await self._get_service()

            def _update():
                name_body: dict = {}
                if first_name is not None:
                    name_body["givenName"] = first_name
                if last_name is not None:
                    name_body["familyName"] = last_name
                if not name_body:
                    return
                service.users().update(
                    userKey=email, body={"name": name_body},
                ).execute()

            await asyncio.to_thread(_update)
            logger.info(
                f"Google names updated for {email}: "
                f"given={first_name!r} family={last_name!r}"
            )
            return GoogleAccountResult(email=email, success=True)

        except Exception as e:
            logger.error(f"Google update_names failed for {email}: {e}")
            return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

    async def set_student_id(self, email: str, student_id: str) -> GoogleAccountResult:
        """
        Write student ID to externalIds and organizations[].employeeId.

        Retries with exponential backoff when Google returns the
        eventual-consistency errors that follow a fresh ``create_account``
        call:
          - 404 ``notFound`` (userKey not yet indexed)
          - 412 ``conditionNotMet`` "User creation is not complete."
          - 503 ``backendError``

        Backoff schedule: 1s, 2s, 4s, 8s (~15 seconds total in the
        worst case). Non-transient errors (400, 403, 409) return
        immediately without retry.
        """
        import asyncio
        from googleapiclient.errors import HttpError

        service = await self._get_service()

        # Reasons Google returns during the ~few-second propagation
        # window after account creation. If we see one of these, the
        # account almost certainly exists — we just need to wait.
        _TRANSIENT_REASONS = {"notFound", "conditionNotMet", "backendError"}
        _TRANSIENT_STATUSES = {404, 412, 500, 502, 503, 504}

        def _set_id():
            user = service.users().get(userKey=email, projection="full").execute()
            prev_ids = [
                x["value"] for x in user.get("externalIds", [])
                if x.get("type") == "organization"
            ]
            prev_sid = prev_ids[0] if prev_ids else None

            ext_ids = [x for x in user.get("externalIds", []) if x.get("type") != "organization"]
            ext_ids.append({"type": "organization", "value": student_id})

            orgs = user.get("organizations", [])
            if orgs:
                orgs[0]["employeeId"] = student_id
            else:
                orgs = [{"employeeId": student_id, "primary": True}]

            service.users().update(
                userKey=email,
                body={"externalIds": ext_ids, "organizations": orgs},
            ).execute()
            return prev_sid

        backoffs = [1.0, 2.0, 4.0, 8.0]
        last_err: Exception | None = None
        for attempt, delay in enumerate([0.0] + backoffs):
            if delay:
                await asyncio.sleep(delay)
            try:
                prev_sid = await asyncio.to_thread(_set_id)
                if attempt > 0:
                    logger.info(
                        f"Set student ID {student_id} on {email} "
                        f"(succeeded on retry {attempt})"
                    )
                else:
                    logger.info(f"Set student ID {student_id} on {email}")
                return GoogleAccountResult(
                    email=email, success=True,
                    before_state={"student_id": prev_sid},
                )
            except HttpError as e:
                status = getattr(getattr(e, "resp", None), "status", None)
                reason = ""
                try:
                    import json as _json
                    body = _json.loads(e.content.decode("utf-8"))
                    errors = body.get("error", {}).get("errors", [])
                    if errors:
                        reason = errors[0].get("reason", "")
                except Exception:
                    pass
                is_transient = (
                    status in _TRANSIENT_STATUSES or reason in _TRANSIENT_REASONS
                )
                if not is_transient or attempt >= len(backoffs):
                    logger.error(
                        f"Failed to set student ID for {email} "
                        f"(status={status} reason={reason!r}): {e}"
                    )
                    return GoogleAccountResult(email=email, success=False, error=str(e)[:200])
                logger.info(
                    f"set_student_id transient for {email} "
                    f"(status={status} reason={reason!r}) — retrying in {backoffs[attempt]}s"
                )
                last_err = e
            except Exception as e:
                logger.error(f"Failed to set student ID for {email}: {e}")
                return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

        # Exhausted retries.
        return GoogleAccountResult(
            email=email, success=False,
            error=f"transient error persisted after {len(backoffs)} retries: {str(last_err)[:150]}",
        )

    async def clear_student_id(self, email: str) -> GoogleAccountResult:
        """Remove the organization externalId and employeeId from an account."""
        try:
            import asyncio
            service = await self._get_service()

            def _clear():
                user = service.users().get(userKey=email, projection="full").execute()
                ext_ids = [x for x in user.get("externalIds", []) if x.get("type") != "organization"]
                orgs = user.get("organizations", [])
                if orgs:
                    orgs[0].pop("employeeId", None)
                service.users().update(
                    userKey=email, body={"externalIds": ext_ids, "organizations": orgs},
                ).execute()

            await asyncio.to_thread(_clear)
            logger.info(f"Cleared student ID from {email}")
            return GoogleAccountResult(email=email, success=True)

        except Exception as e:
            logger.error(f"Failed to clear student ID for {email}: {e}")
            return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

    async def move_to_ou(self, email: str, org_unit_path: str) -> GoogleAccountResult:
        """Move a user to a different organizational unit."""
        try:
            import asyncio
            service = await self._get_service()

            def _move():
                service.users().update(
                    userKey=email, body={"orgUnitPath": org_unit_path},
                ).execute()

            await asyncio.to_thread(_move)
            logger.info(f"Google account moved: {email} → {org_unit_path}")
            return GoogleAccountResult(email=email, success=True)

        except Exception as e:
            logger.error(f"Google move_to_ou failed for {email}: {e}")
            return GoogleAccountResult(email=email, success=False, error=str(e)[:200])

    async def get_user(self, email: str) -> dict | None:
        """
        Look up a Google account by email. Returns full user object dict
        (name, externalIds, suspended, orgUnitPath) or None if not found.
        """
        try:
            import asyncio
            service = await self._get_service()

            def _get():
                return service.users().get(userKey=email, projection="full").execute()

            raw = await asyncio.to_thread(_get)
            # Normalize to a consistent dict
            ext_ids = raw.get("externalIds", [])
            orgs = raw.get("organizations", [])
            student_id = None
            for ext in ext_ids:
                if isinstance(ext, dict) and ext.get("type") == "organization":
                    student_id = ext.get("value")
                    break
            if not student_id and orgs:
                student_id = orgs[0].get("employeeId")

            return {
                "email": raw.get("primaryEmail", ""),
                "first_name": raw.get("name", {}).get("givenName", ""),
                "last_name": raw.get("name", {}).get("familyName", ""),
                "full_name": raw.get("name", {}).get("fullName", ""),
                "suspended": raw.get("suspended", False),
                "org_unit_path": raw.get("orgUnitPath", ""),
                "student_id": student_id,
                "externalIds": ext_ids,
                "organizations": orgs,
            }

        except Exception as e:
            if "404" in str(e) or "Resource Not Found" in str(e):
                return None
            logger.error(f"Google get_user failed for {email}: {e}")
            raise

    async def check_email_exists(self, email: str) -> bool:
        """Return True if the email exists in Google Workspace."""
        result = await self.get_user(email)
        return result is not None

    async def get_active_accounts(
        self, domain: str = "", include_suspended: bool = False,
    ) -> list[dict]:
        """
        Return user accounts in the domain.

        By default only non-suspended accounts (backwards-compatible with
        callers that assume 'active'). Pass ``include_suspended=True`` to
        pull the full set — each row has a ``suspended`` bool so the
        caller can distinguish. Necessary for the SIS roster import so
        suspended-but-reactivatable students don't get mis-tagged as
        ``google_status='missing'``.
        """
        try:
            import asyncio
            service = await self._get_service()

            def _list_active():
                users = []
                page_token = None
                while True:
                    kwargs = dict(
                        domain=domain,
                        maxResults=500,
                        pageToken=page_token,
                        projection="full",
                    )
                    if not include_suspended:
                        kwargs["query"] = "isSuspended=false"
                    result = service.users().list(**kwargs).execute()
                    for u in result.get("users", []):
                        aliases = u.get("aliases", []) + u.get("nonEditableAliases", [])
                        users.append({
                            "email": u["primaryEmail"],
                            "name": u.get("name", {}).get("fullName", ""),
                            "given_name": u.get("name", {}).get("givenName", ""),
                            "family_name": u.get("name", {}).get("familyName", ""),
                            "suspended": u.get("suspended", False),
                            "org_unit_path": u.get("orgUnitPath", ""),
                            "external_ids": u.get("externalIds", []),
                            "organizations": u.get("organizations", []),
                            "aliases": aliases,
                        })
                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break
                return users

            return await asyncio.to_thread(_list_active)

        except Exception as e:
            logger.error(f"Failed to list active accounts for {domain}: {e}")
            raise

    async def list_users(self, query: str = "", max_results: int = 500) -> list[dict]:
        """List users from Google Workspace Admin SDK."""
        try:
            import asyncio
            service = await self._get_service()

            def _list():
                users = []
                request = service.users().list(
                    domain=None,  # All domains under this admin
                    customer="my_customer",
                    maxResults=min(max_results, 500),
                    query=query or None,
                    orderBy="email",
                    projection="full",
                )
                while request:
                    result = request.execute()
                    for u in result.get("users", []):
                        org_unit = u.get("orgUnitPath", "")
                        # Parse building code from OU path
                        building = _parse_building_from_ou(org_unit)

                        # Include every alias email so the matching logic
                        # can use them for HR email matching (catches
                        # maiden names, legacy short addresses, etc.).
                        aliases = (u.get("aliases") or []) + (u.get("nonEditableAliases") or [])

                        users.append({
                            "email": u.get("primaryEmail", ""),
                            "first_name": u.get("name", {}).get("givenName", ""),
                            "last_name": u.get("name", {}).get("familyName", ""),
                            "full_name": u.get("name", {}).get("fullName", ""),
                            "title": (u.get("organizations") or [{}])[0].get("title", ""),
                            "department": (u.get("organizations") or [{}])[0].get("department", ""),
                            "org_unit": org_unit,
                            "building": building,
                            "phone": (u.get("phones") or [{}])[0].get("value", ""),
                            "status": "suspended" if u.get("suspended") else "active",
                            "is_admin": u.get("isAdmin", False),
                            "last_login": u.get("lastLoginTime", ""),
                            "google_id": u.get("id", ""),
                            "aliases": aliases,
                            # 2FA / 2-Step Verification status. Both are
                            # top-level bool fields in the users.get/list
                            # response when projection=full. `.get()` with
                            # a None default keeps "field not returned"
                            # distinguishable from "explicitly false".
                            "is_enrolled_in_2sv": u.get("isEnrolledIn2Sv"),
                            "is_enforced_in_2sv": u.get("isEnforcedIn2Sv"),
                        })
                    request = service.users().list_next(request, result)
                    if len(users) >= max_results:
                        break
                return users

            return await asyncio.to_thread(_list)

        except Exception as e:
            logger.error(f"Google list_users failed: {e}")
            raise


    async def list_groups(self, domain: str | None = None) -> list[dict]:
        """List all Google Groups in the domain."""
        try:
            import asyncio
            service = await self._get_service()

            def _list():
                groups = []
                page_token = None
                while True:
                    params = {"customer": "my_customer", "maxResults": 200, "pageToken": page_token}
                    if domain:
                        params["domain"] = domain
                    result = service.groups().list(**{k: v for k, v in params.items() if v}).execute()
                    for g in result.get("groups", []):
                        groups.append({
                            "email": g.get("email", ""),
                            "name": g.get("name", ""),
                            "description": g.get("description", ""),
                            "member_count": g.get("directMembersCount", ""),
                        })
                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break
                return groups

            return await asyncio.to_thread(_list)

        except Exception as e:
            logger.error(f"Google list_groups failed: {e}")
            raise

    async def update_user_photo(self, email: str, jpeg_bytes: bytes) -> bool:
        """Upload a photo to a Google Workspace user account."""
        try:
            import asyncio
            import base64
            service = await self._get_service()

            def _upload():
                photo_data = base64.urlsafe_b64encode(jpeg_bytes).decode("ascii")
                service.users().photos().update(
                    userKey=email,
                    body={"photoData": photo_data, "mimeType": "JPEG"},
                ).execute()

            await asyncio.to_thread(_upload)
            logger.info(f"Updated Google photo for {email}")
            return True
        except Exception as e:
            logger.error(f"Google photo upload failed for {email}: {e}")
            return False

    async def get_user_groups(self, email: str) -> list[dict]:
        """List all groups a user belongs to."""
        try:
            import asyncio
            service = await self._get_service()

            def _list():
                groups = []
                page_token = None
                while True:
                    result = service.groups().list(
                        userKey=email, maxResults=200, pageToken=page_token,
                    ).execute()
                    for g in result.get("groups", []):
                        groups.append({
                            "email": g.get("email", ""),
                            "name": g.get("name", ""),
                        })
                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break
                return groups

            return await asyncio.to_thread(_list)

        except Exception as e:
            logger.error(f"Google get_user_groups failed for {email}: {e}")
            return []

    async def list_ous(self) -> list[dict]:
        """List all organizational units."""
        try:
            import asyncio
            service = await self._get_service()

            def _list():
                result = service.orgunits().list(customerId="my_customer", type="all").execute()
                ous = []
                for ou in result.get("organizationUnits", []):
                    ous.append({
                        "path": ou.get("orgUnitPath", ""),
                        "name": ou.get("name", ""),
                        "description": ou.get("description", ""),
                        "parent_path": ou.get("parentOrgUnitPath", ""),
                    })
                return sorted(ous, key=lambda x: x["path"])

            return await asyncio.to_thread(_list)

        except Exception as e:
            logger.error(f"Google list_ous failed: {e}")
            raise


def _parse_building_from_ou(org_unit: str, ou_building_map: dict | None = None) -> str | None:
    """
    Parse building code from Google OU path using a configurable mapping.

    ou_building_map: {name_or_keyword: building_code} loaded from Settings.
    If not provided, falls back to matching the last OU segment as a short code.
    """
    if not org_unit:
        return None

    segments = org_unit.strip("/").split("/")
    ou_lower = org_unit.lower()

    if ou_building_map:
        # Check last segment first (most specific)
        if len(segments) >= 2:
            last = segments[-1].lower().strip()
            for pattern, code in ou_building_map.items():
                if last == pattern.lower():
                    return code

        # Broader keyword match on full path
        for pattern, code in ou_building_map.items():
            if pattern.lower() in ou_lower:
                return code

    # Fallback: last segment if short enough to be a building code
    if len(segments) >= 2:
        last = segments[-1].strip()
        if len(last) <= 5 and last.isalpha():
            return last.upper()

    return None
