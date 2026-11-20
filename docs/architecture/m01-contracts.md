# M01 Contracts: identity, tenancy, authorization, audit

Status: **frozen for M01 batch 1** (2026-11-16). Changing anything here requires coordinator approval and a
`DECISION_CHANGES.md` entry. These contracts are what parallel M01 tasks implement against. ADRs: 026, 027, 028, 031, 044.

## 1. Identity and principal

- The API authenticates **only** with `Authorization: Bearer <JWT>` issued by the OIDC provider (Dex locally, DC-012).
- Validation rules (M01.01.01): RS256 only (reject `none` and HS*); signature checked against JWKS fetched from
  `OIDC_INTERNAL_URL/keys` (cached, refetched once on an unknown `kid`); `iss == OIDC_ISSUER_URL` exactly;
  `OIDC_AUDIENCE ∈ aud`; `exp` in the future and `iat` not in the future (leeway ≤ 30 s); `sub` non-empty. Any failure →
  **401** with `WWW-Authenticate: Bearer error="invalid_token"` and body `{"detail": "invalid_token"}`. The reason
  goes to logs only, never the token.
- External identity key = (`iss`, `sub`). Email is an attribute, never an identifier.
- Provisioning (M01.01.03): first authenticated request upserts `users` on (`issuer`, `subject`). This is idempotent
  and race-safe (`INSERT … ON CONFLICT DO UPDATE`), and refreshes `email`, `display_name` and `last_seen_at`.

```python
# app/core/security.py  (foundation, coordinator-owned)
@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID        # users.id (internal)
    issuer: str
    subject: str
    email: str | None
    display_name: str | None

async def get_current_principal(...) -> Principal   # FastAPI dependency: verify → provision → Principal
```

`get_current_principal` composes two replaceable pieces, wired at startup:
`TokenVerifier.verify(token: str) -> VerifiedClaims` (M01.01.01, `app/modules/identity/oidc.py`) and
`UserProvisioner.provision(claims: VerifiedClaims) -> Principal` (M01.01.03, `app/modules/identity/provisioning.py`).
`VerifiedClaims` is a frozen dataclass defined in `app/core/security.py`: `issuer`, `subject`, `email`, `email_verified`,
`name`, `audience: tuple[str, ...]`, `expires_at: datetime`, `raw: Mapping[str, Any]`.

## 2. Tenancy model (PostgreSQL is authoritative, ADR-013)

| Table | Key columns | Notes |
|---|---|---|
| `users` | `id uuid pk`, `issuer`, `subject`, `email`, `display_name`, `created_at`, `last_seen_at` | `unique(issuer, subject)` |
| `organizations` | `id uuid pk`, `slug` (unique, `^[a-z0-9][a-z0-9-]{1,62}$`), `name`, `created_by → users`, `created_at`, `deleted_at` | soft delete only |
| `organization_memberships` | `org_id → organizations`, `user_id → users`, `role` (`owner`\|`admin`\|`member`), `created_at` | pk(`org_id`, `user_id`). **Every live org has ≥ 1 owner** (deferred constraint trigger) |
| `workspaces` | `id uuid pk`, `org_id → organizations`, `slug` (unique per org), `name`, `created_by`, `created_at`, `deleted_at` | |
| `workspace_memberships` | `workspace_id → workspaces`, `org_id` (denormalized, must match the workspace), `user_id → users`, `role` (`admin`\|`editor`\|`viewer`), `created_at` | pk(`workspace_id`, `user_id`); the user must be an org member |
| `resource_grants` | `id uuid pk`, `org_id`, `workspace_id`, `resource_type` (`document`), `resource_id uuid`, `grantee_type` (`user`\|`workspace_role`), `grantee_id` (user uuid or role name), `permission` (`read`\|`write`\|`manage`), `granted_by`, `created_at`, `expires_at null`, `revoked_at null`, `revoked_by null` | active ⇔ `revoked_at is null and (expires_at is null or expires_at > now())` |
| `audit_events` | `id uuid pk`, `occurred_at`, `org_id null`, `actor_user_id null`, `event_type`, `target_type`, `target_id`, `outcome` (`success`\|`denied`\|`error`), `request_id`, `metadata jsonb` (redacted) | **append-only**: the app role has INSERT and SELECT only |

