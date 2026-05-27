"""Google Drive cloud storage provider."""

from __future__ import annotations

import io
import os
from typing import Optional

from madrag.storage.cloud.base import CloudFile, CloudStorageProvider


class GoogleDriveStorageProvider(CloudStorageProvider):
    """Google Drive backed cloud storage via service account.

    Required env vars:
      CLOUD_STORAGE_GOOGLE_SERVICE_ACCOUNT_JSON  (path to SA key file)
      CLOUD_STORAGE_GOOGLE_DRIVE_FOLDER_ID       (target folder ID)
    """

    _SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self, service_account_json: str, folder_id: str):
        self._sa_json = service_account_json
        self._folder_id = folder_id
        self._service = None

    def _get_service(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            raise RuntimeError(
                "google-api-python-client and google-auth are required for Google Drive storage. "
                "Install with: pip install madrag-hku[cloud-storage]"
            )
        creds = service_account.Credentials.from_service_account_file(
            self._sa_json, scopes=self._SCOPES
        )
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    async def upload(self, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        import asyncio
        from googleapiclient.http import MediaIoBaseUpload

        def _do_upload():
            svc = self._get_service()
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type)
            file_meta = {"name": filename, "parents": [self._folder_id]}
            result = (
                svc.files()
                .create(body=file_meta, media_body=media, fields="id")
                .execute()
            )
            return result["id"]

        file_id = await asyncio.get_event_loop().run_in_executor(None, _do_upload)
        return file_id

    async def download(self, cloud_path: str) -> bytes:
        import asyncio
        from googleapiclient.http import MediaIoBaseDownload

        # cloud_path is a file ID for Google Drive
        def _do_download():
            svc = self._get_service()
            request = svc.files().get_media(fileId=cloud_path)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue()

        return await asyncio.get_event_loop().run_in_executor(None, _do_download)

    async def delete(self, cloud_path: str) -> None:
        import asyncio

        def _do_delete():
            svc = self._get_service()
            svc.files().delete(fileId=cloud_path).execute()

        await asyncio.get_event_loop().run_in_executor(None, _do_delete)

    async def list_files(self, prefix: str = "") -> list[CloudFile]:
        import asyncio

        def _do_list():
            svc = self._get_service()
            query = f"'{self._folder_id}' in parents and trashed=false"
            if prefix:
                query += f" and name contains '{prefix}'"
            results = (
                svc.files()
                .list(q=query, fields="files(id,name,size,mimeType,modifiedTime)")
                .execute()
            )
            files = []
            for item in results.get("files", []):
                files.append(CloudFile(
                    name=item["name"],
                    cloud_path=item["id"],
                    size=int(item.get("size", 0)),
                    content_type=item.get("mimeType", "application/octet-stream"),
                ))
            return files

        return await asyncio.get_event_loop().run_in_executor(None, _do_list)

    async def exists(self, cloud_path: str) -> bool:
        import asyncio

        def _do_check():
            try:
                svc = self._get_service()
                svc.files().get(fileId=cloud_path, fields="id").execute()
                return True
            except Exception:
                return False

        return await asyncio.get_event_loop().run_in_executor(None, _do_check)

    @classmethod
    def from_env(cls) -> "GoogleDriveStorageProvider":
        sa_json = os.environ.get("CLOUD_STORAGE_GOOGLE_SERVICE_ACCOUNT_JSON", "")
        folder_id = os.environ.get("CLOUD_STORAGE_GOOGLE_DRIVE_FOLDER_ID", "")
        if not sa_json or not folder_id:
            raise ValueError(
                "CLOUD_STORAGE_GOOGLE_SERVICE_ACCOUNT_JSON and "
                "CLOUD_STORAGE_GOOGLE_DRIVE_FOLDER_ID must be set"
            )
        return cls(service_account_json=sa_json, folder_id=folder_id)
