"""Crawl M&H website, extract text, upload to Vector Store — Section 4.3."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import List, Set
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.knowledge_base.vector_store import VectorStoreService

logger = logging.getLogger(__name__)


def _same_domain(url: str, base_host: str) -> bool:
    try:
        return urlparse(url).netloc == base_host
    except Exception:
        return False


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


async def crawl_site_to_vector_store(
    *,
    vector_store_id: str,
    max_pages: int = 40,
    path_prefix: str = "/",
) -> List[str]:
    """Fetch pages under base URL, chunk to temp files, upload. Returns OpenAI file ids."""
    s = get_settings()
    base = s.website_crawl_base_url.rstrip("/")
    parsed = urlparse(base)
    base_host = parsed.netloc
    visited: Set[str] = set()
    queue: List[str] = [base + path_prefix]
    file_ids: List[str] = []
    vss = VectorStoreService()

    headers = {"User-Agent": s.website_crawl_user_agent}

    async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                r = await client.get(url)
                r.raise_for_status()
            except Exception as e:
                logger.warning("crawl skip %s: %s", url, e)
                continue
            ctype = r.headers.get("content-type", "")
            if "text/html" not in ctype:
                continue
            text = _visible_text(r.text)
            if len(text) < 80:
                continue
            chunk = f"Source: {url}\n\n{text[:48000]}"
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
                tmp.write(chunk)
                tmp_path = tmp.name
            try:
                fid = await vss.upload_file_to_vector_store(
                    vector_store_id=vector_store_id,
                    file_path=tmp_path,
                    filename=f"crawl-{len(file_ids)}.txt",
                )
                file_ids.append(fid)
            finally:
                os.unlink(tmp_path)

            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                next_url = urljoin(url, href)
                if not _same_domain(next_url, base_host):
                    continue
                if next_url.split("#")[0] not in visited and next_url not in queue:
                    queue.append(next_url.split("#")[0])

            await asyncio.sleep(0.3)

    return file_ids
