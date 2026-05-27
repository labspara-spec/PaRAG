"""Microsoft Graph API storage provider — SharePoint and OneDrive."""

from __future__ import annotations

import os
from typing import Optional

from lightrag.storage.cloud.base import CloudFile, CloudStorageProvider


class GraphStorageProvider(CloudStorageProvider):
    """SharePoint / OneDrive via Microsoft Graph API (app-only, client credentials).

    Required env vars:
      CLOUD_STORAGE_GRAPH_TENANT_ID
      CLOUD_STORAGE_GRAPH_CLIENT_ID
      CLOUD_STORAGE_GRAPH_CLIENT_SECRET

    For SharePoint:  CLOUD_STORAGE_SHAREPOINT_SITE_ID
    For OneDrive:    CLOUD_STORAGE_ONEDRIVE_USER_ID  (or leave blank for /me drive)
    Optional:        CLOUD_STORAGE_GRAPH_DRIVE_ID (skip auto-resolve)
    """

    _GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    _TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        drive_id: Optional[str] = None,
        site_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._drive_id = drive_id
        self._site_id = site_id
        self._user_id = user_id
        self._token: Optional[str] = None

    async def _get_token(self) -> str:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx is required for Graph API. It is bundled with the api extra.")

        url = self._TOKEN_URL.format(tenant_id=self._tenant_id)
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://graph.microsoft.com/.default",
            })
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def _get_drive_id(self, headers: dict) -> str:
        if self._drive_id:
            return self._drive_id

        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx is required for Graph API.")

        async with httpx.AsyncClient() as client:
            if self._site_id:
                url = f"{self._GRAPH_BASE}/sites/{self._site_id}/drive"
            elif self._user_id:
                url = f"{self._GRAPH_BASE}/users/{self._user_id}/drive"
            else:
                url = f"{self._GRAPH_BASE}/me/drive"
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            self._drive_id = resp.json()["id"]
            return self._drive_id

    async def _headers(self) -> dict:
        token = await self._get_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def upload(self, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx is required for Graph API.")

        headers = await self._headers()
        drive_id = await self._get_drive_id(headers)
        upload_headers = {"Authorization": headers["Authorization"], "Content-Type": content_type}

        # Use simple upload for files <= 4MB; large upload session for bigger files
        if len(data) <= 4 * 1024 * 1024:
            url = f"{self._GRAPH_BASE}/drives/{drive_id}/root:/{filename}:/content"
            async with httpx.AsyncClient() as client:
                resp = await client.put(url, headers=upload_headers, content=data)
                resp.raise_for_status()
        else:
            async with httpx.AsyncClient() as client:
                session_url = f"{self._GRAPH_BASE}/drives/{drive_id}/root:/{filename}:/createUploadSession"
                session_resp = await client.post(session_url, headers=headers, json={})
                session_resp.raise_for_status()
                upload_url = session_resp.json()["uploadUrl"]

                chunk_size = 5 * 1024 * 1024
                total = len(data)
                for start in range(0, total, chunk_size):
                    end = min(start + chunk_size, total) - 1
                    chunk = data[start:end + 1]
                    chunk_headers = {
                        "Content-Range": f"bytes {start}-{end}/{total}",
                        "Content-Length": str(len(chunk)),
                    }
                    resp = await client.put(upload_url, headers=chunk_headers, content=chunk)
                    resp.raise_for_status()

        return filename

    async def download(self, cloud_path: str) -> bytes:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx is required for Graph API.")

        headers = await self._headers()
        drive_id = await self._get_drive_id(headers)
        url = f"{self._GRAPH_BASE}/drives/{drive_id}/root:/{cloud_path}:/content"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers={"Authorization": headers["Authorization"]})
            resp.raise_for_status()
            return resp.content

    async def delete(self, cloud_path: str) -> None:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx is required for Graph API.")

        headers = await self._headers()
        drive_id = await self._get_drive_id(headers)
        url = f"{self._GRAPH_BASE}/drives/{drive_id}/root:/{cloud_path}"
        async with httpx.AsyncClient() as client:
            resp = await client.delete(url, headers={"Authorization": headers["Authorization"]})
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()

    async def list_files(self, prefix: str = "") -> list[CloudFile]:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx is required for Graph API.")

        headers = await self._headers()
        drive_id = await self._get_drive_id(headers)
        path = prefix.rstrip("/") if prefix else "root"
        url = (
            f"{self._GRAPH_BASE}/drives/{drive_id}/root:/{path}:/children"
            if prefix
            else f"{self._GRAPH_BASE}/drives/{drive_id}/root/children"
        )

        files: list[CloudFile] = []
        async with httpx.AsyncClient() as client:
            while url:
                resp = await client.get(url, headers={"Authorization": headers["Authorization"]})
                resp.raise_for_status()
                body = resp.json()
                for item in body.get("value", []):
                    if "file" in item:
                        name = item["name"]
                        files.append(CloudFile(
                            name=name,
                            cloud_path=name,
                            size=item.get("size", 0),
                            content_type=item.get("file", {}).get("mimeType", "application/octet-stream"),
                        ))
                url = body.get("@odata.nextLink")
        return files

    async def exists(self, cloud_path: str) -> bool:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx is required for Graph API.")

        try:
            headers = await self._headers()
            drive_id = await self._get_drive_id(headers)
            url = f"{self._GRAPH_BASE}/drives/{drive_id}/root:/{cloud_path}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers={"Authorization": headers["Authorization"]})
                return resp.status_code == 200
        except Exception:
            return False

    @classmethod
    def from_env(cls) -> "GraphStorageProvider":
        tenant_id = os.environ.get("CLOUD_STORAGE_GRAPH_TENANT_ID", "")
        client_id = os.environ.get("CLOUD_STORAGE_GRAPH_CLIENT_ID", "")
        client_secret = os.environ.get("CLOUD_STORAGE_GRAPH_CLIENT_SECRET", "")
        if not all([tenant_id, client_id, client_secret]):
            raise ValueError(
                "CLOUD_STORAGE_GRAPH_TENANT_ID, CLOUD_STORAGE_GRAPH_CLIENT_ID, "
                "and CLOUD_STORAGE_GRAPH_CLIENT_SECRET must all be set"
            )
        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            drive_id=os.environ.get("CLOUD_STORAGE_GRAPH_DRIVE_ID"),
            site_id=os.environ.get("CLOUD_STORAGE_SHAREPOINT_SITE_ID"),
            user_id=os.environ.get("CLOUD_STORAGE_ONEDRIVE_USER_ID"),
        )
