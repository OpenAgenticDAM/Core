"""Integration tests against the real local stack (Postgres+pgvector, SeaweedFS).

Run `scripts/infra-up.sh` and `uv run alembic upgrade head` first. Tests skip when the
stack is not reachable, so `pytest` stays usable without Docker.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from openagenticdam.config import Settings
from openagenticdam.db import connect
from openagenticdam.search import Principal, search_assets_sql


def _settings() -> Settings:
    return Settings()  # reads .env


@pytest.fixture()
def conn():
    try:
        c = connect(_settings())
    except psycopg.OperationalError as exc:  # stack not running
        pytest.skip(f"postgres not reachable: {exc}")
    c.autocommit = False
    yield c
    c.rollback()  # every test runs in its own transaction - no residue
    c.close()


def _vec(seed: float) -> list[float]:
    dim = _settings().embed_dim
    return [seed] + [0.0] * (dim - 1)


def _insert_asset(conn, tenant: uuid.UUID, source: uuid.UUID, name: str, acl: list[str]) -> uuid.UUID:
    aid = uuid.uuid4()
    conn.execute(
        "INSERT INTO assets (id, tenant_id, source_id, external_id, hash_sha256, file_name, mime_type,"
        " file_size_bytes, title) VALUES (%s,%s,%s,%s,%s,%s,'image/jpeg',1,%s)",
        (aid, tenant, source, name, "0" * 64, name, name),
    )
    for p in acl:
        conn.execute(
            "INSERT INTO asset_acl (tenant_id, asset_id, source_id, principal, permission) VALUES (%s,%s,%s,%s,'read')",
            (tenant, aid, source, p),
        )
    conn.execute(
        "INSERT INTO embeddings (tenant_id, asset_id, model, embedding) VALUES (%s,%s,'test',%s)",
        (tenant, aid, str(_vec(1.0))),
    )
    return aid


@pytest.fixture()
def world(conn):
    tenant = uuid.uuid4()
    other_tenant = uuid.uuid4()
    src = uuid.uuid4()
    other_src = uuid.uuid4()
    conn.execute("INSERT INTO sources (id, tenant_id, kind, config) VALUES (%s,%s,'s3','{}')", (src, tenant))
    conn.execute(
        "INSERT INTO sources (id, tenant_id, kind, config) VALUES (%s,%s,'s3','{}')", (other_src, other_tenant)
    )
    ids = {
        "public": _insert_asset(conn, tenant, src, "public.jpg", ["group:everyone"]),
        "marketing": _insert_asset(conn, tenant, src, "marketing.jpg", ["group:marketing"]),
        "no_acl": _insert_asset(conn, tenant, src, "no_acl.jpg", []),
        "foreign": _insert_asset(conn, other_tenant, other_src, "foreign.jpg", ["group:everyone"]),
    }
    ids["_other_src"] = other_src
    return tenant, src, ids


def _search(conn, tenant, src, principals):
    p = Principal(tenant_id=tenant, actor="t", principals=frozenset(principals))
    rows = conn.execute(*search_assets_sql(p, _vec(1.0), "test", limit=10)).fetchall()
    return {r[0] for r in rows}


def test_search_returns_asset_visible_to_principal(conn, world):
    tenant, src, ids = world
    assert ids["public"] in _search(conn, tenant, src, [f"{src}:group:everyone"])


def test_search_hides_asset_without_matching_acl(conn, world):
    tenant, src, ids = world
    assert ids["marketing"] not in _search(conn, tenant, src, [f"{src}:group:everyone"])


def test_search_hides_asset_with_no_acl_at_all_fail_closed(conn, world):
    tenant, src, ids = world
    assert ids["no_acl"] not in _search(conn, tenant, src, [f"{src}:group:everyone", f"{src}:group:marketing"])


def test_search_never_crosses_tenants(conn, world):
    tenant, src, ids = world
    assert ids["foreign"] not in _search(conn, tenant, src, [f"{src}:group:everyone"])


def test_tenant_filter_holds_even_with_misconfigured_foreign_principal(conn, world):
    # Identity mapping bug: the caller somehow holds a principal of another tenant's source.
    # The tenant boundary must still hold on its own.
    tenant, src, ids = world
    leaked = f"{ids['_other_src']}:group:everyone"
    assert ids["foreign"] not in _search(conn, tenant, src, [f"{src}:group:everyone", leaked])


def test_principal_of_other_source_does_not_grant_access(conn, world):
    tenant, src, ids = world
    other = uuid.uuid4()
    assert _search(conn, tenant, src, [f"{other}:group:everyone", f"{other}:group:marketing"]) == set()


def test_empty_principals_see_nothing(conn, world):
    tenant, src, _ = world
    assert _search(conn, tenant, src, []) == set()
