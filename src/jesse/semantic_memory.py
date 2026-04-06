"""Semantic memory layer — LanceDB + Ollama embeddings for learned trading patterns."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lancedb
from lancedb.embeddings.ollama import OllamaEmbeddings

logger = logging.getLogger(__name__)

TABLE_NAME = "trading_patterns"
EMBEDDING_MODEL = "qwen3-embedding:0.6b"
EMBEDDING_DIM = 1024
DEFAULT_SIMILARITY_K = 5
INDEX_THRESHOLD = 50
VECTOR_COL = "vector"


class SemanticMemory:
    """LanceDB-backed semantic memory with Ollama embeddings.

    Patterns are embedded via qwen3-embedding:0.6b (1024 dims) for
    cosine-similarity semantic search. Metadata is stored alongside
    for structured filtering (symbol, regime, confidence, type).
    """

    def __init__(self, persist_dir: str | None = None):
        if persist_dir is None:
            persist_dir = str(Path.home() / ".mt5-mcp" / "lancedb")
        Path(persist_dir).mkdir(parents=True, exist_ok=True)

        self._db = lancedb.connect(persist_dir)
        self._embedder = OllamaEmbeddings(name=EMBEDDING_MODEL)
        self._table = self._get_or_create_table()
        self._index_ready = self._check_index()

    def _get_or_create_table(self):
        try:
            return self._db.open_table(TABLE_NAME)
        except Exception:
            import pyarrow as pa

            schema = pa.schema(
                [
                    pa.field("id", pa.string()),
                    pa.field("text", pa.string()),
                    pa.field("metadata_json", pa.string()),
                    pa.field("created_at", pa.string()),
                    pa.field("valid", pa.bool_()),
                    pa.field(VECTOR_COL, pa.list_(pa.float32(), EMBEDDING_DIM)),
                ]
            )
            return self._db.create_table(TABLE_NAME, schema=schema)

    def _check_index(self) -> bool:
        try:
            indices = self._table.list_indices()
            return any(VECTOR_COL in str(idx) for idx in indices)
        except Exception:
            return False

    def _make_doc_id(self, pattern_id: str) -> str:
        return f"pattern_{hashlib.md5(pattern_id.encode()).hexdigest()[:12]}"

    def _row_to_dict(self, row: dict) -> dict:
        meta = {}
        raw = row.get("metadata_json", "{}")
        if isinstance(raw, str):
            try:
                meta = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        elif isinstance(raw, dict):
            meta = raw
        return {
            "id": row.get("id", ""),
            "text": row.get("text", ""),
            "metadata": meta,
            "created_at": row.get("created_at", ""),
            "valid": row.get("valid", True),
            "distance": row.get("_distance"),
        }

    def _embed(self, text: str) -> list[float]:
        return self._embedder.generate_embeddings([text])[0]

    def _maybe_create_index(self):
        if self._index_ready:
            return
        count = self._table.count_rows()
        if count < INDEX_THRESHOLD:
            return
        try:
            self._table.create_index(
                metric="cosine",
                num_partitions=1,
                num_sub_vectors=1,
                vector_column_name=VECTOR_COL,
                replace=True,
            )
            self._index_ready = True
            logger.info("Vector index created (%d patterns)", count)
        except Exception as exc:
            logger.warning("Vector index creation failed: %s", exc)

    def add_pattern(
        self,
        pattern_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        doc_id = self._make_doc_id(pattern_id)
        now = datetime.now(timezone.utc).isoformat()

        existing = self._table.search().where(f"id = '{doc_id}'").limit(1).to_list()
        vector = self._embed(text)

        if existing:
            self._table.update(
                where=f"id = '{doc_id}'",
                values={
                    "text": text,
                    "metadata_json": json.dumps(metadata or {}),
                    "created_at": now,
                    "valid": True,
                    VECTOR_COL: vector,
                },
            )
            logger.debug("Updated pattern: %s", doc_id)
        else:
            self._table.add(
                [
                    {
                        "id": doc_id,
                        "text": text,
                        "metadata_json": json.dumps(metadata or {}),
                        "created_at": now,
                        "valid": True,
                        VECTOR_COL: vector,
                    }
                ]
            )
            logger.debug("Added pattern: %s", doc_id)

        self._maybe_create_index()
        return doc_id

    def search(
        self,
        query: str,
        k: int = DEFAULT_SIMILARITY_K,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []

        try:
            query_vec = self._embed(query)
            search_obj = self._table.search(query_vec, vector_column_name=VECTOR_COL)

            if filters:
                conditions = []
                for fk, fv in filters.items():
                    if isinstance(fv, str):
                        conditions.append(f'metadata_json LIKE \'%"{fk}": "{fv}"%\'')
                    elif isinstance(fv, bool):
                        conditions.append(
                            f"metadata_json LIKE '%\"{fk}\": {json.dumps(fv)}%'"
                        )
                if conditions:
                    where_clause = " AND ".join(conditions)
                    search_obj = search_obj.where(where_clause)

            results = search_obj.limit(k).to_list()
        except Exception as exc:
            logger.warning("Vector search failed, falling back to scan: %s", exc)
            try:
                rows = self._table.search().to_list()
            except Exception:
                return []
            results = rows[: k * 3]

        if not results:
            return []

        items = []
        for row in results:
            item = self._row_to_dict(row)
            if item["valid"]:
                items.append(item)

        return items[:k]

    def get_active_rules(
        self,
        symbol: str | None = None,
        regime: str | None = None,
        min_confidence: float = 0.3,
    ) -> list[dict[str, Any]]:
        try:
            rows = self._table.search().to_list()
        except Exception as exc:
            logger.warning("Failed to fetch active rules: %s", exc)
            return []

        items = []
        for row in rows:
            item = self._row_to_dict(row)
            if not item["valid"]:
                continue

            meta = item["metadata"]
            confidence = float(meta.get("confidence", 0.0))
            if confidence < min_confidence:
                continue

            if symbol and meta.get("symbol") != symbol:
                continue
            if regime and meta.get("regime") != regime:
                continue

            items.append(item)

        return sorted(
            items, key=lambda x: x["metadata"].get("confidence", 0), reverse=True
        )

    def invalidate(self, pattern_id: str):
        doc_id = self._make_doc_id(pattern_id)
        try:
            self._table.update(
                where=f"id = '{doc_id}'",
                values={"valid": False},
            )
        except Exception as exc:
            logger.warning("Failed to invalidate pattern %s: %s", doc_id, exc)

    def delete_pattern(self, pattern_id: str):
        doc_id = self._make_doc_id(pattern_id)
        try:
            self._table.delete(f"id = '{doc_id}'")
        except Exception as exc:
            logger.warning("Failed to delete pattern %s: %s", doc_id, exc)

    def count(self) -> int:
        try:
            return self._table.count_rows()
        except Exception:
            return 0

    def get_all(self) -> list[dict[str, Any]]:
        try:
            rows = self._table.search().to_list()
            return [self._row_to_dict(row) for row in rows]
        except Exception:
            return []

    def update_metadata(self, doc_id: str, metadata: dict[str, Any]):
        try:
            self._table.update(
                where=f"id = '{doc_id}'",
                values={"metadata_json": json.dumps(metadata)},
            )
        except Exception as exc:
            logger.warning("Failed to update metadata for %s: %s", doc_id, exc)

    def delete_by_id(self, doc_id: str):
        try:
            self._table.delete(f"id = '{doc_id}'")
        except Exception as exc:
            logger.warning("Failed to delete %s: %s", doc_id, exc)

    def clear(self):
        try:
            all_rows = self._table.search().to_list()
            count = len(all_rows)
            if count > 0:
                ids = [row["id"] for row in all_rows]
                id_list = ", ".join(f"'{i}'" for i in ids)
                self._table.delete(f"id IN ({id_list})")
            logger.info("Cleared %d patterns", count)
        except Exception as exc:
            logger.warning("Failed to clear patterns: %s", exc)

    def close(self):
        try:
            self._db = None
            self._table = None
            self._embedder = None
        except Exception:
            pass
