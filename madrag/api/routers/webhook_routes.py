"""
Webhook endpoints for cloud storage change events.

Each provider delivers events to a dedicated endpoint:
  POST /webhooks/s3         — AWS S3 (via SNS or EventBridge)
  POST /webhooks/azure      — Azure Blob (via Event Grid)
  POST /webhooks/sharepoint — SharePoint (Microsoft Graph subscription)
  POST /webhooks/onedrive   — OneDrive (Microsoft Graph subscription)
  POST /webhooks/google     — Google Drive (push notification channel)

On CREATE/UPDATE events: file is downloaded from cloud, indexed, temp copy deleted.
On DELETE events: document is removed from the RAG index.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from madrag.storage.cloud.base import CloudStorageProvider

logger = logging.getLogger("madrag.webhooks")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_webhook_secret() -> str:
    return os.environ.get("CLOUD_STORAGE_WEBHOOK_SECRET", "")


async def _handle_upsert(
    pool,
    doc_manager,
    cloud_provider: CloudStorageProvider,
    filename: str,
    workspace: str = "",
):
    """Download file from cloud, index it, delete temp copy."""
    from madrag.api.routers.document_routes import pipeline_index_file
    import uuid

    if not doc_manager.is_supported_file(filename):
        logger.info("Webhook: skipping unsupported file type '%s'", filename)
        return

    # Use a unique temp name to avoid collisions with concurrent events.
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    temp_path = doc_manager.input_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
    try:
        data = await cloud_provider.download(filename)
        temp_path.write_bytes(data)
        await pipeline_index_file(pool, workspace, temp_path)
        logger.info("Webhook: indexed '%s' (workspace=%s)", filename, workspace)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


async def _handle_delete(
    pool,
    cloud_provider: CloudStorageProvider,
    filename: str,
    workspace: str = "",
):
    """Delete a document from the RAG index by filename."""
    async with pool.acquire(workspace) as rag:
        match = await rag.doc_status.get_doc_by_file_basename(filename)
        if not match:
            logger.info("Webhook delete: no record for '%s', ignoring", filename)
            return
        doc_id, _ = match
        await rag.adelete_by_doc_id(doc_id)
        logger.info("Webhook: deleted '%s' (doc_id=%s, workspace=%s)", filename, doc_id, workspace)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_webhook_routes(
    pool,
    doc_manager,
    cloud_provider: Optional[CloudStorageProvider],
    api_key: Optional[str] = None,
) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    if cloud_provider is None:
        # No cloud provider configured — register stub endpoints that return 501
        for path in ("/s3", "/azure", "/sharepoint", "/onedrive", "/google"):
            @router.post(path, include_in_schema=False)
            async def _no_provider():
                raise HTTPException(
                    status_code=501,
                    detail="Cloud storage provider not configured (CLOUD_STORAGE_PROVIDER is unset).",
                )
        return router

    # ------------------------------------------------------------------
    # AWS S3 — SNS HTTP/S subscription
    # ------------------------------------------------------------------
    @router.post("/s3", summary="AWS S3 event notification (SNS)")
    async def s3_webhook(request: Request):
        """Handle S3 event notifications delivered via AWS SNS.

        Supports SNS SubscriptionConfirmation (returns SubscribeURL confirmation)
        and Notification messages with S3 event records.
        """
        raw = await request.body()
        try:
            envelope = json.loads(raw)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        msg_type = envelope.get("Type", "")
        workspace = request.headers.get("X-Workspace", "")

        # Confirm SNS subscription
        if msg_type == "SubscriptionConfirmation":
            subscribe_url = envelope.get("SubscribeURL", "")
            if subscribe_url:
                import httpx
                async with httpx.AsyncClient() as client:
                    await client.get(subscribe_url)
            return {"status": "confirmed"}

        if msg_type != "Notification":
            return {"status": "ignored", "type": msg_type}

        try:
            message = json.loads(envelope.get("Message", "{}"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid SNS Message JSON")

        for record in message.get("Records", []):
            event_name = record.get("eventName", "")
            s3_obj = record.get("s3", {})
            key = s3_obj.get("object", {}).get("key", "")
            if not key:
                continue

            # URL-decode key (S3 encodes spaces as +)
            from urllib.parse import unquote_plus
            key = unquote_plus(key)

            # Strip configured prefix
            from madrag.storage.cloud.s3 import S3StorageProvider
            if isinstance(cloud_provider, S3StorageProvider):
                if key.startswith(cloud_provider.prefix):
                    key = key[len(cloud_provider.prefix):]

            filename = Path(key).name
            if event_name.startswith("ObjectRemoved"):
                await _handle_delete(pool, cloud_provider, filename, workspace)
            elif event_name.startswith("ObjectCreated"):
                await _handle_upsert(pool, doc_manager, cloud_provider, filename, workspace)

        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Azure Blob Storage — Event Grid
    # ------------------------------------------------------------------
    @router.post("/azure", summary="Azure Blob Storage event (Event Grid)")
    async def azure_webhook(
        request: Request,
        aeg_event_type: Optional[str] = Header(None, alias="aeg-event-type"),
    ):
        """Handle Azure Event Grid notifications for Blob Storage.

        Responds to EventGrid validation handshake and BlobCreated / BlobDeleted events.
        Validate with CLOUD_STORAGE_WEBHOOK_SECRET in the event subscription filter.
        """
        raw = await request.body()
        workspace = request.headers.get("X-Workspace", "")

        # Validation handshake
        if aeg_event_type == "SubscriptionValidation":
            events = json.loads(raw)
            code = events[0]["data"]["validationCode"]
            return {"validationResponse": code}

        try:
            events = json.loads(raw)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        for event in (events if isinstance(events, list) else [events]):
            event_type = event.get("eventType", "")
            data = event.get("data", {})
            url = data.get("url", "")
            if not url:
                continue

            filename = Path(url.split("?")[0]).name  # strip SAS token query params

            if event_type == "Microsoft.Storage.BlobDeleted":
                await _handle_delete(pool, cloud_provider, filename, workspace)
            elif event_type in ("Microsoft.Storage.BlobCreated", "Microsoft.Storage.BlobTierChanged"):
                await _handle_upsert(pool, doc_manager, cloud_provider, filename, workspace)

        return {"status": "ok"}

    # ------------------------------------------------------------------
    # SharePoint — Microsoft Graph subscription
    # ------------------------------------------------------------------
    @router.post("/sharepoint", summary="SharePoint change notification (Graph)")
    async def sharepoint_webhook(
        request: Request,
        validationToken: Optional[str] = None,
    ):
        """Handle Microsoft Graph change notifications for SharePoint.

        On first-time subscription, Graph sends a GET/POST with validationToken.
        Subsequent events are JSON payloads with driveItem change notifications.
        """
        if validationToken:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(validationToken)

        return await _handle_graph_notification(request, pool, doc_manager, cloud_provider)

    # ------------------------------------------------------------------
    # OneDrive — Microsoft Graph subscription
    # ------------------------------------------------------------------
    @router.post("/onedrive", summary="OneDrive change notification (Graph)")
    async def onedrive_webhook(
        request: Request,
        validationToken: Optional[str] = None,
    ):
        """Handle Microsoft Graph change notifications for OneDrive."""
        if validationToken:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(validationToken)

        return await _handle_graph_notification(request, pool, doc_manager, cloud_provider)

    # ------------------------------------------------------------------
    # Google Drive — push notification channel
    # ------------------------------------------------------------------
    @router.post("/google", summary="Google Drive push notification")
    async def google_webhook(
        request: Request,
        x_goog_channel_token: Optional[str] = Header(None, alias="X-Goog-Channel-Token"),
        x_goog_resource_state: Optional[str] = Header(None, alias="X-Goog-Resource-State"),
        x_goog_resource_id: Optional[str] = Header(None, alias="X-Goog-Resource-Id"),
    ):
        """Handle Google Drive push notifications.

        Google delivers a POST with X-Goog-Resource-State header.
        We validate X-Goog-Channel-Token against CLOUD_STORAGE_WEBHOOK_SECRET.
        """
        secret = _get_webhook_secret()
        if secret and x_goog_channel_token != secret:
            raise HTTPException(status_code=401, detail="Invalid channel token")

        workspace = request.headers.get("X-Workspace", "")

        if x_goog_resource_state in ("sync",):
            return {"status": "sync_ack"}

        if x_goog_resource_state == "trash":
            if x_goog_resource_id:
                await _handle_delete(pool, cloud_provider, x_goog_resource_id, workspace)
        elif x_goog_resource_state in ("update", "add"):
            if x_goog_resource_id:
                # Resource ID is the Drive file ID; download using it directly
                try:
                    data = await cloud_provider.download(x_goog_resource_id)
                    # We don't know the filename from headers alone; attempt via Drive API metadata
                    from madrag.storage.cloud.google_drive import GoogleDriveStorageProvider
                    if isinstance(cloud_provider, GoogleDriveStorageProvider):
                        import asyncio

                        def _get_name():
                            svc = cloud_provider._get_service()
                            meta = svc.files().get(fileId=x_goog_resource_id, fields="name").execute()
                            return meta["name"]

                        filename = await asyncio.get_event_loop().run_in_executor(None, _get_name)
                    else:
                        filename = x_goog_resource_id

                    if doc_manager.is_supported_file(filename):
                        temp_path = doc_manager.input_dir / filename
                        try:
                            temp_path.write_bytes(data)
                            from madrag.api.routers.document_routes import pipeline_index_file
                            await pipeline_index_file(pool, workspace, temp_path)
                        finally:
                            temp_path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("Google webhook: failed to process %s: %s", x_goog_resource_id, exc)

        return {"status": "ok"}

    return router


# ---------------------------------------------------------------------------
# Shared Graph notification handler
# ---------------------------------------------------------------------------

async def _handle_graph_notification(request: Request, pool, doc_manager, cloud_provider):
    """Common handler for SharePoint and OneDrive Graph notifications."""
    raw = await request.body()
    workspace = request.headers.get("X-Workspace", "")

    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    for notification in payload.get("value", []):
        client_state = notification.get("clientState", "")
        secret = _get_webhook_secret()
        if secret and client_state != secret:
            logger.warning("Graph webhook: clientState mismatch, ignoring notification")
            continue

        change_type = notification.get("changeType", "")
        resource = notification.get("resource", "")

        # The resource string looks like "drives/{driveId}/items/{itemId}"
        # We need the item name — fetch it via Graph API if provider is available
        if not resource:
            continue

        try:
            from madrag.storage.cloud.graph import GraphStorageProvider
            if isinstance(cloud_provider, GraphStorageProvider):
                import httpx
                headers = await cloud_provider._headers()
                item_url = f"https://graph.microsoft.com/v1.0/{resource}"
                async with httpx.AsyncClient() as client:
                    resp = await client.get(item_url, headers={"Authorization": headers["Authorization"]})
                    if resp.status_code != 200:
                        continue
                    item = resp.json()

                filename = item.get("name", "")
                if not filename:
                    continue

                if change_type == "deleted":
                    await _handle_delete(pool, cloud_provider, filename, workspace)
                elif change_type in ("created", "updated"):
                    await _handle_upsert(pool, doc_manager, cloud_provider, filename, workspace)
        except Exception as exc:
            logger.warning("Graph notification handler error: %s", exc)

    return {"status": "ok"}
