"""GCS helpers for the ICH index service — shared guideline index and per-program indexes."""
from __future__ import annotations

from pathlib import Path

from config.settings import settings


def download_index_from_gcs() -> None:
    from google.cloud import storage

    bucket_name = settings.gcs_bucket_name
    prefix = settings.gcs_index_prefix.rstrip("/")
    local_dir = settings.index_persist_dir
    local_dir.mkdir(parents=True, exist_ok=True)

    client = storage.Client()
    blobs = list(client.list_blobs(bucket_name, prefix=f"{prefix}/"))
    if not blobs:
        raise RuntimeError(
            f"No index files found at gs://{bucket_name}/{prefix}/. "
            "Run `python scripts/build_index.py` first."
        )

    for blob in blobs:
        relative = blob.name[len(prefix) + 1:]
        if not relative:
            continue
        dest = local_dir / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(dest))

    print(f"  Downloaded {len(blobs)} index file(s) to {local_dir}")


def upload_program_index_to_gcs(namespace: str, local_dir: Path) -> None:
    """Upload a per-program LlamaIndex to GCS under program_index/{namespace}/."""
    from google.cloud import storage  # noqa: PLC0415

    prefix = f"program_index/{namespace}"
    client = storage.Client()
    bucket = client.bucket(settings.gcs_bucket_name)
    count  = 0
    for f in local_dir.rglob("*"):
        if f.is_file():
            blob_name = f"{prefix}/{f.relative_to(local_dir)}"
            bucket.blob(blob_name).upload_from_filename(str(f))
            count += 1
    print(f"  Uploaded {count} program index file(s) to gs://{settings.gcs_bucket_name}/{prefix}/")


def download_program_index_from_gcs(namespace: str, local_dir: Path) -> None:
    """Download a per-program LlamaIndex from GCS to local_dir."""
    from google.cloud import storage  # noqa: PLC0415

    local_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"program_index/{namespace}"
    client = storage.Client()
    blobs  = list(client.list_blobs(settings.gcs_bucket_name, prefix=f"{prefix}/"))
    if not blobs:
        raise FileNotFoundError(
            f"No program index found at gs://{settings.gcs_bucket_name}/{prefix}/"
        )
    for blob in blobs:
        relative = blob.name[len(prefix) + 1:]
        if not relative:
            continue
        dest = local_dir / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(dest))
    print(f"  Downloaded {len(blobs)} program index file(s) to {local_dir}")
