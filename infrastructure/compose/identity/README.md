# Local identity (Dex): test-only users and clients

These accounts exist **only** in the local Dex password DB defined in `dex.yaml`. They are test values for
development and automated tests. Never reuse these passwords anywhere else, and never enable this config outside a
local machine.

| User | Email | Password | Intended role in fixtures |
|---|---|---|---|
| alice | alice@ukos.test | `ukos-dev-alice-password` | organization owner |
| bob | bob@ukos.test | `ukos-dev-bob-password` | member of alice's organization |
| mallory | mallory@ukos.test | `ukos-dev-mallory-password` | outsider (cross-tenant attack tests) |

## Clients
- `ukos-web`: the confidential client used by the Next.js server (authorization code + PKCE). Its secret is `OIDC_CLIENT_SECRET`.
- `ukos-api`: the audience of API tokens. `ukos-web` requests scope `audience:server:client_id:ukos-api`, so tokens
  carry `aud: ["ukos-api", "ukos-web"]` and `azp: "ukos-web"` (verified 2026-11-16).

## Getting a token in tests (password grant, local only)

```bash
curl -s -u ukos-web:change-me-oidc-client-secret http://127.0.0.1:8380/dex/token \
  -d grant_type=password -d username=alice@ukos.test -d password=ukos-dev-alice-password \
  -d scope="openid email profile audience:server:client_id:ukos-api"
```

## Claims the API relies on
- `iss` must equal `OIDC_ISSUER_URL` (the public URL), even when JWKS is fetched from `OIDC_INTERNAL_URL`.
- `aud` must contain `OIDC_AUDIENCE`. `exp`, `iat` and the RS256 signature are always checked.
- Identity key = (`iss`, `sub`). `sub` is Dex's opaque encoding of user ID + connector. Email is a display attribute, not an identifier.
- Add `offline_access` to the scope to receive a refresh token.

To add a user, generate a bcrypt hash (`python -c "import bcrypt;print(bcrypt.hashpw(b'pw', bcrypt.gensalt()).decode())"`)
and add a `staticPasswords` entry with a new UUID `userID`.
