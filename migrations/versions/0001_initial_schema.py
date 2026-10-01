"""initial schema: sources, assets, rights, acl, versions, embeddings, audit

Deviations from PRD v0.9 data model (see docs/ARCHITECTURE.md, "Schema decisions"):
- tenant_id on every table, so tenant isolation never depends on a join
- asset_acl carries source_id; principals are only meaningful within their source
- primary keys on asset_rights / asset_acl, ON DELETE CASCADE from assets
- embedding dimension fixed per migration (768 = nomic-embed-text); a model with another
  dimension needs its own column/table migration

Revision ID: 0001
Revises:
Create Date: 2026-10-01
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBED_DIM = 768


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE sources (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id UUID NOT NULL,
          kind TEXT NOT NULL,
          name TEXT,
          config JSONB NOT NULL DEFAULT '{}',
          last_sync_at TIMESTAMPTZ,
          sync_status TEXT NOT NULL DEFAULT 'never',
          UNIQUE (tenant_id, id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE assets (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id UUID NOT NULL,
          source_id UUID NOT NULL,
          external_id TEXT NOT NULL,
          hash_sha256 CHAR(64) NOT NULL,
          file_name TEXT NOT NULL,
          mime_type TEXT NOT NULL,
          file_size_bytes BIGINT NOT NULL,
          title TEXT,
          description TEXT,
          tags TEXT[] NOT NULL DEFAULT '{}',
          creator TEXT,
          technical_metadata JSONB NOT NULL DEFAULT '{}',
          ai_metadata JSONB NOT NULL DEFAULT '{}',
          deleted_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (source_id, external_id),
          UNIQUE (tenant_id, id),
          FOREIGN KEY (tenant_id, source_id) REFERENCES sources (tenant_id, id)
        )
        """
    )
    op.execute("CREATE INDEX assets_tenant_idx ON assets (tenant_id) WHERE deleted_at IS NULL")
    op.execute("CREATE INDEX assets_hash_idx ON assets (hash_sha256)")

    op.execute(
        """
        CREATE TABLE asset_rights (
          tenant_id UUID NOT NULL,
          asset_id UUID PRIMARY KEY,
          license TEXT,
          channels TEXT[],
          regions TEXT[],
          valid_from DATE,
          valid_until DATE,
          ai_generated BOOLEAN,
          c2pa_manifest JSONB,
          FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE asset_acl (
          tenant_id UUID NOT NULL,
          asset_id UUID NOT NULL,
          source_id UUID NOT NULL,
          principal TEXT NOT NULL,
          permission TEXT NOT NULL CHECK (permission IN ('read', 'write', 'admin')),
          synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (asset_id, source_id, principal, permission),
          FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX asset_acl_lookup_idx ON asset_acl (tenant_id, source_id, principal)")

    op.execute(
        """
        CREATE TABLE asset_versions (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id UUID NOT NULL,
          asset_id UUID NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('original', 'rendition', 'thumbnail')),
          storage_path TEXT NOT NULL,
          params JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE embeddings (
          tenant_id UUID NOT NULL,
          asset_id UUID NOT NULL,
          segment TEXT NOT NULL DEFAULT 'full',
          model TEXT NOT NULL,
          embedding vector({EMBED_DIM}) NOT NULL,
          PRIMARY KEY (asset_id, segment, model),
          FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX embeddings_hnsw_idx ON embeddings USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX embeddings_tenant_model_idx ON embeddings (tenant_id, model)")

    op.execute(
        """
        CREATE TABLE audit_log (
          id BIGSERIAL PRIMARY KEY,
          tenant_id UUID NOT NULL,
          actor TEXT NOT NULL,
          client TEXT,
          tool TEXT NOT NULL,
          params JSONB NOT NULL DEFAULT '{}',
          asset_ids UUID[] NOT NULL DEFAULT '{}',
          outcome TEXT NOT NULL,
          at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX audit_log_tenant_at_idx ON audit_log (tenant_id, at DESC)")


def downgrade() -> None:
    for t in ("audit_log", "embeddings", "asset_versions", "asset_acl", "asset_rights", "assets", "sources"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
