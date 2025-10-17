import json
from decimal import Decimal
from typing import Any, Dict, Optional

from botocore.exceptions import ClientError


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


class S3JSONResourceStore:
    """Lightweight helper persisting tenant-scoped JSON payloads to S3."""

    def __init__(self, bucket: str, s3_client: Any, filename: str, base_prefix: str = "") -> None:
        self._bucket = bucket
        self._s3 = s3_client
        self._filename = filename.strip("/")
        self._base_prefix = base_prefix.strip("/ ")

    def _key_for(self, tenant_id: str) -> str:
        tenant_key = (tenant_id or "").strip("/ ")
        if not tenant_key:
            raise ValueError("tenant_id must be a non-empty string")
        key = f"{tenant_key}/data/{self._filename}"
        if self._base_prefix:
            return f"{self._base_prefix}/{key}"
        return key

    def load(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        key = self._key_for(tenant_id)
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            error_code = exc.response["Error"].get("Code")
            if error_code in {"NoSuchKey", "404"}:
                return None
            raise

        body = obj.get("Body")
        if body is None:
            return None
        raw = body.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def save(self, tenant_id: str, payload: Dict[str, Any]) -> None:
        key = self._key_for(tenant_id)
        body = json.dumps(payload, indent=2, sort_keys=True, default=_json_default).encode("utf-8")
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

    def delete(self, tenant_id: str) -> None:
        key = self._key_for(tenant_id)
        try:
            self._s3.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            error_code = exc.response["Error"].get("Code")
            if error_code in {"NoSuchKey", "404"}:
                return
            raise
