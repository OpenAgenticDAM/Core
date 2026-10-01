"""Rights-aware search.

Product principle "rights before relevance": the ACL filter is part of the WHERE clause, so
pgvector ranks only rows the caller may already see. An asset without any matching ACL row is
invisible (fail closed). Principals are namespaced by source - "group:marketing" in Bynder is
not the same group as "group:marketing" in S3 - and are passed as "<source_id>:<principal>".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np
from pgvector import Vector


@dataclass(frozen=True)
class Principal:
    """Who is asking: tenant, audit actor, and the source-namespaced principals they hold."""

    tenant_id: uuid.UUID
    actor: str
    principals: frozenset[str]


SEARCH_SQL = """
SELECT a.id, a.file_name, a.title, a.mime_type, a.source_id,
       1 - (e.embedding <=> %(qvec)s) AS score,
       a.file_size_bytes, a.technical_metadata, a.created_at
FROM embeddings e
JOIN assets a ON a.id = e.asset_id AND a.tenant_id = e.tenant_id
WHERE e.tenant_id = %(tenant)s
  AND e.model = %(model)s
  AND a.deleted_at IS NULL
  AND EXISTS (
        SELECT 1 FROM asset_acl acl
        WHERE acl.asset_id = a.id
          AND acl.tenant_id = a.tenant_id
          AND acl.source_id = a.source_id
          AND acl.permission = 'read'
          AND (acl.source_id::text || ':' || acl.principal) = ANY(%(principals)s)
  )
ORDER BY e.embedding <=> %(qvec)s
LIMIT %(limit)s
"""


def search_assets_sql(who: Principal, query_vec: list[float], model: str, limit: int) -> tuple[str, dict[str, Any]]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    return SEARCH_SQL, {
        "qvec": Vector(np.asarray(query_vec, dtype=np.float32)),
        "tenant": who.tenant_id,
        "model": model,
        "principals": sorted(who.principals),
        "limit": limit,
    }


def run_search(conn: Any, who: Principal, query_vec: list[float], model: str, limit: int) -> list[tuple[Any, ...]]:
    """Execute the rights-filtered search on an open psycopg connection."""
    return conn.execute(*search_assets_sql(who, query_vec, model, limit)).fetchall()
