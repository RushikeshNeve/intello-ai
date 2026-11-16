# Spike M00.02.03 — Local S3-compatible object storage

- **Status:** Done (evaluation run 2026-11-16 on the Windows 11 / Docker Desktop 29.3.1 dev host)
- **Decides:** DC-002 / ADR-051 provider for the local `objectstore` service
- **Recommendation:** **RustFS `rustfs/rustfs:1.0.5`**. Fallback: **SeaweedFS `chrislusf/seaweedfs:4.53`** (`weed mini`).
- **Evidence:** `tests/spikes/objectstore_contract.py` (21 checks) run against all three candidates from
  `infrastructure/compose/spikes/objectstore-candidates.yml` (project `ukos-objectstore-eval`).

## 1. What we need (from the architecture)

| Need | Source | How the suite checks it |
|---|---|---|
| $0, local, single node, official image | Master architecture, ADR-051 | Official Docker Hub images, one container each |
| S3 API only, so we can swap to Cloudflare R2 later | ADR-007/028/051 | Only plain S3 calls: path-style SigV4, no vendor admin API at runtime |
| Immutable originals keyed by version ID | ADR-007/009 | Keys `org/<org>/doc/<doc>/v<version>/…`; `If-None-Match: *` conditional create checked |
| Presigned browser uploads and reads | ADR-028 | Presigned GET/PUT/UploadPart driven by `httpx` (stands in for the browser), CORS preflight |
| Deletion with verified purge | ADR-018/032 | Delete → GET/HEAD 404, listing empty, presigned URL issued before the delete is dead |
| SHA-256 checksums | ADR-010 | `x-amz-checksum-sha256` accepted, **wrong checksum rejected**, checksum read back |
| Multipart for large files | — | 5 MiB + 5 MiB + 1 MiB complete; abort path; presigned UploadPart |
| Private buckets | ADR-028 | Anonymous GET/LIST/PUT denied, wrong secret rejected |
| Backups | ADR-056 | Section 7 |

MinIO is out of scope. `minio/minio` on GitHub is archived (`archived: true`, last push 2026-04-24, AGPL-3.0).
The Docker Hub tag API for `minio/minio` returned HTTP 404 on 2026-11-16, so there is no maintained Community
Edition image to pin. DC-002 stands.

## 2. Candidates

| | SeaweedFS | RustFS | Garage |
|---|---|---|---|
| Image (pinned, pulled and run) | `chrislusf/seaweedfs:4.53` | `rustfs/rustfs:1.0.5` | `dxflrs/garage:v2.5.0` |
| Licence | Apache-2.0 | Apache-2.0 | AGPL-3.0 (see §6) |
| Language / shape | Go; master + volume + filer + S3 gateway in one `weed mini` process | Rust; one binary | Rust; one binary, `FROM scratch` image |
| Recent releases | 4.53 on 2026-11-08, 4.52 on 11-02, 4.51 on 10-25, 4.50 on 10-25, 4.49 on 10-23, 4.48 on 09-28 (about weekly) | 1.0.5 on 2026-11-10, 1.0.4 on 10-25, 1.0.3 on 10-17, 1.0.2 on 10-16, 1.0.1 on 10-03, **1.0.0 GA on 2026-09-16** (rc.6 on 09-11) | v2.5.0 on 2026-10-24, v2.4.1 on 09-08, v2.4.0 on 09-06, v2.3.0 on 04-16, v2.2.0 on 01-24 (every few months) |
| Project activity (2026-11-16) | about 36.1k GitHub stars, last push 2026-11-15 | about 36.1k GitHub stars, last push 2026-11-15 | Developed on Deuxfleurs' Forgejo; nightly commit images still being published (latest 2026-11-15) |
| Compressed image (Docker Hub `full_size`) | 97.0 MB (amd64) | 134.8 MB | 26.5 MB |
| Local image size (`docker image ls`) | 1.08 GB | 485 MB | 97.1 MB |
| RAM (`docker stats`), idle after start | 69 MiB | 35 MiB | 8 MiB |
| RAM after one suite run (≈30 MiB of uploads) | 339 MiB | 64 MiB | 30 MiB |
| Setup | Write an identities JSON (`-s3.config`) holding the access/secret pair. Turn off WebDAV and the admin UI. Set `-s3.allowedOrigins` (it defaults to `*`). | Env vars only: `RUSTFS_ACCESS_KEY`, `RUSTFS_SECRET_KEY`, `RUSTFS_ADDRESS`, `RUSTFS_VOLUMES`. Buckets are created over S3. | Write a TOML config (rpc secret, region). Then a one-time **cluster layout assign/apply**, `key import` (key ID must be `GK` + 24 hex, secret 64 hex) and `key allow --create-bucket`. These are CLI or admin-API calls, not S3 calls. |
| Listens on 9000 natively? | Yes, with `-s3.port=9000` (default 8333) | Yes (default `:9000`) | No, 3900 (configurable) |

