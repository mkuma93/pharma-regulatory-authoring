"""Download the pre-built LlamaIndex from GCS to the local index_persist_dir."""
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
