"""Provider-agnostic S3 contract suite for the UKOS object store (M00.02.03).

Run against any S3-compatible endpoint (see objectstore_support.py for env vars):

    uv run --no-project --with pytest --with boto3 --with httpx \
        pytest -q -rA tests/spikes/objectstore_contract.py

Outcome semantics:
  * PASS    - behaviour the platform depends on works.
  * FAIL    - required behaviour is missing or wrong (a finding, not something to hide).
  * SKIPPED "UNSUPPORTED [...]" - optional feature the provider does not implement; the
              platform must not rely on it (or must compensate in the API layer).
"""

from __future__ import annotations

import os
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
import pytest
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from objectstore_support import *  # noqa: F403 - pytest fixtures live here (no conftest by rule)
from objectstore_support import (
    MIB,
    StoreConfig,
    cors_rules,
    empty_bucket,
    error_code,
    http_status,
    make_client,
    sha256_b64,
    sha256_hex,
    unsupported,
)


def _payload(size: int) -> bytes:
    return os.urandom(size)


def _tamper_signature(url: str) -> str:
    parts = urlparse(url)
    qs = parse_qs(parts.query, keep_blank_values=True)
    sig = qs["X-Amz-Signature"][0]
    flipped = "1" if sig[0] == "0" else "0"
    qs["X-Amz-Signature"] = [flipped + sig[1:]]
    assert qs["X-Amz-Signature"][0] != sig
    return urlunparse(parts._replace(query=urlencode(qs, doseq=True)))


# --------------------------------------------------------------------------- buckets


def test_create_private_bucket(s3: BaseClient, store_cfg: StoreConfig, http: httpx.Client) -> None:
    name = f"ukos-private-{uuid.uuid4().hex[:10]}"
    s3.create_bucket(Bucket=name)
    try:
        s3.head_bucket(Bucket=name)
        assert name in [b["Name"] for b in s3.list_buckets()["Buckets"]]
        s3.put_object(Bucket=name, Key="probe.txt", Body=b"private")
        # Anonymous listing and reads of a freshly created bucket must be denied.
        anon_list = http.get(f"{store_cfg.endpoint}/{name}")
        anon_get = http.get(f"{store_cfg.endpoint}/{name}/probe.txt")
        assert anon_list.status_code in (401, 403), anon_list.text[:300]
        assert anon_get.status_code in (401, 403), anon_get.text[:300]
    finally:
        empty_bucket(s3, name)
        s3.delete_bucket(Bucket=name)


def test_wrong_credentials_rejected(store_cfg: StoreConfig) -> None:
    bad = make_client(StoreConfig(**{**store_cfg.__dict__, "secret_key": "not-the-secret-key-000"}))
    with pytest.raises(ClientError) as exc:
        bad.list_buckets()
    assert http_status(exc.value) in (401, 403)


# --------------------------------------------------------------------------- put / get


def test_put_with_sha256_and_get_byte_exact(s3: BaseClient, bucket: str, org_prefix: str) -> None:
    data = _payload(MIB + 123) + b"\x00\xff\r\n binary tail"
    key = f"{org_prefix}doc/v1/original.bin"
    resp = s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType="application/octet-stream",
        ChecksumSHA256=sha256_b64(data),
        Metadata={"sha256": sha256_hex(data), "version-id": "v1"},
    )
    assert resp["ResponseMetadata"]["HTTPStatusCode"] == 200

    head = s3.head_object(Bucket=bucket, Key=key)
    assert head["ContentLength"] == len(data)
    assert head["ContentType"] == "application/octet-stream"
    assert head["Metadata"].get("sha256") == sha256_hex(data)

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert body == data


def test_put_with_wrong_sha256_rejected(s3: BaseClient, bucket: str, org_prefix: str) -> None:
    data = b"the real bytes" * 1000
    key = f"{org_prefix}doc/v1/corrupt.bin"
    with pytest.raises(ClientError) as exc:
        s3.put_object(Bucket=bucket, Key=key, Body=data, ChecksumSHA256=sha256_b64(b"other bytes"))
    assert http_status(exc.value) == 400, exc.value.response
    # Nothing may have been stored under the key.
    with pytest.raises(ClientError) as missing:
        s3.head_object(Bucket=bucket, Key=key)
    assert http_status(missing.value) == 404


