from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

Kind = Literal["unstructured", "structured"]


class Submission(BaseModel):
    source: str
    kind: Kind
    content_type: str
    fetched_at: datetime
    idempotency_key: str
    payload: dict | str | None = None
    payload_uri: str | None = None
    provenance: dict = {}

    @field_validator("fetched_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _exactly_one_payload(self):
        has_inline = self.payload is not None
        has_uri = self.payload_uri is not None
        if has_inline == has_uri:
            raise ValueError("provide exactly one of payload or payload_uri")
        return self


class IngestResult(BaseModel):
    raw_record_id: int
    status: Literal["created", "duplicate", "unroutable"]
    payload_uri: str
