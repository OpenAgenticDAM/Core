"""Database access. Plain psycopg 3; schema lives in Alembic migrations."""

from __future__ import annotations

import psycopg
from pgvector.psycopg import register_vector

from openagenticdam.config import Settings


def connect(settings: Settings) -> psycopg.Connection:
    conn = psycopg.connect(settings.dsn, connect_timeout=3)
    register_vector(conn)
    return conn
