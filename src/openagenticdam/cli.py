"""OpenAgenticDAM CLI: `openagenticdam serve|ingest|reindex`."""

from __future__ import annotations

import argparse
import uuid

from openagenticdam.config import Settings

WRITE_ACL_HELP = (
    "principals that may delete/change these assets in OpenAgenticDAM (default: same as --acl, i.e. you "
    "manage your own bucket; pass '' for read-only mirrors of foreign systems). Deleting never removes "
    "originals of synced files."
)


def _principals(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(prog="openagenticdam")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the MCP gateway")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    ing = sub.add_parser("ingest", help="ingest every object under a prefix of the configured bucket")
    ing.add_argument("--prefix", default="")
    ing.add_argument("--acl", default="group:everyone", help="comma-separated source-local principals")
    ing.add_argument("--write-acl", default=None, help=WRITE_ACL_HELP)
    ing.add_argument("--async", dest="use_queue", action="store_true", help="enqueue to Celery workers")

    rx = sub.add_parser(
        "reindex",
        help="re-read every stored original (S3 sync and chat uploads): metadata, renditions, embeddings. "
        "Idempotent - existing assets keep their UUID.",
    )
    rx.add_argument("--acl", default="group:everyone", help="comma-separated source-local principals")
    rx.add_argument("--write-acl", default=None, help=WRITE_ACL_HELP)

    sub.add_parser("dedupe", help="delete identical chat uploads, keeping the oldest copy of each file")

    args = parser.parse_args()
    settings = Settings()

    if args.cmd == "serve":
        if args.transport == "stdio":
            from openagenticdam.server import build_server

            build_server(settings).run("stdio")
            return
        import uvicorn

        from openagenticdam.http_app import MissingTokenError, build_http_app

        try:
            app = build_http_app(settings)
        except MissingTokenError as exc:
            raise SystemExit(f"error: {exc}") from exc
        # Compose publishes the port on 127.0.0.1 only; inside the container we bind 0.0.0.0.
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return

    from openagenticdam.db import connect
    from openagenticdam.ingest import ensure_source, ingest_s3_object
    from openagenticdam.storage import s3_client

    tenant = uuid.UUID(settings.tenant_id)
    if args.cmd == "dedupe":
        _dedupe(settings, tenant)
        return
    acl = _principals(args.acl)
    write_acl = acl if args.write_acl is None else _principals(args.write_acl)
    s3 = s3_client(settings)

    if args.cmd == "reindex":
        _reindex(settings, tenant, acl, write_acl)
        return

    keys = [
        o["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket, Prefix=args.prefix)
        for o in page.get("Contents", [])
        if not o["Key"].startswith(("thumbnails/", "renditions/", "chat-uploads/"))
    ]
    with connect(settings) as conn:
        source_id = ensure_source(conn, tenant, kind="s3", name=settings.s3_bucket, bucket=settings.s3_bucket)
        conn.commit()
    if args.use_queue:
        from openagenticdam.worker import ingest_object

        for k in keys:
            ingest_object.delay(str(tenant), str(source_id), k, acl, write_acl)
        print(f"enqueued {len(keys)} objects")
        return
    with connect(settings) as conn:
        for k in keys:
            ingest_s3_object(conn, settings, tenant, source_id, k, acl=acl, write_acl=write_acl)
            conn.commit()
            print(f"ingested {k}")
    print(f"done: {len(keys)} objects")


def _reindex(settings: Settings, tenant: uuid.UUID, acl: list[str], write_acl: list[str]) -> None:
    from openagenticdam.db import connect
    from openagenticdam.ingest import ensure_source, ingest_s3_object
    from openagenticdam.server import CHAT_UPLOAD_SOURCE
    from openagenticdam.storage import s3_client

    s3 = s3_client(settings)
    keys = [
        o["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket)
        for o in page.get("Contents", [])
        if not o["Key"].startswith(("thumbnails/", "renditions/"))
    ]
    ok = failed = 0
    with connect(settings) as conn:
        s3_src = ensure_source(conn, tenant, kind="s3", name=settings.s3_bucket, bucket=settings.s3_bucket)
        up_src = ensure_source(conn, tenant, kind="upload", name=CHAT_UPLOAD_SOURCE, bucket=settings.s3_bucket)
        conn.commit()
        for i, k in enumerate(keys, 1):
            uploaded = k.startswith(f"{CHAT_UPLOAD_SOURCE}/")
            try:
                ingest_s3_object(
                    conn,
                    settings,
                    tenant,
                    up_src if uploaded else s3_src,
                    k,
                    acl=acl,
                    write_acl=acl if uploaded else write_acl,
                )
                conn.commit()
                ok += 1
                print(f"[{i}/{len(keys)}] {k}")
            except Exception as exc:  # noqa: BLE001 - report every bad object, keep going
                conn.rollback()
                failed += 1
                print(f"[{i}/{len(keys)}] FAILED {k}: {type(exc).__name__}: {exc}")
    print(f"done: {ok} indexed, {failed} failed, {len(keys)} objects")
    if failed:
        raise SystemExit(1)


def _dedupe(settings: Settings, tenant: uuid.UUID) -> None:
    from openagenticdam.db import connect
    from openagenticdam.dedupe import remove_duplicate_uploads
    from openagenticdam.storage import delete_objects

    with connect(settings) as conn:
        ids, keys = remove_duplicate_uploads(conn, tenant)
        if ids:
            conn.execute(
                "INSERT INTO audit_log (tenant_id, actor, client, tool, params, asset_ids, outcome)"
                " VALUES (%s,%s,'cli','dedupe','{}',%s,'deleted')",
                (tenant, settings.dev_actor, ids),
            )
        conn.commit()
    delete_objects(settings, keys)
    print(f"removed {len(ids)} duplicate(s)" + "".join(f"\n  {i}" for i in ids))
