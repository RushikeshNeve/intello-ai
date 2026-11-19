# Manual test guide (M01: sign-in, organizations, workspaces)

Everything runs locally; no cloud account is needed. These steps were checked against the real stack on 2026-11-18
(cold start from nothing: 27 s, exit code 0). Test accounts here are **local test values only**.

## 1. Start, stop, reset

```bash
cd "D:/Career/Projects/Universal Knowlege Hub"
cp infrastructure/env/.env.example .env        # first time only (already done if .env exists)
docker compose up -d --wait                    # about 30 s once images are built (first build: several minutes)
docker compose ps                              # every service should say "healthy"
```

| Command | Effect |
|---|---|
| `docker compose stop` | stop everything, keep your data |
| `docker compose up -d --wait` | start again |
| `docker compose down` | remove containers, keep data (volumes) |
| `docker compose down -v` | **wipe all data**: fresh users, orgs, workspaces |
| `docker compose logs -f api web dex` | follow logs |

If Docker Desktop won't start (stale socket error), run
`powershell -ExecutionPolicy Bypass -File scripts\windows\fix-docker-sockets.ps1 -Start`.

## 2. Links

> **Always use `127.0.0.1`, not `localhost`.** The login cookie and redirect are bound to `127.0.0.1:3300`;
> opening `localhost:3300` breaks sign-in.

| What | URL |
|---|---|
| **Web app (start here)** | http://127.0.0.1:3300 |
| Home after sign-in (your orgs) | http://127.0.0.1:3300/home |
| Org page / workspace page | `http://127.0.0.1:3300/o/<org-slug>` and `/o/<org-slug>/w/<workspace-slug>` |
| API documentation (Swagger, lists every route) | http://127.0.0.1:8300/docs |
| API health | http://127.0.0.1:8300/health/ready |
| Web health | http://127.0.0.1:3300/api/health |
| Login provider (Dex) discovery | http://127.0.0.1:8380/dex/.well-known/openid-configuration |
| RushDB / object store / Postgres / Redis (not used by the screens yet) | 127.0.0.1:3310 / 127.0.0.1:9300 / 127.0.0.1:5532 / 127.0.0.1:6479 |

## 3. Test accounts (Dex, test-only)

Sign in with the email and password on Dex's login page. After **Sign out**, Dex asks for credentials again, so you
can switch users in the same browser window.

| User | Email | Password | Role in this guide |
|---|---|---|---|
| alice | alice@ukos.test | `ukos-dev-alice-password` | owns "Acme" |
| bob | bob@ukos.test | `ukos-dev-bob-password` | member of "Acme" |
| mallory | mallory@ukos.test | `ukos-dev-mallory-password` | outsider (must see nothing) |

## 4. Console helper

M01 has **no screen for adding members** (the API supports it). Those steps use your signed-in browser session:
open DevTools on a page of the web app (F12, Console; if Chrome blocks pasting, type `allow pasting` first) and paste
this. **Paste it again after every page load or sign-in**, because navigation clears it:

```js
const api = async (m, p, b) => { const r = await fetch('/api/v1' + p, { method: m, headers: { 'content-type': 'application/json' }, body: b ? JSON.stringify(b) : undefined }); return [r.status, await r.json().catch(() => null)]; };
```

Then, for example, `await api('GET', '/me')` shows who you are, and `await api('GET', '/orgs')` lists your orgs.

## 5. The workflow

**Step 1. Create the users (bob first).** A user is created the first time they sign in and the app calls the API.
1. Open http://127.0.0.1:3300, click sign in, log in as **bob**. You land on `/home` with no orgs.
2. In the console (paste the helper), run `await api('GET', '/me')`. **Copy the `id`**: this is `BOB_ID`.
3. Click **Sign out**.

**Step 2. Alice builds her organization.**
1. Sign in as **alice**. `/home` is empty: create an org named `Acme` (the slug `acme` fills in by itself). You are taken to `/o/acme`.
2. Create a workspace `Lab Notes` (slug `lab-notes`). Open it: the sidebar highlights it.
3. Create a second org `Globex` from `/home`, then use the **org switcher** in the header to go between Acme and Globex.
   **Expect:** the header name changes immediately, and Globex never shows Acme's workspaces, not even for a moment.
4. Console on any Acme page: run `await api('GET', '/orgs')` and copy Acme's `id` (`ORG_ID`); run
   `await api('GET', '/orgs/ORG_ID/workspaces')` (use the real id) and copy Lab Notes' `id` (`WS_ID`).
5. Add bob: `await api('PUT', '/orgs/ORG_ID/members/BOB_ID', { role: 'member' })` → **200**, `role: "member"`.
6. Give bob the workspace: `await api('PUT', '/orgs/ORG_ID/workspaces/WS_ID/members/BOB_ID', { role: 'viewer' })` → **200**.

**Step 3. Rules alice's session must enforce.**
- Demote the only owner: `await api('PUT', '/orgs/ORG_ID/members/ALICE_ID', { role: 'member' })` (`ALICE_ID` from `/me`) → **409 `last_owner`**.
- Upper-case slug: `await api('POST', '/orgs', { slug: 'ACME', name: 'x' })` → **422 `invalid_request`**.
- Duplicate slug: `await api('POST', '/orgs', { slug: 'acme', name: 'x' })` → **409 `slug_taken`**.
- Click **Sign out**.

**Step 4. Bob (a member).** Sign in as **bob**.
- `/home` lists **Acme** with role **member** (not Globex).
- On `/o/acme` there is **no create-workspace form**; Lab Notes is listed, and opens (viewer).
- Console: `await api('PATCH', '/orgs/ORG_ID', { name: 'hijack' })` → **403 `forbidden`**.
- Console: `await api('POST', '/orgs/ORG_ID/workspaces', { slug: 'x1', name: 'x' })` → **403**.
- Sign out.