All ids are UUIDv4 generated in the application. Timestamps are `timestamptz`, UTC.

### RLS (ADR-028, M01.03.02 tests it)
- Every org-scoped table has `ENABLE` + `FORCE ROW LEVEL SECURITY`. The API connects as the `ukos_app` role (NOBYPASSRLS).
- Each request transaction sets context with `set_config(..., true)` (transaction-local):
  `app.current_user_id` (always, when authenticated) and `app.current_org_id` (when the route is org-scoped).
- Policies (summary):
  - `organizations`: visible if `id = current_org_id` OR the current user has a membership row; insert only with `created_by = current_user_id`.
  - `organization_memberships`: visible if `org_id = current_org_id` OR `user_id = current_user_id`; writes only within `current_org_id`.
  - `workspaces`, `workspace_memberships`, `resource_grants`, `audit_events`: rows only where `org_id = current_org_id`.
  - `users`: visible if `id = current_user_id` OR they share `current_org_id` membership.
- With **no context set, nothing org-scoped is visible** (fail closed).
- **Identity context (DC-014):** the provisioner can't know `users.id` before upserting, so it also sets
  transaction-local `app.current_issuer` / `app.current_subject` (`set_identity_context()`). The `users` SELECT,
  INSERT and UPDATE policies additionally allow the row matching that verified (issuer, subject).
- **Org-less audit (DC-014):** the `audit_events` INSERT check also allows `org_id IS NULL AND (actor_user_id IS NULL OR
  actor_user_id = current_user_id)`, for events such as `user.provisioned`. SELECT stays current-org only. Audit rows are
  inserted without RETURNING (`eager_defaults=False`).
- Write policies beyond the summary: `organizations` UPDATE only where `id = current_org_id`; there is no DELETE grant on
  `organizations`, `workspaces` or `resource_grants` (soft delete, and revoke is an update).
- **Database error → HTTP mapping:** SQLSTATE `23514` with constraint `organization_has_owner` (raised at COMMIT, because the
  check is deferred) → 409 `last_owner`. `uq_organizations_slug` / `uq_workspaces_org_id_slug` (and
  `ck_organizations_slug_format` for a case-variant collision) → 409 `slug_taken`. Audit UPDATE/DELETE → 42501.
- RLS is defense in depth. **Authorization is still decided by the policy service (§3).**

## 3. Authorization (M01.03.01 policy, M01.03.03 grants)

Roles: org `owner > admin > member`; workspace `admin > editor > viewer`. Org owner/admin have implicit workspace-admin on every workspace in their org.

| Action | Allowed for |
|---|---|
| `org.read` | any org member |
| `org.update` | org owner, admin |
| `org.delete` | org owner |
| `org.members.read` | any org member |
| `org.members.manage` (add/remove/change role; only owners may grant or revoke `owner`) | org owner, admin |
| `workspace.create` | org owner, admin |
| `workspace.read` | workspace member (any role), org owner/admin |
| `workspace.update` | workspace admin, org owner/admin |
| `workspace.delete` | workspace admin, org owner/admin |
| `workspace.members.manage` | workspace admin, org owner/admin |
| `document.read` / `document.write` / `document.manage` | role-derived (viewer: read; editor: read and write; admin: all), **plus** active resource grants. Used from M02 on. |

```python
# app/modules/authorization/policy.py (M01.03.01): pure, no I/O
class Action(StrEnum): ...                 # the values above
@dataclass(frozen=True)
class SubjectContext:                      # loaded by the caller from PostgreSQL
    user_id: uuid.UUID
    org_role: OrgRole | None               # None = not a member
    workspace_role: WorkspaceRole | None
    grants: frozenset[GrantPermission] = frozenset()   # active grants on the target resource
@dataclass(frozen=True)
class Decision: allowed: bool; reason: str  # stable codes: allowed_by_org_role | allowed_by_workspace_role | allowed_by_grant | not_org_member | not_workspace_member | insufficient_role | unknown_action (DC-013)
def decide(subject: SubjectContext, action: Action) -> Decision
```

