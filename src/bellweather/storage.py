import json
from datetime import datetime
from functools import lru_cache

from google.api_core.exceptions import PreconditionFailed
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from bellweather.config import get_settings


class BronzeStore:
    def __init__(self, bucket: str | None = None):
        s = get_settings()
        self._bucket_name = bucket or s.bellweather_bucket
        if s.storage_emulator_host:
            self._client = storage.Client(
                project="local",
                credentials=AnonymousCredentials(),
                client_options={"api_endpoint": s.storage_emulator_host},
            )
            b = self._client.bucket(self._bucket_name)
            if not b.exists():
                self._client.create_bucket(self._bucket_name)
        else:
            self._client = storage.Client()
        self._bucket = self._client.bucket(self._bucket_name)

    def object_key(self, source, fetched_at: datetime, idempotency_key: str) -> str:
        return f"{source}/{fetched_at:%Y/%m/%d}/{idempotency_key}.json"

    def put(self, source, fetched_at, idempotency_key, envelope: dict) -> str:
        key = self.object_key(source, fetched_at, idempotency_key)
        blob = self._bucket.blob(key)
        try:
            blob.upload_from_string(
                json.dumps(envelope),
                content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed:
            pass  # already captured → immutable, treat as success
        return f"gs://{self._bucket_name}/{key}"

    def get(self, uri: str) -> dict:
        key = uri.removeprefix(f"gs://{self._bucket_name}/")
        return json.loads(self._bucket.blob(key).download_as_text())


@lru_cache
def get_bronze_store() -> BronzeStore:
    return BronzeStore()
