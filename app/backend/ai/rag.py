"""
RAG Pipeline — SQLite FTS5 full-text search.
No embedding model, no extra memory.  Same public interface as the old ChromaDB version.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import db as _portfolio


class RAGPipeline:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag")
        self._sources: dict[str, dict] = self._load_sources()
        conn = _portfolio._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]
        print(f"RAG ready (FTS5) — {count} chunks indexed")

    # ── Public API ─────────────────────────────────────────────────────────────

    def query(self, text: str, n_results: int = 4) -> str:
        conn = _portfolio._get_conn()
        rows = conn.execute("""
            SELECT rc.content, rc.source_name
            FROM rag_chunks_fts fts
            JOIN rag_chunks rc ON rc.rowid = fts.rowid
            WHERE rag_chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (_fts_query(text), n_results)).fetchall()
        if not rows:
            return ""
        return "\n\n".join(f"[Source: {r['source_name']}]\n{r['content']}" for r in rows)

    async def query_async(self, text: str, n_results: int = 4) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.query, text, n_results)

    def add_text(self, text: str, name: str = "manual", source_id: str | None = None) -> str:
        if source_id is None:
            source_id = str(uuid.uuid4())
            self._sources[source_id] = {
                "id": source_id,
                "name": name,
                "type": "text",
                "status": "ready",
                "created": datetime.now(UTC).isoformat(),
                "chunk_count": 0,
            }

        chunks = _chunk_text(text)
        if not chunks:
            return source_id

        conn = _portfolio._get_conn()
        with _portfolio._write_lock:
            for i, chunk in enumerate(chunks):
                chunk_id = f"{source_id}_{i}"
                conn.execute(
                    "INSERT OR REPLACE INTO rag_chunks(id, source_id, source_name, content) VALUES(?,?,?,?)",
                    (chunk_id, source_id, name, chunk),
                )
            conn.commit()

        if source_id in self._sources:
            self._sources[source_id]["chunk_count"] = (
                self._sources[source_id].get("chunk_count", 0) + len(chunks)
            )
        self._save_sources()
        return source_id

    async def add_text_async(self, text: str, name: str = "manual", source_id: str | None = None) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.add_text, text, name, source_id)

    def has_source(self, source_id: str) -> bool:
        return source_id in self._sources

    def create_source(self, source_id: str, name: str, source_type: str) -> str:
        self._sources[source_id] = {
            "id": source_id,
            "name": name,
            "type": source_type,
            "status": "pending",
            "created": datetime.now(UTC).isoformat(),
            "chunk_count": 0,
        }
        self._save_sources()
        return source_id

    def list_sources(self) -> list[dict]:
        return list(self._sources.values())

    def delete_source(self, source_id: str) -> None:
        with _portfolio._write_lock:
            conn = _portfolio._get_conn()
            conn.execute("DELETE FROM rag_chunks WHERE source_id=?", (source_id,))
            conn.commit()
        self._sources.pop(source_id, None)
        self._save_sources()

    def update_source_status(self, source_id: str, status: str) -> None:
        if source_id in self._sources:
            self._sources[source_id]["status"] = status
            self._save_sources()

    def get_source_status(self, source_id: str) -> dict:
        return self._sources.get(source_id, {"error": "not found"})

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    # ── Internals ──────────────────────────────────────────────────────────────

    def _load_sources(self) -> dict[str, dict]:
        try:
            return _portfolio.get_rag_sources()
        except Exception as e:
            print(f"[RAG] WARNING: failed to load sources ({e})")
        return {}

    def _save_sources(self) -> None:
        try:
            _portfolio.save_all_rag_sources(self._sources)
        except Exception as e:
            print(f"[RAG] WARNING: failed to save sources ({e})")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _chunk_text(text: str, size: int = 512, overlap: int = 64) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunk = " ".join(words[i: i + size])
        if len(chunk.strip()) > 20:
            chunks.append(chunk)
        i += size - overlap
    return chunks


def _fts_query(text: str) -> str:
    """Convert free text to an FTS5 query — quote each token to avoid syntax errors."""
    tokens = [t.strip('",\'') for t in text.split() if t.strip('",\'')]
    return " OR ".join(f'"{t}"' for t in tokens[:16]) if tokens else '""'