Sources: GitHub releases API for `seaweedfs/seaweedfs` and `rustfs/rustfs`, GitHub repo API (archived flag,
licence, stars, pushed_at), Docker Hub tag API for `chrislusf/seaweedfs`, `rustfs/rustfs`, `dxflrs/garage` and
`minio/minio`. All queried 2026-11-16:
<https://github.com/seaweedfs/seaweedfs/releases>, <https://github.com/rustfs/rustfs/releases>,
<https://hub.docker.com/r/dxflrs/garage/tags>, <https://garagehq.deuxfleurs.fr/>, <https://github.com/minio/minio>.

## 3. How the suite was run

```bash
docker compose -p ukos-objectstore-eval -f infrastructure/compose/spikes/objectstore-candidates.yml up -d --wait
# Garage only, once per fresh volume (CLI inside the container):
#   garage node id -q  →  garage layout assign -z dc1 -c 1G <id>  →  garage layout apply --version 1
#   garage key import --yes -n ukos-eval GK0123456789abcdef01234567 <64-hex>  →  garage key allow --create-bucket <key>

OBJSTORE_PROVIDER=rustfs OBJSTORE_ENDPOINT=http://127.0.0.1:9302 \
OBJSTORE_ACCESS_KEY=eval-rustfs-access OBJSTORE_SECRET_KEY=eval-rustfs-secret-key-0000 \
uv run --no-project --with pytest --with pytest-timeout --with boto3 --with httpx \
  python -m pytest -q -rA -p no:cacheprovider --timeout 120 tests/spikes/objectstore_contract.py
# same for seaweedfs (127.0.0.1:9301) and garage (127.0.0.1:9303) with their eval credentials
```

On this host, Windows Application Control blocks uv's `pytest.exe` launcher (os error 4551), so the suite runs as
`python -m pytest`. Client versions: boto3/botocore 1.44.3, httpx 0.28.1, pytest 9.2.0. The botocore client sets
`request_checksum_calculation="when_required"`, so botocore's default CRC32 trailers are not sent. Checksums are
only sent where a test sends them explicitly.

Each test gets its own `org/<uuid>/` prefix. When `OBJSTORE_BUCKET` is unset, the session creates a temporary
bucket and deletes it at the end. An optional feature the provider lacks is reported as
`SKIPPED UNSUPPORTED [...]`; required behaviour that is missing is a `FAILED`.

## 4. Results matrix (real results, final run 2026-11-16)

