"""Scale behaviour of rights-filtered vector search (PRD v0.1 exit criterion: search at scale).

Seeds 50,000 assets with random vectors directly in SQL, of which the caller may read only 2 %.
Opt-in because seeding takes a while:  uv run pytest -m scale
"""

from __future__ import annotations

import statistics
import time
import uuid

import psycopg
import pytest

from openagenticdam.config import Settings
from openagenticdam.db import connect
from openagenticdam.search import Principal, run_search

pytestmark = pytest.mark.scale

N_ASSETS = 50_000
VISIBLE_EVERY = 50  # 1 in 50 = 2 % visible to the caller
MODEL = "scale-test"


@pytest.fixture(scope="module")
def big_world():
    settings = Settings()
    try:
        conn = connect(settings)
    except psycopg.OperationalError as exc:
        pytest.skip(f"postgres not reachable: {exc}")
    conn.autocommit = True  # ANALYZE/VACUUM must commit; tests run outside explicit transactions
    tenant, src = uuid.uuid4(), uuid.uuid4()
    t0 = time.perf_counter()
    with conn.transaction():
        conn.execute("INSERT INTO sources (id, tenant_id, kind, config) VALUES (%s,%s,'s3','{}')", (src, tenant))
        conn.execute(
            """
            INSERT INTO assets (id, tenant_id, source_id, external_id, hash_sha256, file_name, mime_type,
                                file_size_bytes)
            SELECT gen_random_uuid(), %(t)s, %(s)s, 'k' || i, repeat('0', 64), 'f' || i || '.jpg', 'image/jpeg', 1
            FROM generate_series(1, %(n)s) i
            """,
            {"t": tenant, "s": src, "n": N_ASSETS},
        )
        conn.execute(
            """
            INSERT INTO asset_acl (tenant_id, asset_id, source_id, principal, permission)
            SELECT tenant_id, id, source_id,
                   CASE WHEN substr(external_id, 2)::int %% %(every)s = 0 THEN 'group:me' ELSE 'group:other' END,
                   'read'
            FROM assets WHERE tenant_id = %(t)s
            """,
            {"t": tenant, "every": VISIBLE_EVERY},
        )
        conn.execute(
            """
            INSERT INTO embeddings (tenant_id, asset_id, model, embedding)
            SELECT a.tenant_id, a.id, %(m)s,
                   (SELECT array_agg(random())::vector FROM generate_series(1, 768) WHERE a.id IS NOT NULL)
            FROM assets a WHERE a.tenant_id = %(t)s
            """,
            {"t": tenant, "m": MODEL},
        )
    for table in ("assets", "asset_acl", "embeddings"):
        conn.execute(f"ANALYZE {table}")
    print(f"\nseeded {N_ASSETS} assets in {time.perf_counter() - t0:.1f}s")
    yield conn, tenant, src
    with conn.transaction():
        conn.execute("DELETE FROM assets WHERE tenant_id = %s", (tenant,))
        conn.execute("DELETE FROM sources WHERE tenant_id = %s", (tenant,))
    conn.execute("VACUUM (ANALYZE) embeddings, asset_acl, assets")
    conn.close()


def _query_vec(conn) -> list[float]:
    row = conn.execute("SELECT array_agg(random()) FROM generate_series(1, 768)").fetchone()
    assert row is not None
    return list(row[0])


def test_low_visibility_caller_still_gets_full_page(big_world):
    """With only 2 % visible, an index scan that filters afterwards would return < limit hits."""
    conn, tenant, src = big_world
    who = Principal(tenant, "t", frozenset({f"{src}:group:me"}))
    for _ in range(5):
        hits = run_search(conn, who, _query_vec(conn), MODEL, limit=10)
        assert len(hits) == 10


def test_results_match_exact_search(big_world):
    """Approximate (HNSW) results must agree with an exact scan for the visible subset."""
    conn, tenant, src = big_world
    who = Principal(tenant, "t", frozenset({f"{src}:group:me"}))
    q = _query_vec(conn)
    approx = [h[0] for h in run_search(conn, who, q, MODEL, limit=10)]
    with conn.transaction():
        conn.execute("SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        exact = [h[0] for h in run_search(conn, who, q, MODEL, limit=10)]
    assert len(set(approx) & set(exact)) >= 8  # recall@10 >= 0.8


def test_search_latency_p95(big_world):
    conn, tenant, src = big_world
    who = Principal(tenant, "t", frozenset({f"{src}:group:me"}))
    times = []
    for _ in range(30):
        q = _query_vec(conn)
        t0 = time.perf_counter()
        run_search(conn, who, q, MODEL, limit=10)
        times.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(times, n=20)[18]
    print(f"\nnarrow caller (2 % of {N_ASSETS}): p50={statistics.median(times):.1f} ms p95={p95:.1f} ms")
    assert p95 < 800  # PRD target (at 1M assets; this is a 50k smoke check)


def test_wide_visibility_caller_gets_full_page_and_is_fast(big_world):
    """98 % visible: the planner prefers the HNSW index; results must still fill the page."""
    conn, tenant, src = big_world
    who = Principal(tenant, "t", frozenset({f"{src}:group:other"}))
    times = []
    for _ in range(20):
        q = _query_vec(conn)
        t0 = time.perf_counter()
        hits = run_search(conn, who, q, MODEL, limit=10)
        times.append((time.perf_counter() - t0) * 1000)
        assert len(hits) == 10
    p95 = statistics.quantiles(times, n=20)[18]
    print(f"\nwide caller (98 % visible): p50={statistics.median(times):.1f} ms p95={p95:.1f} ms")
    assert p95 < 800
