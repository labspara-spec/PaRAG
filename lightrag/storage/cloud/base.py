"""Abstract base class for cloud storage providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class CloudFile:
    name: str
    cloud_path: str
    size: int = 0
    last_modified: Optional[datetime] = None
    content_type: str = "application/octet-stream"
    extra: dict = field(default_factory=dict)


class CloudStorageProvider(ABC):
    """Async interface for cloud blob/file storage."""

    @abstractmethod
    async def upload(self, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Upload bytes and return the cloud path/key."""

    @abstractmethod
    async def download(self, cloud_path: str) -> bytes:
        """Download and return the full file bytes."""

    @abstractmethod
    async def delete(self, cloud_path: str) -> None:
        """Delete a file by its cloud path."""

    @abstractmethod
    async def list_files(self, prefix: str = "") -> list[CloudFile]:
        """List files under the given prefix."""

    @abstractmethod
    async def exists(self, cloud_path: str) -> bool:
        """Return True if the file exists."""

    async def get_image_url(self, cloud_path: str, _expiry_seconds: int = 3600) -> str:
        """Return a URL to access the image at cloud_path.

        Default implementation returns the cloud_path unchanged.
        Providers that support signed URLs should override this.
        """
        return cloud_path