def test_stored_sha256_returned(s3: BaseClient, bucket: str, org_prefix: str) -> None:
    """Optional: provider persists the x-amz-checksum-sha256 and returns it (ChecksumMode)."""
    data = _payload(4096)
    key = f"{org_prefix}doc/v1/checksum.bin"
    s3.put_object(Bucket=bucket, Key=key, Body=data, ChecksumSHA256=sha256_b64(data))
    head = s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    returned = head.get("ChecksumSHA256")
    if not returned:
        unsupported("checksum-readback", "HeadObject(ChecksumMode=ENABLED) returned no ChecksumSHA256")
    assert returned == sha256_b64(data)


# --------------------------------------------------------------------------- presigned URLs


def test_presigned_get(s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client) -> None:
    data = _payload(2048)
    key = f"{org_prefix}doc/v1/presigned-read.bin"
    s3.put_object(Bucket=bucket, Key=key, Body=data)
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    r = http.get(url)
    assert r.status_code == 200, r.text[:300]
    assert r.content == data


def test_presigned_get_tampered_signature_rejected(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client
) -> None:
    key = f"{org_prefix}doc/v1/tamper.bin"
    s3.put_object(Bucket=bucket, Key=key, Body=b"secret")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    r = http.get(_tamper_signature(url))
    assert r.status_code == 403, (r.status_code, r.text[:300])
    # Re-pointing a valid signature at another key must also fail.
    other = url.replace("tamper.bin", "tamper2.bin")
    s3.put_object(Bucket=bucket, Key=f"{org_prefix}doc/v1/tamper2.bin", Body=b"other secret")
    r2 = http.get(other)
    assert r2.status_code == 403, (r2.status_code, r2.text[:300])


def test_presigned_get_expired_rejected(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client
) -> None:
    key = f"{org_prefix}doc/v1/expiring.bin"
    s3.put_object(Bucket=bucket, Key=key, Body=b"short lived")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=1)
    time.sleep(3)
    r = http.get(url)
    # AWS/R2 answer 403 AccessDenied; some providers answer 400 ("request expired"). Both deny.
    assert r.status_code in (400, 403), (r.status_code, r.text[:300])
    assert b"short lived" not in r.content


def test_presigned_put_browser_upload(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client, store_cfg: StoreConfig
) -> None:
    data = _payload(256 * 1024)
    key = f"{org_prefix}doc/v1/browser-upload.pdf"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": "application/pdf"},
        ExpiresIn=300,
    )
    r = http.put(url, content=data, headers={"Content-Type": "application/pdf", "Origin": store_cfg.cors_origin})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    got = s3.get_object(Bucket=bucket, Key=key)
    assert got["Body"].read() == data
    assert got["ContentType"] == "application/pdf"


def test_presigned_put_wrong_content_type_rejected(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client
) -> None:
    """Content-Type is a signed header: a browser cannot swap it."""
    key = f"{org_prefix}doc/v1/ct.pdf"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": "application/pdf"},
        ExpiresIn=300,
    )
    r = http.put(url, content=b"<html>", headers={"Content-Type": "text/html"})
    assert r.status_code == 403, (r.status_code, r.text[:300])


def test_presigned_put_with_sha256_enforced(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client
) -> None:
    """Optional: a presigned PUT bound to a SHA-256 rejects a body with a different hash."""
    expected = _payload(4096)
    key = f"{org_prefix}doc/v1/bound.bin"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ChecksumSHA256": sha256_b64(expected)},
        ExpiresIn=300,
    )
    hdr = {"x-amz-checksum-sha256": sha256_b64(expected)}
    bad = http.put(url, content=b"tampered body", headers=hdr)
    if bad.status_code == 200:
        unsupported("presigned-put-checksum", "upload with mismatching body was accepted (verify server-side)")
    assert bad.status_code in (400, 403), (bad.status_code, bad.text[:300])
    good = http.put(url, content=expected, headers=hdr)
    assert good.status_code == 200, (good.status_code, good.text[:300])
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == expected