- **Deny by default.** Unknown action → deny.
- HTTP mapping for routes: if the caller is **not a member** of the org/workspace in the path → **404** (don't reveal
  existence); a member lacking the permission → **403** `{"detail": "forbidden"}`. Every denial writes an audit event with `outcome=denied`.
- Grants (M01.03.03, `app/modules/authorization/grants.py`): `grant()`, `revoke()` (idempotent, records `revoked_by`),
  `active_permissions(resource) -> frozenset[GrantPermission]` (honours expiry at evaluation time), and permission
  implication `manage ⊃ write ⊃ read`. Expired or revoked grants never authorize.

## 4. REST API (M01.02.02, M01.02.04): JSON, `/v1` prefix, all routes require auth

| Method and path | Action | Success | Body / response |
|---|---|---|---|
| `GET /v1/me` | — | 200 | `{"id","email","display_name"}` |
| `GET /v1/orgs` | (lists the caller's memberships) | 200 | `{"items":[OrgSummary]}`, where `OrgSummary = {"id","slug","name","role"}` |
| `POST /v1/orgs` | any authenticated user | 201 | in `{"slug","name"}`; out `Org = {"id","slug","name","created_at","role":"owner"}`; creator becomes owner atomically; 409 `slug_taken` |
| `GET /v1/orgs/{org_id}` | `org.read` | 200 | `Org` |
| `PATCH /v1/orgs/{org_id}` | `org.update` | 200 | in `{"name"?}` |
| `DELETE /v1/orgs/{org_id}` | `org.delete` | 204 | soft delete |
| `GET /v1/orgs/{org_id}/members` | `org.members.read` | 200 | `{"items":[{"user_id","email","display_name","role"}]}` |
| `PUT /v1/orgs/{org_id}/members/{user_id}` | `org.members.manage` | 200 | in `{"role"}`; the user must already exist (invites come later); 409 `last_owner` when demoting the last owner |
| `DELETE /v1/orgs/{org_id}/members/{user_id}` | `org.members.manage` | 204 | 409 `last_owner` |
| `GET /v1/orgs/{org_id}/workspaces` | `org.read` (filtered to the workspaces the caller may read) | 200 | `{"items":[Workspace]}`, where `Workspace = {"id","org_id","slug","name","created_at","role"}` |
| `POST /v1/orgs/{org_id}/workspaces` | `workspace.create` | 201 | in `{"slug","name"}`; the creator becomes workspace admin; 409 `slug_taken` |
| `GET /v1/orgs/{org_id}/workspaces/{workspace_id}` | `workspace.read` | 200 | `Workspace` |
| `PATCH …/workspaces/{workspace_id}` | `workspace.update` | 200 | in `{"name"?}` |
| `DELETE …/workspaces/{workspace_id}` | `workspace.delete` | 204 | soft delete |
| `PUT …/workspaces/{workspace_id}/members/{user_id}` | `workspace.members.manage` | 200 | in `{"role"}`; 422 `not_org_member` |
| `DELETE …/workspaces/{workspace_id}/members/{user_id}` | `workspace.members.manage` | 204 | |

- Errors are `{"detail": "<stable_code>"}`. Pagination is `?limit=` (default 50, max 200) and `&cursor=` (opaque) on list routes.
- Soft-deleted orgs and workspaces behave as **404**.
- The web app reaches these only through its own server (BFF). The browser never holds the bearer token.

## 5. Audit (M01.03.04, `app/modules/audit/`)

```python
def record_event(session, *, event_type: str, actor_user_id: uuid.UUID | None, org_id: uuid.UUID | None,
                 target_type: str, target_id: str, outcome: Literal["success","denied","error"],
                 request_id: str | None, metadata: Mapping[str, Any] | None = None) -> None
```
- Events are written **in the same transaction** as the change they describe. A denied action is written in its own transaction, so a rollback can't lose it.
- Event types (M01): `org.created`, `org.updated`, `org.deleted`, `org.member.added`, `org.member.role_changed`,
  `org.member.removed`, `workspace.created`, `workspace.updated`, `workspace.deleted`, `workspace.member.added`,
  `workspace.member.role_changed`, `workspace.member.removed`, `grant.created`, `grant.revoked`, `authz.denied`, `user.provisioned`.
- Redaction: `metadata` passes through `redact()`. Keys matching `/(pass|secret|token|key|authorization|cookie)/i` are
  replaced with `"[REDACTED]"`, values are truncated to 512 chars, and emails are kept (they are needed for membership audit).
  Never store raw tokens or request bodies.

## 6. Web session (M01.01.02, M01.02.03, M01.02.05)

- Next.js server routes `/api/auth/login`, `/api/auth/callback` and `/api/auth/logout` run the authorization-code + PKCE
  flow against Dex (browser → `OIDC_ISSUER_URL`, server → `OIDC_INTERNAL_URL` for token exchange). The scope is
  `openid email profile offline_access audience:server:client_id:<OIDC_AUDIENCE>`.
- The session is an **HttpOnly, SameSite=Lax, Secure-when-https** cookie holding an encrypted server-side session reference
  (tokens are never readable by browser JS). `state`, `nonce` and the PKCE verifier are verified. Logout clears the session (and revokes the refresh token where supported).
- The web server proxies `/api/v1/*` → API with the bearer token (BFF), so the browser never sees it.
- Org and workspace scope live in the URL: `/o/{orgSlug}/w/{workspaceSlug}/…`. Switching org clears the TanStack Query cache (ADR-044/047).

## 7. As implemented (M01.02.02 / M01.02.04, DC-018/DC-019)

The REST API follows §4. These details were fixed during implementation and are binding for later milestones.

- **Lists:** `{"items": [...], "next_cursor": string|null}`; `?limit=1..200` (default 50; outside the range is 422, not clamped) and an opaque `cursor`. A null cursor is the last page.
- **Workspace `role`** is the caller's *explicit* workspace role (`admin|editor|viewer`), always present as a key, and `null` for an org owner/admin without a workspace membership (they still see and manage every workspace).
- **Slugs** (orgs and workspaces) must match `^[a-z0-9][a-z0-9-]{1,62}$` at the API. Anything else, including upper-case, is **422 `invalid_request`**. `409 slug_taken` means a real collision only. Soft-deleted slugs stay reserved.
- **Error codes beyond §4:** 404 `user_not_found` (PUT org member for an unknown user id); 409 `conflict` (two concurrent requests adding the same membership); 422 `invalid_cursor`; 422 `{"detail": "invalid_request", "errors": [{"loc","msg","type"}]}` for bad ids, bodies, roles, limits and unknown fields (request models forbid extra fields; input values are never echoed).
- **Behaviours:** PUT of a member's current role returns 200 and writes no audit row; a PATCH with no change writes none either; DELETE of a non-existent membership is 404; the request id is a safe `X-Request-ID` header (up to 128 characters of `[A-Za-z0-9._:-]`) or a fresh uuid4.
- **Authorization and audit:** a denial is a 404 `not_found` when concealed (non-member, or an org member without any workspace role) and a 403 `forbidden` otherwise, and is audited as `authz.denied` with `{action, reason}` in its own transaction. Every successful mutation writes its §5 event in the same transaction; the actor is always the principal.
- **Last-owner race:** the deferred owner trigger alone cannot stop two concurrent owners demoting each other under READ COMMITTED, so the member PUT/DELETE routes lock the org row with `SELECT … FOR UPDATE`. A database-level guarantee is a follow-up for M08.
- **Known accepted risk:** `user_not_found` lets an org owner/admin learn whether a user id exists. Members are added by user id in M01. M08 should replace this with invitations by email.

### Pre-approve by email (migration 0011)

`POST|GET /v1/orgs/{o}/members/preapprovals` and `DELETE .../preapprovals/{id}` (`org.members.manage`; only owners grant or withdraw `owner`). A person who has never signed in is added by email: if exactly one user with that **verified** email exists the membership is created at once (`status: member_added`), otherwise a pending row in `org_member_preapprovals` (FORCE RLS by org) is created (`status: pending`, no email is sent). `users.email_verified` mirrors the OIDC `email_verified` claim on each sign-in; the provisioner calls the `preapproval_consume(user_id)` definer only for a verified email, which turns pending rows of live orgs into memberships (`org.member.added`, metadata `via: preapproval`) and stamps `consumed_at`. An unverified email never matches in either direction. Known accepted risk: the `member_added` vs `pending` answer tells an org admin whether a verified account with that email exists.
