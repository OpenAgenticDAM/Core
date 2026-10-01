"""S3-compatible object storage client (SeaweedFS locally, any S3 in production)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config

from openagenticdam.config import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


def s3_client(settings: Settings, endpoint: str | None = None) -> S3Client:
    return boto3.client(
        "s3",
        endpoint_url=endpoint or settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def presign_get(settings: Settings, key: str, expires: int = 900) -> str:
    """Pre-signed GET URL. Signed against the public endpoint so a host-side client can use it."""
    client = s3_client(settings, endpoint=settings.s3_public_endpoint or settings.s3_endpoint)
    return client.generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=expires
    )
