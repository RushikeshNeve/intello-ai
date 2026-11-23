#!/usr/bin/env bash
# M14.08 optional backup encryption (sourced by backup.sh / restore.sh, never executed).
# Tool: `openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt` (present in Git Bash and on any
# Linux host; age is not installed and the alpine helper image has no openssl CLI: no new installs).
# CBC is unauthenticated, so integrity comes from the manifest: the payload sha256 is checked BEFORE
# decrypting and every plaintext file sha256 (manifest.json, written before encryption) AFTER.
#   BACKUP_ENCRYPTION_KEY_FILE  file with >= 32 random chars (`openssl rand -base64 32 > backup.key`, chmod 600,
#                               kept OUTSIDE the backup root). BACKUP_ENCRYPTION_KEY_ID optional label; default is
#                               a sha256 fingerprint of the key file (16 hex). The key itself is never written.
# Encrypted layout: manifest.json + SHA256SUMS (plaintext metadata: versions, counts, object keys, hashes) +
# payload.tar.enc (postgres.dump, rushdb-data.tgz, rowcounts.txt, rushdb-counts.json, objects/).
BK_CIPHER="openssl-aes-256-cbc-pbkdf2-sha256-i200000"
BK_PAYLOAD_FILES=(postgres.dump rushdb-data.tgz rowcounts.txt rushdb-counts.json objects)

_bk_host() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
_bk_check_key() {
  [[ -f "$1" ]] || { echo "ERROR: backup encryption key file not found" >&2; return 1; }
  [[ "$(tr -d '\r\n' < "$1" | wc -c | tr -d ' ')" -ge 32 ]] || { echo "ERROR: backup key file must hold >= 32 random characters" >&2; return 1; }
}
bk_key_id() {
  if [[ -n "${BACKUP_ENCRYPTION_KEY_ID:-}" ]]; then printf '%s' "$BACKUP_ENCRYPTION_KEY_ID"
  else printf 'sha256:%s' "$(openssl dgst -sha256 -r "$(_bk_host "$1")" | cut -c1-16)"; fi
}
_bk_sha() { openssl dgst -sha256 -r "$(_bk_host "$1")" | cut -d' ' -f1 | tr -d '*'; }
_bk_manifest() { node -e "$1" "$(_bk_host "$2")/manifest.json" "${@:3}"; }

# bk_encrypt <backup-dir> <key-file> : replace the payload by payload.tar.enc and mark the manifest.
bk_encrypt() {
  local dir="$1" key="$2" kid
  _bk_check_key "$key" || return 1
  kid="$(bk_key_id "$key")"
  (cd "$dir" && tar -cf - "${BK_PAYLOAD_FILES[@]}") \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass "file:$(_bk_host "$key")" > "$dir/payload.tar.enc"
  (cd "$dir" && rm -rf "${BK_PAYLOAD_FILES[@]}")
  _bk_manifest "const fs=require('fs'),p=process.argv[1],m=JSON.parse(fs.readFileSync(p,'utf8'));
    m.encrypted=true;m.key_id=process.argv[2];
    m.encryption={cipher:process.argv[3],payload:'payload.tar.enc',payload_sha256:process.argv[4]};
    fs.writeFileSync(p,JSON.stringify(m,null,2));" "$dir" "$kid" "$BK_CIPHER" "$(_bk_sha "$dir/payload.tar.enc")"
}

# bk_is_encrypted <backup-dir> : exit 0 when the manifest says encrypted
bk_is_encrypted() {
  [[ "$(_bk_manifest "console.log(JSON.parse(require('fs').readFileSync(process.argv[1],'utf8')).encrypted===true)" "$1")" == "true" ]]
}

# bk_decrypt <backup-dir> <key-file> <out-dir> : verified plaintext copy of the backup in out-dir.
bk_decrypt() {
  local dir="$1" key="$2" out="$3" want_kid want_sha
  _bk_check_key "$key" || return 1
  want_kid="$(_bk_manifest "console.log(JSON.parse(require('fs').readFileSync(process.argv[1],'utf8')).key_id)" "$dir")"
  want_sha="$(_bk_manifest "console.log(JSON.parse(require('fs').readFileSync(process.argv[1],'utf8')).encryption.payload_sha256)" "$dir")"
  if [[ -z "${BACKUP_ENCRYPTION_KEY_ID:-}" && "$(bk_key_id "$key")" != "$want_kid" ]]; then
    echo "ERROR: the key file does not match the backup key id $want_kid" >&2; return 1; fi
  [[ "$(_bk_sha "$dir/payload.tar.enc")" == "$want_sha" ]] || { echo "ERROR: payload.tar.enc checksum mismatch" >&2; return 1; }
  mkdir -p "$out"
  cp "$dir/manifest.json" "$dir/SHA256SUMS" "$out/"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -pass "file:$(_bk_host "$key")" < "$dir/payload.tar.enc" \
    | (cd "$out" && tar -xf -) || { echo "ERROR: decryption failed (wrong key or corrupted payload)" >&2; return 1; }
}