def test_cors_preflight_for_presigned_put(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client, store_cfg: StoreConfig
) -> None:
    origin = store_cfg.cors_origin
    configured = True
    try:
        existing = s3.get_bucket_cors(Bucket=bucket).get("CORSRules", [])
        configured = any(origin in r.get("AllowedOrigins", []) for r in existing)
    except ClientError:
        configured = False
    if not configured:
        try:
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors_rules([origin]))
        except ClientError as exc:
            unsupported("bucket-cors", f"PutBucketCors -> {error_code(exc)} {http_status(exc)}")
        readback = s3.get_bucket_cors(Bucket=bucket).get("CORSRules", [])
        assert any(origin in r.get("AllowedOrigins", []) for r in readback), readback

    key = f"{org_prefix}doc/v1/cors.bin"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": "application/octet-stream"},
        ExpiresIn=300,
    )
    pre = http.request(
        "OPTIONS",
        url,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert pre.status_code in (200, 204), (pre.status_code, pre.text[:300])
    assert pre.headers.get("access-control-allow-origin") in (origin, "*"), dict(pre.headers)
    allowed_methods = pre.headers.get("access-control-allow-methods", "").upper()
    assert "PUT" in allowed_methods or allowed_methods == "*", dict(pre.headers)

    put = http.put(url, content=b"x", headers={"Origin": origin, "Content-Type": "application/octet-stream"})
    assert put.status_code == 200
    assert put.headers.get("access-control-allow-origin") in (origin, "*"), dict(put.headers)


def test_cors_preflight_unknown_origin_not_allowed(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client, store_cfg: StoreConfig
) -> None:
    try:
        rules = s3.get_bucket_cors(Bucket=bucket).get("CORSRules", [])
    except ClientError as exc:
        unsupported("bucket-cors", f"GetBucketCors -> {error_code(exc)}")
    if not any(store_cfg.cors_origin in r.get("AllowedOrigins", []) for r in rules):
        pytest.skip("bucket has no CORS rule for the web origin (run the preflight test first)")
    url = s3.generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": f"{org_prefix}evil.bin"}, ExpiresIn=300
    )
    pre = http.request(
        "OPTIONS",
        url,
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "PUT"},
    )
    assert pre.headers.get("access-control-allow-origin") not in ("http://evil.example", "*"), dict(pre.headers)


def test_anonymous_get_denied(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client, store_cfg: StoreConfig
) -> None:
    key = f"{org_prefix}doc/v1/anon.bin"
    s3.put_object(Bucket=bucket, Key=key, Body=b"not public")
    r = http.get(f"{store_cfg.endpoint}/{bucket}/{key}")
    assert r.status_code in (401, 403), (r.status_code, r.text[:300])
    assert b"not public" not in r.content
    r_list = http.get(f"{store_cfg.endpoint}/{bucket}?list-type=2&prefix={org_prefix}")
    assert r_list.status_code in (401, 403), (r_list.status_code, r_list.text[:300])
    r_put = http.put(f"{store_cfg.endpoint}/{bucket}/{org_prefix}anon-write.bin", content=b"x")
    assert r_put.status_code in (401, 403), (r_put.status_code, r_put.text[:300])


# --------------------------------------------------------------------------- multipart


def test_multipart_upload_complete(s3: BaseClient, bucket: str, org_prefix: str) -> None:
    key = f"{org_prefix}doc/v1/large.bin"
    parts_data = [_payload(5 * MIB), _payload(5 * MIB), _payload(1 * MIB + 7)]
    up = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="application/octet-stream")
    upload_id = up["UploadId"]
    parts = []
    for n, chunk in enumerate(parts_data, start=1):
        r = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=n, Body=chunk)
        parts.append({"PartNumber": n, "ETag": r["ETag"]})
    listed = s3.list_parts(Bucket=bucket, Key=key, UploadId=upload_id)["Parts"]
    assert [p["PartNumber"] for p in listed] == [1, 2, 3]
    s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
    whole = b"".join(parts_data)
    head = s3.head_object(Bucket=bucket, Key=key)
    assert head["ContentLength"] == len(whole)
    assert sha256_hex(s3.get_object(Bucket=bucket, Key=key)["Body"].read()) == sha256_hex(whole)


def test_multipart_upload_abort(s3: BaseClient, bucket: str, org_prefix: str) -> None:
    key = f"{org_prefix}doc/v1/aborted.bin"
    upload_id = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=1, Body=_payload(5 * MIB))
    pending = s3.list_multipart_uploads(Bucket=bucket, Prefix=org_prefix).get("Uploads", [])
    assert upload_id in [u["UploadId"] for u in pending]
    s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    with pytest.raises(ClientError) as exc:
        s3.list_parts(Bucket=bucket, Key=key, UploadId=upload_id)
    assert error_code(exc.value) == "NoSuchUpload" or http_status(exc.value) == 404
    pending = s3.list_multipart_uploads(Bucket=bucket, Prefix=org_prefix).get("Uploads", [])
    assert upload_id not in [u["UploadId"] for u in pending]
    with pytest.raises(ClientError):
        s3.head_object(Bucket=bucket, Key=key)


