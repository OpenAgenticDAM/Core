"""Exact duplicates (same SHA-256 of the original bytes, per tenant) are removed automatically.

Upload: a file whose bytes the caller can already see is not stored again - the existing asset
is returned instead. `lock_content` takes a transaction-scoped advisory lock on (tenant, hash),
so two simultaneous uploads of one file cannot both slip past the check.

An identical file the caller may NOT see is no reason to refuse theirs (and must not be
revealed): their copy is stored normally.

Existing duplicates among chat uploads: `remove_duplicate_uploads` keeps the oldest copy and
deletes the rest. S3-synced assets are left alone - the source system decides about its files.
"""

from __future__ import annotations

import uuid

import psycopg

from openagenticdam.ingest import delete_asset_rows


def lock_content(conn: psycopg.Connection, tenant: uuid.UUID, sha256: str) -> None:
    """Serialise writers of the same content in this tenant until the transaction ends."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"oad:content:{tenant}:{sha256}",))


def find_visible_duplicate(
    conn: psycopg.Connection, tenant: uuid.UUID, sha256: str, principals: frozenset[str]
) -> tuple[uuid.UUID, str] | None:
    """(asset_id, file_name) of the oldest live asset with these bytes that the caller may read."""
    row = conn.execute(
        """
        SELECT a.id, a.file_name FROM assets a
        WHERE a.tenant_id = %(tenant)s AND a.hash_sha256 = %(sha)s AND a.deleted_at IS NULL
          AND EXISTS (
            SELECT 1 FROM asset_acl acl
            WHERE acl.asset_id = a.id AND acl.tenant_id = a.tenant_id AND acl.source_id = a.source_id
              AND acl.permission = 'read'
              AND (acl.source_id::text || ':' || acl.principal) = ANY(%(principals)s))
        ORDER BY a.created_at, a.id
        LIMIT 1
        """,
        {"tenant": tenant, "sha": sha256, "principals": sorted(principals)},
    ).fetchone()
    return (row[0], row[1]) if row else None


def remove_duplicate_uploads(conn: psycopg.Connection, tenant: uuid.UUID) -> tuple[list[uuid.UUID], list[str]]:
    """Delete all but the oldest copy of each identical chat upload.

    Returns (deleted asset ids, object keys to remove after commit). Caller commits.
    """
    ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT id FROM (
              SELECT a.id, row_number() OVER (PARTITION BY a.hash_sha256 ORDER BY a.created_at, a.id) AS n
              FROM assets a JOIN sources s ON s.id = a.source_id AND s.tenant_id = a.tenant_id
              WHERE a.tenant_id = %s AND a.deleted_at IS NULL AND s.kind = 'upload'
            ) ranked WHERE n > 1
            """,
            (tenant,),
        ).fetchall()
    ]
    return ids, delete_asset_rows(conn, tenant, ids) if ids else []
