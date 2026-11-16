#!/bin/sh
# objectstore-init: idempotently create the private UKOS buckets and apply browser CORS rules.
# Uses only plain S3 calls (HeadBucket, CreateBucket, PutBucketCors, GetBucketCors), so it also works
# against Cloudflare R2 or any other S3-compatible provider. Safe to re-run at any time.
set -eu

: "${S3_ENDPOINT_URL:?}" "${S3_BUCKET_ORIGINALS:?}" "${S3_BUCKET_ARTIFACTS:?}"
export AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID:?}"
export AWS_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY:?}"
export AWS_DEFAULT_REGION="${S3_REGION:-us-east-1}"
export AWS_EC2_METADATA_DISABLED=true
export AWS_PAGER=""

s3api() { aws --endpoint-url "$S3_ENDPOINT_URL" s3api "$@"; }

# Wait for the S3 endpoint (compose already waits for the healthcheck; this covers `run --rm` too).
i=0
until s3api list-buckets >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then echo "objectstore-init: S3 endpoint $S3_ENDPOINT_URL not reachable" >&2; exit 1; fi
  sleep 1
done

# Build the CORS document from the comma-separated API_CORS_ORIGINS.
origins=""
old_ifs=$IFS; IFS=','
for o in ${API_CORS_ORIGINS:-}; do
  o=$(echo "$o" | tr -d ' ')
  [ -n "$o" ] || continue
  if [ "$o" = "*" ]; then echo "objectstore-init: refusing wildcard CORS origin" >&2; exit 1; fi
  origins="${origins:+$origins,}\"$o\""
done
IFS=$old_ifs

cors_file=/tmp/ukos-cors.json
cat >"$cors_file" <<EOF
{"CORSRules":[{"AllowedOrigins":[${origins}],"AllowedMethods":["GET","PUT","HEAD"],"AllowedHeaders":["*"],"ExposeHeaders":["ETag","x-amz-checksum-sha256","x-amz-version-id"],"MaxAgeSeconds":3000}]}
EOF

for bucket in "$S3_BUCKET_ORIGINALS" "$S3_BUCKET_ARTIFACTS"; do
  if s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    echo "objectstore-init: bucket $bucket exists"
  else
    # Buckets are private by default; no ACL or policy is ever applied.
    s3api create-bucket --bucket "$bucket" >/dev/null
    echo "objectstore-init: bucket $bucket created"
  fi
  if [ -n "$origins" ]; then
    s3api put-bucket-cors --bucket "$bucket" --cors-configuration "file://$cors_file"
    s3api get-bucket-cors --bucket "$bucket" >/dev/null
    echo "objectstore-init: CORS applied to $bucket for [${origins}]"
  else
    echo "objectstore-init: API_CORS_ORIGINS empty, no CORS applied to $bucket"
  fi
done
echo "objectstore-init: done"