def test_presigned_multipart_part_upload(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client
) -> None:
    """Browser-direct multipart: the API presigns UploadPart URLs, the browser PUTs parts."""
    key = f"{org_prefix}doc/v1/browser-large.bin"
    chunks = [_payload(5 * MIB), _payload(MIB)]
    upload_id = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    parts = []
    for n, chunk in enumerate(chunks, start=1):
        url = s3.generate_presigned_url(
            "upload_part",
            Params={"Bucket": bucket, "Key": key, "UploadId": upload_id, "PartNumber": n},
            ExpiresIn=300,
        )
        r = http.put(url, content=chunk)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        parts.append({"PartNumber": n, "ETag": r.headers["etag"]})
    s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == b"".join(chunks)


# --------------------------------------------------------------------------- delete / purge


def test_delete_then_get_404_and_presigned_url_dead(
    s3: BaseClient, bucket: str, org_prefix: str, http: httpx.Client
) -> None:
    key = f"{org_prefix}doc/v1/to-delete.bin"
    s3.put_object(Bucket=bucket, Key=key, Body=b"delete me")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    assert http.get(url).status_code == 200
    s3.delete_object(Bucket=bucket, Key=key)

    with pytest.raises(ClientError) as exc:
        s3.get_object(Bucket=bucket, Key=key)
    assert http_status(exc.value) == 404 and error_code(exc.value) in ("NoSuchKey", "404")
    with pytest.raises(ClientError) as head_exc:
        s3.head_object(Bucket=bucket, Key=key)
    assert http_status(head_exc.value) == 404
    r = http.get(url)
    assert r.status_code == 404, (r.status_code, r.text[:300])
    assert b"delete me" not in r.content
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=key).get("Contents", [])
    assert listed == []


def test_delete_objects_batch(s3: BaseClient, bucket: str, org_prefix: str) -> None:
    keys = [f"{org_prefix}purge/{i}.bin" for i in range(5)]
    for k in keys:
        s3.put_object(Bucket=bucket, Key=k, Body=b"x")
    resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": False})
    assert sorted(d["Key"] for d in resp.get("Deleted", [])) == sorted(keys)
    assert not resp.get("Errors")
    assert s3.list_objects_v2(Bucket=bucket, Prefix=f"{org_prefix}purge/").get("KeyCount", 0) == 0


# --------------------------------------------------------------------------- immutability


def test_conditional_create_if_none_match(s3: BaseClient, bucket: str, org_prefix: str) -> None:
    key = f"{org_prefix}doc/v1/immutable.bin"
    s3.put_object(Bucket=bucket, Key=key, Body=b"first", IfNoneMatch="*")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b"second", IfNoneMatch="*")
        overwritten = True
    except ClientError as exc:
        overwritten = False
        assert http_status(exc) in (409, 412), exc.response
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if overwritten:
        unsupported("if-none-match", f"second PutObject(If-None-Match: *) overwrote the key (body now {body!r})")
    assert body == b"first"


# --------------------------------------------------------------------------- listing


def test_list_objects_v2_org_prefix(s3: BaseClient, bucket: str, org_prefix: str) -> None:
    other = f"org/{uuid.uuid4().hex}/"
    mine = [f"{org_prefix}doc/{i}/v1/original.bin" for i in range(3)]
    try:
        for k in mine:
            s3.put_object(Bucket=bucket, Key=k, Body=b"m")
        s3.put_object(Bucket=bucket, Key=f"{other}doc/0/v1/original.bin", Body=b"o")
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=org_prefix)
        assert sorted(o["Key"] for o in resp.get("Contents", [])) == sorted(mine)
        assert resp["KeyCount"] == 3

        page1 = s3.list_objects_v2(Bucket=bucket, Prefix=org_prefix, MaxKeys=2)
        assert page1["IsTruncated"] is True and len(page1["Contents"]) == 2
        page2 = s3.list_objects_v2(
            Bucket=bucket, Prefix=org_prefix, MaxKeys=2, ContinuationToken=page1["NextContinuationToken"]
        )
        assert len(page2["Contents"]) == 1
        keys = [o["Key"] for o in page1["Contents"] + page2["Contents"]]
        assert sorted(keys) == sorted(mine)

        delim = s3.list_objects_v2(Bucket=bucket, Prefix=f"{org_prefix}doc/", Delimiter="/")
        assert len(delim.get("CommonPrefixes", [])) == 3
    finally:
        empty_bucket(s3, bucket, other)
