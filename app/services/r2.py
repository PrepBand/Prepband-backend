"""Private Cloudflare R2 storage for the resource library.

The bucket is private. The API never proxies file bytes; it signs
short-lived URLs (AWS Signature V4, query-string form, which R2 speaks):

- PUT for an admin's browser to upload straight to R2. The URL is bound to
  the exact Content-Type and Content-Length the API validated, so a
  different or larger file is rejected by R2 itself.
- GET for a signed-in student to open a file or see a cover. It expires in
  minutes, so a copied link stops working.

After an upload, verify_object() reads the object's first bytes and checks
they match the declared type (a PDF really starts with %PDF), so a renamed
executable can't be published as a "PDF".

No SDK: SigV4 is ~40 lines of hmac/hashlib, tested against AWS's own
published example (tests/test_resources.py).
"""

import hashlib
import hmac
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import HTTPException

from ..core.config import get_settings

REGION = "auto"
SERVICE = "s3"

COVER_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
FILE_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
COVER_MAX_BYTES = 5 * 1024 * 1024
FILE_MAX_BYTES = 25 * 1024 * 1024

KEY_PATTERNS = {
    "cover": re.compile(r"^resources/covers/[0-9a-f-]{36}\.(jpg|png|webp)$"),
    "file": re.compile(r"^resources/files/[0-9a-f-]{36}\.(pdf|docx|pptx|jpg|png|webp)$"),
}


def _uri_encode(value: str, keep_slash: bool = False) -> str:
    return quote(value, safe="/-_.~" if keep_slash else "-_.~")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def presign(
    *,
    method: str,
    host: str,
    path: str,
    access_key: str,
    secret_key: str,
    region: str = REGION,
    expires: int = 300,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """A SigV4 query-string signed URL. `headers` (beyond host) become
    signed headers the client must send with exactly these values."""

    moment = now or datetime.now(timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    day = moment.strftime("%Y%m%d")
    scope = f"{day}/{region}/{SERVICE}/aws4_request"

    signed = {"host": host, **{k.lower(): v.strip() for k, v in (headers or {}).items()}}
    signed_names = ";".join(sorted(signed))
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": signed_names,
        **(params or {}),
    }
    canonical_query = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items()))
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in sorted(signed))
    canonical_request = "\n".join(
        [method, _uri_encode(path, keep_slash=True), canonical_query, canonical_headers, signed_names, "UNSIGNED-PAYLOAD"]
    )
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), SERVICE), "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"https://{host}{_uri_encode(path, keep_slash=True)}?{canonical_query}&X-Amz-Signature={signature}"


@dataclass(frozen=True)
class Bucket:
    account_id: str
    access_key: str
    secret_key: str
    name: str

    @property
    def host(self) -> str:
        return f"{self.account_id}.r2.cloudflarestorage.com"

    def _path(self, key: str) -> str:
        return f"/{self.name}/{key}"

    def upload_url(self, key: str, content_type: str, size_bytes: int, expires: int = 600) -> str:
        return presign(
            method="PUT",
            host=self.host,
            path=self._path(key),
            access_key=self.access_key,
            secret_key=self.secret_key,
            expires=expires,
            headers={"content-type": content_type, "content-length": str(size_bytes)},
        )

    def download_url(self, key: str, file_name: str | None = None, inline: bool = True, expires: int = 300) -> str:
        params: dict[str, str] = {}
        if file_name:
            safe = re.sub(r'[^\w.\- ]', "_", file_name)[:150] or "download"
            params["response-content-disposition"] = f'{"inline" if inline else "attachment"}; filename="{safe}"'
        return presign(
            method="GET",
            host=self.host,
            path=self._path(key),
            access_key=self.access_key,
            secret_key=self.secret_key,
            expires=expires,
            params=params,
        )

    def read_head(self, key: str, length: int = 16) -> tuple[bytes, int]:
        """The object's first bytes and its total size."""

        url = presign(method="GET", host=self.host, path=self._path(key), access_key=self.access_key, secret_key=self.secret_key, expires=60)
        request = urllib.request.Request(url, headers={"Range": f"bytes=0-{length - 1}"})  # noqa: S310 - signed https URL to our own bucket
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed https host
                data = response.read(length)
                total = response.headers.get("Content-Range", "").rpartition("/")[2]
                size = int(total) if total.isdigit() else int(response.headers.get("Content-Length") or len(data))
                return data, size
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise HTTPException(status_code=422, detail="The uploaded file wasn't found. Upload it again.") from exc
            raise HTTPException(status_code=502, detail="File storage didn't respond as expected. Try again.") from exc
        except OSError as exc:
            raise HTTPException(status_code=502, detail="File storage is unreachable. Try again.") from exc

    def delete(self, key: str) -> None:
        """Best effort: a leftover object costs storage, not correctness."""

        url = presign(method="DELETE", host=self.host, path=self._path(key), access_key=self.access_key, secret_key=self.secret_key, expires=60)
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="DELETE"), timeout=10)  # noqa: S310 - signed https URL to our own bucket
        except OSError:
            pass


def get_bucket() -> Bucket | None:
    settings = get_settings()
    if not (settings.r2_account_id and settings.r2_access_key_id and settings.r2_secret_access_key and settings.r2_resources_bucket):
        return None
    return Bucket(settings.r2_account_id, settings.r2_access_key_id, settings.r2_secret_access_key, settings.r2_resources_bucket)


def require_bucket() -> Bucket:
    bucket = get_bucket()
    if bucket is None:
        raise HTTPException(status_code=503, detail="Resource file storage isn't configured yet.")
    return bucket


def _looks_like(content_type: str, head: bytes) -> bool:
    if content_type == "application/pdf":
        return head.startswith(b"%PDF-")
    if content_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if content_type.startswith("application/vnd.openxmlformats-officedocument"):
        return head.startswith(b"PK\x03\x04")
    return False


def verify_object(bucket: Bucket, key: str, purpose: str, content_type: str) -> int:
    """Checks an uploaded object is what it claims to be; deletes it and
    refuses if not. Returns its size in bytes."""

    if not KEY_PATTERNS[purpose].match(key):
        raise HTTPException(status_code=422, detail="That file reference isn't valid.")
    head, size = bucket.read_head(key)
    limit = COVER_MAX_BYTES if purpose == "cover" else FILE_MAX_BYTES
    if not _looks_like(content_type, head) or size > limit:
        bucket.delete(key)
        raise HTTPException(status_code=422, detail="The uploaded file doesn't match its type or is too large.")
    return size