**Step 5. Mallory (an outsider).** Sign in as **mallory**.
- `/home` is empty.
- Open `http://127.0.0.1:3300/o/acme`: a calm **"not found or no access"** page, with no Acme names anywhere.
- Same for `/o/acme/w/lab-notes`.
- Console: `await api('GET', '/orgs/ORG_ID')` → **404** (the org's existence is hidden).
- Sign out.

**Step 6. Deep link through login.** While signed out, open `http://127.0.0.1:3300/o/acme/w/lab-notes`. You are sent to the
login page; sign in as alice (or bob) and you **land back on that workspace**.

**Step 7. Look at the audit trail** (everything above was recorded):

```bash
docker exec ukos-postgres-1 psql -U ukos_admin -d ukos -c "SELECT occurred_at, event_type, outcome FROM audit_events ORDER BY occurred_at"
docker exec ukos-postgres-1 psql -U ukos_admin -d ukos -c "SELECT email, created_at FROM users"
```

You should see `user.provisioned` for each user, `org.created`, `org.member.added`, `workspace.created`,
`workspace.member.added`, and `authz.denied` (`denied`) for the 403/404 attempts.

## 5b. Documents (M02): upload, search, versions, delete

Do this as **alice** in the org and workspace from step 2 (and again as bob and mallory at the end). Keep using `127.0.0.1`:
uploads go from your browser straight to the object store at `http://127.0.0.1:9300`, and its CORS rule only allows the web origin.

1. **Open the library.** On the workspace page click **Open documents** (or go to `/o/<org-slug>/w/<workspace-slug>/documents`).
   Empty state, with the upload area visible because alice may upload.
2. **Upload.** Drag in a few files, or use **Choose files**: a PDF, a `.txt`, a `.png`, and a small `.html` file.
   **Expect:** a progress bar per file, then "done", and a new row in the table (title = file name without its extension).
3. **Search and filter.** Type part of a title; pick *PDF* / *Text* / *Images* in the type filter; change the sort. **Reload the page and use
   Back/Forward:** the same view returns, because everything lives in the URL. (*Excel (.xlsx)* matches `.xlsx` only; *Unknown type* matches files whose type the
   browser couldn't identify.)
4. **Open each file in the viewer** (click the title):
   - PDF → shown inline; PNG → shown inline; `.txt` → shown as plain text.
   - `.html` → **never rendered**: you get a download card instead (that's the security rule).
5. **New version.** In the viewer click **Upload new version** and pick a different file. **Expect:** the version list shows *v2 (current)* above
   *v1 (superseded)*; selecting v1 still shows the **original** content; the title did not change.
6. **Rename**, then **Delete** (it asks you to confirm). **Expect:** the row disappears from the list, and opening the old viewer link now shows a calm
   "not available" page.
7. **Errors.** Try a file over 100 MB: you get a clear "larger than 100 MB" message *before* any upload starts. Stop the object store
   (`docker compose stop objectstore`), upload a small file, and see the retry/error state; start it again (`docker compose start objectstore`) and press **Retry**.
8. **Other users.** Add bob to the workspace as a viewer (the console commands in step 2 of the workflow above). As **bob**: the document list and viewer work and
   downloads work, but there is no upload area and no Rename/Delete. As **mallory**: the not-found page, nothing listed.
9. **Look under the hood:**

```bash
docker exec ukos-postgres-1 psql -U ukos_admin -d ukos -c "SELECT d.title, v.version_number, v.original_filename, v.size_bytes, left(v.sha256, 12) AS sha FROM documents d JOIN document_versions v ON v.document_id = d.id ORDER BY d.created_at, v.version_number"
docker exec ukos-postgres-1 psql -U ukos_admin -d ukos -c "SELECT occurred_at, event_type, outcome FROM audit_events WHERE event_type LIKE 'document.%' ORDER BY occurred_at"
```

**Please tell me what you see for these two, they are the only parts no automated test could check in a real browser:**
(a) does the **PDF** appear inline in the viewer? (b) does the **`.txt`** preview show its text? If either fails, "Download" still works, and I'll fix the preview.

## 6. Calling the API directly (optional)

Get a token with Dex's password grant, then call any route from the `/docs` list:

```bash
TOKEN=$(curl -s -u ukos-web:change-me-oidc-client-secret http://127.0.0.1:8380/dex/token \
  -d grant_type=password -d username=alice@ukos.test -d password=ukos-dev-alice-password \
  -d scope="openid email profile audience:server:client_id:ukos-api" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8300/v1/orgs
```

The Swagger page at `/docs` lists the routes and request shapes, but has no "Authorize" button; use curl or the console helper.
Tokens last 15 minutes.

## 7. Not built yet (so you don't look for it)

No screen to add or remove members (use the console), no turning uploaded files into searchable knowledge (M03), no search or chat (M04), no
dark-mode switch (M07). Sessions live in the web server's memory, so restarting the `web` container signs everyone out.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Sign-in loops or fails | You opened `localhost` instead of `127.0.0.1`. Use `http://127.0.0.1:3300`. |
| "Temporarily unavailable" on `/home` | The API isn't ready: `docker compose ps`, `docker compose logs api`. |
| Port already in use on start | Another project uses it. Change the `*_HOST_PORT` value in `.env` (and keep `WEB_PUBLIC_URL` / `OIDC_*` in step). |
| 401 from the console helper | Your session expired (12 h) or you signed out: sign in again and re-paste the helper. |
| `docker compose` can't connect | Start Docker Desktop (see §1 if it crashes). |
