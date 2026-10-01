"""full embedded metadata (ExifTool) + separately stored personal data

asset_metadata            every tag ExifTool reads (EXIF, XMP, IPTC, ICC, maker notes, ...)
asset_sensitive_metadata  personal data under GDPR Art. 4: location, face/person regions,
                          device serial numbers, owner names. Stored for later, explicitly
                          authorised use; never returned by the MCP tools.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE asset_metadata (
          tenant_id UUID NOT NULL,
          asset_id UUID PRIMARY KEY,
          tool TEXT NOT NULL,
          extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          data JSONB NOT NULL,
          FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX asset_metadata_data_idx ON asset_metadata USING gin (data jsonb_path_ops)")
    op.execute(
        """
        CREATE TABLE asset_sensitive_metadata (
          tenant_id UUID NOT NULL,
          asset_id UUID PRIMARY KEY,
          latitude DOUBLE PRECISION CHECK (latitude BETWEEN -90 AND 90),
          longitude DOUBLE PRECISION CHECK (longitude BETWEEN -180 AND 180),
          altitude_m DOUBLE PRECISION,
          direction_deg DOUBLE PRECISION,
          accuracy_m DOUBLE PRECISION,
          face_count INTEGER NOT NULL DEFAULT 0,
          data JSONB NOT NULL,
          extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE asset_sensitive_metadata IS "
        "'Personal data (GDPR Art. 4): location, face/person regions, device serials, owner names. "
        "Never returned by MCP tools.'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS asset_sensitive_metadata")
    op.execute("DROP TABLE IF EXISTS asset_metadata")
