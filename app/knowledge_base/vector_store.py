"""Upload and manage files in OpenAI Vector Stores."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)


class VectorStoreService:
    def __init__(self) -> None:
        s = get_settings()
        self._client = AsyncOpenAI(api_key=s.openai_api_key) if s.openai_api_key else None

    async def upload_file_to_vector_store(
        self,
        *,
        vector_store_id: str,
        file_path: str,
        filename: Optional[str] = None,
    ) -> str:
        if not self._client:
            raise RuntimeError("OPENAI_API_KEY not set")
        path = Path(file_path)
        fname = filename or path.name
        with open(path, "rb") as f:
            uploaded = await self._client.files.create(file=(fname, f), purpose="assistants")
        await self._client.vector_stores.files.create(
            vector_store_id=vector_store_id,
            file_id=uploaded.id,
        )
        return uploaded.id

    async def delete_file_from_store(self, vector_store_id: str, file_id: str) -> None:
        if not self._client:
            raise RuntimeError("OPENAI_API_KEY not set")
        await self._client.vector_stores.files.delete(
            vector_store_id=vector_store_id,
            file_id=file_id,
        )

    async def list_files_in_store(self, vector_store_id: str) -> list[dict]:
        """List all files in a vector store, paginating through all results."""
        if not self._client:
            raise RuntimeError("OPENAI_API_KEY not set")
        files = []
        after: Optional[str] = None
        while True:
            kwargs = {"vector_store_id": vector_store_id, "limit": 100}
            if after:
                kwargs["after"] = after
            result = await self._client.vector_stores.files.list(**kwargs)
            for f in result.data:
                files.append({
                    "file_id": f.id,
                    "status": f.status,
                    "created_at": f.created_at,
                })
            if not result.has_more:
                break
            after = result.data[-1].id
        return files

    async def create_vector_store(self, name: str) -> str:
        if not self._client:
            raise RuntimeError("OPENAI_API_KEY not set")
        vs = await self._client.vector_stores.create(name=name)
        return vs.id