| # | Contract check | SeaweedFS 4.53 | RustFS 1.0.5 | Garage v2.5.0 |
|---|---|---|---|---|
| 1 | Create private bucket; anonymous list/get of new bucket denied | PASS | PASS | PASS |
| 2 | Wrong secret key rejected (403) | PASS | PASS | PASS |
| 3 | PutObject with `x-amz-checksum-sha256` + user metadata; HEAD/GET byte-exact | PASS | PASS | PASS |
| 4 | PutObject with **wrong** SHA-256 rejected (400) and nothing stored | PASS | PASS | PASS |
| 5 | Stored SHA-256 returned by HeadObject `ChecksumMode=ENABLED` | PASS | PASS | PASS |
| 6 | Presigned GET returns exact bytes | PASS | PASS | PASS |
| 7 | Presigned GET with tampered signature → 403; signature re-pointed at another key → 403 | PASS | PASS | PASS |
| 8 | Expired presigned GET (TTL 1 s) denied, body not leaked | PASS (403) | PASS (403) | PASS (**400** "Date is too old", not AWS's 403) |
| 9 | Presigned PUT upload from "browser" (httpx), content type kept | PASS | PASS | PASS |
| 10 | Presigned PUT with a different Content-Type than signed → 403 | PASS | PASS | PASS |
| 11 | Presigned PUT bound to a SHA-256 rejects a mismatching body, accepts the right one | PASS | PASS | PASS |
| 12 | Bucket CORS (`PutBucketCors`/`GetBucketCors`) + preflight for presigned PUT from `http://127.0.0.1:3300` | PASS | PASS | PASS (but answers `*`, see 13) |
| 13 | Preflight from an unknown origin (`http://evil.example`) **not** allowed | PASS | PASS | **FAIL**: replies `Access-Control-Allow-Origin: *` and `Allow-Methods: *` whatever the bucket's CORS rules are |
| 14 | Anonymous GET / LIST / PUT on objects denied | PASS | PASS | PASS |
| 15 | Multipart: 5 MiB + 5 MiB + 1 MiB parts, ListParts, complete, SHA-256 of whole object | PASS | PASS | PASS |
| 16 | Multipart abort: upload disappears from ListMultipartUploads, ListParts → NoSuchUpload, no object | PASS | PASS | PASS |
| 17 | Presigned UploadPart (browser-direct multipart) + complete | PASS | PASS | PASS |
| 18 | DeleteObject → GET/HEAD 404, listing empty, previously issued presigned GET → 404 | PASS | PASS | PASS |
| 19 | DeleteObjects batch (5 keys), all reported deleted, prefix empty | PASS | PASS | PASS |
| 20 | Conditional create `If-None-Match: *` refuses to overwrite (412) | PASS | PASS | **UNSUPPORTED**: second PUT overwrote the key |
| 21 | ListObjectsV2 with `org/<id>/` prefix isolation, MaxKeys pagination, delimiter common prefixes | PASS | PASS | PASS |
| | **Totals** | **21 passed** | **21 passed** | **19 passed, 1 failed, 1 unsupported** |

Extra CORS probe (same stacks, one-off script, not part of the suite):

| Situation | SeaweedFS | RustFS | Garage |
|---|---|---|---|
| Bucket with no CORS rule, allowed web origin | 200 + origin echoed (taken from the global `-s3.allowedOrigins` flag) | 200, no CORS headers (browser blocks it) | 200, `*` |
| Bucket with no CORS rule, unknown origin | 403 | 200, no CORS headers (browser blocks it) | 200, `*` |
| Bucket rule for the web origin, preflight for `DELETE` (method not in the rule) | 200 + origin, no `Allow-Methods` (browser blocks it) | 403 | 200, `*` |

Fixes made to the suite during the evaluation:
- The tamper helper sometimes left the signature unchanged (1 in 16 chance). It now always flips a character and
  asserts the change.
- The expired-URL check now accepts 400 or 403, because both deny access. Garage's 400 is recorded in the matrix.
- An `exc.value` bug in the If-None-Match test was fixed.

The earlier run that hit these bugs is not counted.

## 5. Cloudflare R2 compatibility notes

So the swap stays trivial, the API layer (M02) must use only what both the local provider and R2 support:

- **Use:** PutObject/GetObject/HeadObject/DeleteObject(s), ListObjectsV2, multipart (create, upload part,
  complete, abort, list), SigV4 presigned GET/PUT/UploadPart, `x-amz-checksum-sha256` on PUT, user metadata,
  PutBucketCors. Cloudflare's R2 S3-compatibility docs list these, including conditional `If-None-Match`
  writes. This spike did not test against R2 ($0 rule), so re-run this suite against R2 before the swap.
- **Avoid:** S3 bucket versioning and object lock. R2 has no S3 versioning and our design doesn't need it: the
  immutable version ID lives in the key (`org/<org>/doc/<doc>/v<ver>/original`) and in PostgreSQL. Also avoid
  ACLs and bucket policies (R2 has no S3 ACLs; buckets are private by default), STS/IAM APIs, provider admin
  APIs at runtime, SSE-KMS, and anonymous/public-read buckets.
- **Immutability is enforced in the app.** PostgreSQL allocates version IDs, so keys are never reused. On
  RustFS/SeaweedFS/R2 the API also sends `If-None-Match: *` on writes to original keys as defence in depth. The
  adapter must treat a 412 as a "key already exists" bug, not as success.
- **Path-style** addressing locally (`S3_FORCE_PATH_STYLE=true`). R2 accepts both. The region is `us-east-1`
  locally and `auto` on R2, so keep `S3_REGION` configurable.
- **Expired/denied status codes** differ between providers (400 vs 403). Clients must treat any 4xx on a
  presigned URL as "re-request a URL from the API", not branch on the exact code.
- **CORS** comes from bucket CORS rules on R2 too. The init job applies them from `API_CORS_ORIGINS`.

## 6. Licence notes

- RustFS and SeaweedFS are Apache-2.0, with no obligations beyond attribution. Both are safe to bundle in Compose.
- Garage is **AGPL-3.0**. Running the unmodified upstream image as a separate network service doesn't make our
  code a derivative work. However, if we ever patched Garage and offered it to users over a network (hosted
  SaaS), we would have to publish those changes. That obligation doesn't arise today, but it is extra review load
  for a provider that also failed CORS (13) and conditional writes (20).

## 7. Backup approach (input for M00.03.05 / ADR-056)

Recommended for every provider and for R2: a **logical, S3-level backup**.

1. **Primary: S3 mirror.** Copy both buckets object by object to a backup target (a local directory, a second
   bucket, or later R2) using the same S3 credentials. The tool can be `rclone sync` (official `rclone/rclone`
   image) or a small boto3 script in the API's maintenance CLI. Record and verify SHA-256 per object against the
   `sha256` metadata, or against the PostgreSQL version row. This is provider-neutral, survives a provider swap,
   and restores into any S3 store.
2. **Secondary: cold volume snapshot.** Stop `objectstore`, then
   `docker run --rm -v ukos_objectstore-data:/data -v <dir>:/backup alpine tar czf /backup/objectstore.tgz -C /data .`,
   then restart it. RustFS's on-disk layout (`/data/<bucket>/<key>/xl.meta` plus `.rustfs.sys`) is internal, so a
   snapshot is only valid for the **same provider and version**, and only when taken while stopped.
3. **Order with PostgreSQL:** back up PostgreSQL first, then objects. Objects are immutable and written before
   their version row commits, so the object backup is always a superset of what the DB references. Orphan
   objects are harmless and are reaped by the purge job.
4. **Deletion vs backups (ADR-018/032):** backups must expire on the retention schedule, so purged originals
   don't live on forever in old snapshots. The purge job verifies with HEAD → 404 (check 18).

## 8. Risks

| Risk | Provider | Mitigation |
|---|---|---|
| Young 1.x codebase (GA 2026-09-16), fast release cadence, possible regressions or security advisories | RustFS | Pin an exact tag. Re-run this contract suite before any bump (it is the upgrade gate). Watch GitHub security advisories. SeaweedFS is a tested drop-in fallback because only the S3 API is used. |
| Default credentials if env vars are missing (the image warns and falls back to built-in defaults) | RustFS | Compose uses `${S3_ACCESS_KEY_ID:?}` / `${S3_SECRET_ACCESS_KEY:?}`, so a missing value fails fast. The port binds to 127.0.0.1 only. |
| Global CORS defaults to `*`; WebDAV, admin UI and Iceberg are on by default; heavier RAM; filer metadata must stay under `/data` | SeaweedFS | If used as the fallback: pass `-s3.allowedOrigins`, `-webdav=false`, `-admin.ui=false`, and keep `/data` on the named volume |
| Permissive CORS preflight; no If-None-Match protection; extra layout/key bootstrap; AGPL | Garage | Not selected |
| Provider status codes differ from AWS/R2 | all | The adapter branches on 2xx vs 4xx, plus `NoSuchKey`/`NoSuchUpload`/`PreconditionFailed` codes only |
| The suite runs against a single node on a dev laptop; no durability or performance claims | all | Durability on the hosted path comes from R2. Locally, backups (§7). |

## 9. Recommendation

**Use RustFS `rustfs/rustfs:1.0.5`** for the local `objectstore` service:

- It passed all 21 contract checks, including wrong-checksum rejection, presigned checksum binding, strict
  bucket-scoped CORS and `If-None-Match` conditional create.
- It listens on container port 9000 natively, matching `S3_ENDPOINT_URL=http://objectstore:9000`.
- Credentials come straight from env vars that map onto `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`, so no
  config file has to be templated.
- It has the lowest RAM of the two finalists (35 MiB idle).
- It is Apache-2.0 and its image runs as non-root (uid 10001).

**Fallback: SeaweedFS `chrislusf/seaweedfs:4.53`** (`weed mini -s3.port=9000 -s3.config=… -webdav=false
-admin.ui=false -s3.allowedOrigins=…`). It also passed 21/21, has the longer track record and the more mature
project, but uses more RAM, needs a templated identities file, and has permissive defaults.

**Not selected: Garage v2.5.0.** Its CORS preflight is permissive regardless of bucket rules (check 13). It
ignores `If-None-Match` (check 20). It needs non-S3 cluster/key bootstrap. And it is AGPL.

The swap path to R2 (or to SeaweedFS) is: change `S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL`, `S3_REGION` and the
credentials, run the init job (or R2's bucket/CORS setup), then run this contract suite against it.
