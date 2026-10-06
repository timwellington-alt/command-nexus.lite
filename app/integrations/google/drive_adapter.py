"""
Google Drive API adapter — document management for the Operations module.

Browse, search, upload, and organize files in a shared Drive folder.
Credentials loaded via service account with domain-wide delegation.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# Standard folder structure for auto-creation
FOLDER_STRUCTURE = {
    "Incidents": {},
    "Modules": {
        "Staff": {},
        "Roster": {},
        "Access": {},
        "Network": {},
        "Chromebook": {},
        "Transportation": {},
        "Phones": {},
    },
    "Integrations": {
        "Google Admin SDK": {},
        "Paxton Net2": {},
        "Active Directory": {},
        "LibreNMS": {},
        "Grandstream UCM": {},
        "Clever SIS": {},
        "Transfinder": {},
        "SMB-HR": {},
    },
    "Architecture": {},
    "Runbooks": {},
    "Vendor Contracts": {},
    "Emergency Contacts": {},
}


class GoogleDriveAdapter:
    """
    Google Drive API v3 adapter.

    Uses service account with domain-wide delegation.
    All file operations scoped to a configurable root folder.
    """

    def __init__(self, db):
        self.db = db

    async def _build_service(self):
        """Build authenticated Drive API service.

        Uses the service account's own credentials (not impersonation)
        since the service account is added directly as an editor on the
        Shared Drive. This avoids needing the Drive scope in domain-wide
        delegation.
        """
        cred_file = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE",
            "/run/secrets/google_service_account.json",
        )
        if not os.path.exists(cred_file):
            raise RuntimeError("Google service account file not found")

        def _build():
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                cred_file,
                scopes=["https://www.googleapis.com/auth/drive"],
            )
            return build("drive", "v3", credentials=creds, cache_discovery=False)

        return await asyncio.to_thread(_build)

    async def _get_root_folder_id(self) -> str:
        """Get the configured root folder ID from settings."""
        from app.modules.settings.repository import get_setting_value
        folder_id = await get_setting_value(self.db, "drive", "root_folder_id")
        if not folder_id:
            raise RuntimeError("Drive root folder ID not configured — set it in Settings")
        return folder_id.strip()

    async def list_files(self, folder_id: str | None = None, page_size: int = 100) -> list[dict]:
        """List files and folders in a folder."""
        if not folder_id:
            folder_id = await self._get_root_folder_id()
        service = await self._build_service()

        def _list():
            results = service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                pageSize=page_size,
                fields="files(id,name,mimeType,modifiedTime,size,webViewLink,iconLink)",
                orderBy="folder,name",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            return results.get("files", [])

        files = await asyncio.to_thread(_list)
        return [
            {
                "id": f["id"],
                "name": f["name"],
                "mime_type": f.get("mimeType", ""),
                "is_folder": f.get("mimeType") == "application/vnd.google-apps.folder",
                "modified": f.get("modifiedTime", ""),
                "size": int(f.get("size", 0)) if f.get("size") else None,
                "web_link": f.get("webViewLink", ""),
                "icon": f.get("iconLink", ""),
            }
            for f in files
        ]

    async def search_files(self, query: str, folder_id: str | None = None) -> list[dict]:
        """Search for files by name or content within the doc root."""
        root_id = folder_id or await self._get_root_folder_id()
        service = await self._build_service()

        def _search():
            # Get the Shared Drive ID if applicable, then scope search
            try:
                folder_meta = service.files().get(
                    fileId=root_id, fields="driveId",
                    supportsAllDrives=True,
                ).execute()
                drive_id = folder_meta.get("driveId")
            except Exception:
                drive_id = None

            q = f"fullText contains '{query}' and trashed=false"
            params = {
                "q": q,
                "pageSize": 50,
                "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink,parents)",
                "orderBy": "modifiedTime desc",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            }
            if drive_id:
                params["driveId"] = drive_id
                params["corpora"] = "drive"
            results = service.files().list(**params).execute()
            return results.get("files", [])

        files = await asyncio.to_thread(_search)
        return [
            {
                "id": f["id"],
                "name": f["name"],
                "mime_type": f.get("mimeType", ""),
                "is_folder": f.get("mimeType") == "application/vnd.google-apps.folder",
                "modified": f.get("modifiedTime", ""),
                "size": int(f.get("size", 0)) if f.get("size") else None,
                "web_link": f.get("webViewLink", ""),
            }
            for f in files
        ]

    async def create_folder(self, name: str, parent_id: str) -> dict:
        """Create a folder."""
        service = await self._build_service()

        def _create():
            metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            folder = service.files().create(
                body=metadata, fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()
            return folder

        result = await asyncio.to_thread(_create)
        logger.info(f"Drive: created folder '{name}' in {parent_id}")
        return {"id": result["id"], "name": result["name"], "web_link": result.get("webViewLink", "")}

    async def upload_file(self, name: str, content: bytes, mime_type: str, parent_id: str) -> dict:
        """Upload a file to a folder."""
        service = await self._build_service()

        def _upload():
            from googleapiclient.http import MediaInMemoryUpload

            metadata = {"name": name, "parents": [parent_id]}
            media = MediaInMemoryUpload(content, mimetype=mime_type)
            f = service.files().create(
                body=metadata, media_body=media,
                fields="id,name,webViewLink,size",
                supportsAllDrives=True,
            ).execute()
            return f

        result = await asyncio.to_thread(_upload)
        logger.info(f"Drive: uploaded '{name}' ({len(content)} bytes) to {parent_id}")
        return {
            "id": result["id"],
            "name": result["name"],
            "web_link": result.get("webViewLink", ""),
            "size": int(result.get("size", 0)),
        }

    async def get_folder_tree(self, folder_id: str | None = None) -> list[dict]:
        """Get the folder tree (folders only, recursive one level)."""
        if not folder_id:
            folder_id = await self._get_root_folder_id()
        service = await self._build_service()

        def _tree():
            # Get top-level folders
            results = service.files().list(
                q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                pageSize=100,
                fields="files(id,name)",
                orderBy="name",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            top_folders = results.get("files", [])

            tree = []
            for f in top_folders:
                # Get subfolders
                sub_results = service.files().list(
                    q=f"'{f['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                    pageSize=100,
                    fields="files(id,name)",
                    orderBy="name",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
                subfolders = [{"id": s["id"], "name": s["name"]} for s in sub_results.get("files", [])]
                tree.append({
                    "id": f["id"],
                    "name": f["name"],
                    "children": subfolders,
                })
            return tree

        return await asyncio.to_thread(_tree)

    async def ensure_folder_structure(self, root_id: str | None = None) -> dict:
        """Create the standard folder structure if it doesn't exist."""
        if not root_id:
            root_id = await self._get_root_folder_id()

        existing = await self.list_files(root_id)
        existing_names = {f["name"]: f["id"] for f in existing if f["is_folder"]}

        created = 0

        async def _ensure(parent_id: str, structure: dict, parent_existing: dict):
            nonlocal created
            for name, children in structure.items():
                if name in parent_existing:
                    folder_id = parent_existing[name]
                else:
                    result = await self.create_folder(name, parent_id)
                    folder_id = result["id"]
                    created += 1

                if children:
                    child_files = await self.list_files(folder_id)
                    child_existing = {f["name"]: f["id"] for f in child_files if f["is_folder"]}
                    await _ensure(folder_id, children, child_existing)

        await _ensure(root_id, FOLDER_STRUCTURE, existing_names)

        logger.info(f"Drive: folder structure ensured — {created} folders created")
        return {"status": "ok", "created": created}

    async def test_connection(self) -> dict:
        """Test Drive API access."""
        try:
            root_id = await self._get_root_folder_id()
            files = await self.list_files(root_id)
            return {
                "status": "ok",
                "message": f"Connected — {len(files)} items in root folder",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)[:200]}
