"""Runtime configuration. Every value comes from the environment or `.env` - no secrets in code."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Postgres. In compose OAD_DATABASE_URL is set; on the host it is assembled from .env.
    database_url: str | None = Field(default=None, alias="OAD_DATABASE_URL")
    postgres_user: str = Field(default="oad", alias="POSTGRES_USER")
    postgres_password: str = Field(default="", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="oad", alias="POSTGRES_DB")
    postgres_host: str = Field(default="127.0.0.1", alias="OAD_POSTGRES_HOST")
    postgres_port: int = Field(default=5433, alias="OAD_POSTGRES_PORT")

    broker_url: str = Field(default="redis://127.0.0.1:6379/0", alias="OAD_BROKER_URL")

    s3_endpoint: str = Field(default="http://127.0.0.1:8333", alias="OAD_S3_ENDPOINT")
    s3_access_key: str = Field(default="", alias="OAD_S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="", alias="OAD_S3_SECRET_KEY")
    s3_bucket: str = Field(default="oad-assets", alias="OAD_S3_BUCKET")
    exiftool: str = Field(default="exiftool", alias="OAD_EXIFTOOL")
    # Endpoint used when signing URLs handed to clients (they run on the host, not in compose).
    s3_public_endpoint: str | None = Field(default=None, alias="OAD_S3_PUBLIC_ENDPOINT")

    ollama_url: str = Field(default="http://127.0.0.1:11434", alias="OAD_OLLAMA_URL")
    embed_model: str = Field(default="nomic-embed-text", alias="OAD_EMBED_MODEL")
    embed_dim: int = Field(default=768, alias="OAD_EMBED_DIM")

    tenant_id: str = Field(default="00000000-0000-0000-0000-000000000001", alias="OAD_DEV_TENANT")

    dev_actor: str = Field(default="dev@localhost", alias="OAD_DEV_ACTOR")
    dev_principals: str = Field(default="", alias="OAD_DEV_PRINCIPALS")

    # HTTP transport only (stdio for Claude Desktop needs neither)
    api_token: str = Field(default="", alias="OAD_API_TOKEN")
    allowed_hosts: str = Field(
        default="127.0.0.1:*,localhost:*,host.docker.internal:*",
        alias="OAD_ALLOWED_HOSTS",
        description="Comma-separated Host header patterns accepted by the HTTP transport",
    )

    upload_max_bytes: int = Field(default=25 * 1024 * 1024, alias="OAD_UPLOAD_MAX_BYTES")
    upload_staging_dir: str = Field(
        default_factory=lambda: str(Path(tempfile.gettempdir()) / "oad-uploads"), alias="OAD_UPLOAD_STAGING_DIR"
    )

    @property
    def dsn(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sqlalchemy_url(self) -> str:
        return self.dsn.replace("postgresql://", "postgresql+psycopg://", 1)
