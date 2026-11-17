# AI Catalogs: setup and reference

The technical side of AI Catalogs: running it, every setting, the command line, the editor's
security model, operations and backups. The [README](../README.md) is the short tour;
[PLAN.md](../PLAN.md) has the design and its history.

## What it does

- The curated addon: `<base>/c/<token>/manifest.json` lists every enabled catalog that has a
  list, and each catalog URL returns its whole list in one response (`skip > 0` is always
  empty; see [PLAN.md](../PLAN.md) section 9 for why).
- Catalog definitions in `catalogs.d/*.yaml` (see [catalogs.d/README.md](../catalogs.d/README.md)
  and [docs/catalog-definition.example.yaml](catalog-definition.example.yaml)): a
  free-form prompt plus optional limits, each rendered into the prompt and enforced in code.
- The refresh pipeline: generate with overgeneration and top-ups, resolve and gate every
  title on TMDB, never repeat the last `history_window_runs` lists, apply caps and quotas,
  and publish exactly `target_count` items, or keep the previous list (PLAN.md section 5).
- `python -m app.curation load-fixture PATH` publishes a list from a JSON file
  ([fixtures/test_catalog_100.json](../fixtures/test_catalog_100.json) is 100 test films).
- `GET /healthz`
- The worker: applies `catalogs.d` on start and whenever a file changes, refreshes each catalog
  on its `schedule` (cron + timezone), catches up at start (a scheduled catalog with no list,
  or one older than its interval, gets one run), recovers runs a crashed process left behind,
  takes a nightly backup, and stops cleanly on `docker compose stop`.
- `/ops`: webhooks to queue a refresh or dry run, and a status page (below).

## Catalogs and commands

Run these inside the worker container (`docker compose exec worker python -m app.curation ...`):

| Command | What it does |
|---|---|
| `validate` | Check every definition file; changes nothing |
| `apply` | Accept the definition files into the database (exclusion names are resolved on TMDB automatically) |
| `run ID [--dry-run]` | Apply, then refresh one catalog now (`--dry-run`: everything except publishing) |
| `rollback ID SNAPSHOT` | Serve an earlier list again |
| `history clear ID` | Let previously shown titles appear again |
| `status [--json]` | Catalogs, active lists, recent runs, next runs, budget, worker heartbeat |
| `status --check` | Exit 0 if this container's worker is alive (the worker's health check) |
| `backup PATH` | Online copy of the database, safe while everything runs |

A run that cannot deliver `target_count` qualifying, unrepeated films keeps the previous list
(`history_mode` and `on_shortfall` in the definition change that). Model calls are limited per
run (`max_model_calls_per_run`, default 8) and per day across all catalogs
(`MAX_MODEL_CALLS_PER_DAY`, default 40).

## Schedules

A definition's `schedule` is a five-field cron expression on the catalog's local time:

```yaml
schedule:
  cron: "0 6 * * *"
  timezone: "Europe/Berlin"   # default UTC
```

Daylight saving time: a time that does not exist on the spring-forward day (02:30 when clocks
jump from 02:00 to 03:00) runs after the jump, shifted by the gap (03:30); a time that occurs
twice on the autumn day runs once, at the first occurrence. The next run time is stored, so a
restart neither skips nor repeats a run; runs missed while the worker was down add up to one
run. A catalog without a schedule refreshes only when asked (CLI or `/ops`). Each refresh
still counts against `max_runs_per_day` and the daily model-call budget.

## Browser editor: `/configure`

Off by default. Turn it on with `ENABLE_EDITOR=true` and pick how it signs you in:

- **Its own password** (`EDITOR_AUTH=password`, the default): also set `SESSION_SECRET` (at
  least 32 bytes) and `ADMIN_PASSWORD` (at least 12 characters); with either missing or too
  short, the web service does not start.
- **Your reverse proxy's login** (`EDITOR_AUTH=proxy`), e.g. Authelia behind Traefik
  forward-auth: no password of its own. The editor trusts the user name the proxy passes in
  `EDITOR_PROXY_USER_HEADER` (default `Remote-User`, which Authelia sets) and refuses any request
  without it, so it stays locked if the proxy rule is ever removed. Traefik's forward-auth deletes
  a client-sent copy of that header, so it cannot be forged through the proxy; make sure the
  proxy requires its login for `/configure` and everything under it, and publish no port. Anything
  else that can reach the container directly (another container on the same Docker network) is
  trusted as much as the proxy, because it could send the header itself. `SESSION_SECRET` is optional here;
  without it a random key is kept next to the database (`editor-csrf.key`) for the CSRF tokens.

Then open `https://<your server>/` (it redirects to `/configure/`). `/configure` is the usual
Stremio addon settings path, so proxy rules that protect addon settings pages cover the editor.

- **Addon URL**: the catalog list shows the manifest URL to paste into AIOMetadata, with a copy
  button (it contains `ADDON_ACCESS_TOKEN`; this is the only page that shows it).

- **Edit** a catalog in a form generated from the definition fields (sliders for bounded
  limits, an empty box means "off"), or as raw YAML. **Validate** runs the same checks as
  `python -m app.curation validate` and shows the prompt rendered for a sample day; **Save**
  makes the definition live at once (a refresh in progress ends `superseded` and is queued again).
- **Files vs editor.** A catalog from `catalogs.d` is read-only in the editor until you press
  **Move to editor**. From then on the editor owns it: the file is ignored (delete or rename it)
  and its removal does not remove the catalog. The move is one-way; to go back, export the YAML,
  delete the catalog in the editor, then put the file back. New catalogs can be created in the
  editor directly. **Export YAML** downloads a definition (comments are not kept).
- **Dry run** queues a dry run of the saved definition for the worker and shows its stage counts
  and reject reasons when it ends. It spends model calls but publishes nothing. To tune a new
  catalog without it going live on a schedule: save it without `schedule`, dry-run until the
  counts look right, then add the schedule.
- **Rollback**: the catalog page lists the published lists; roll back to any that still has its
  payload and was not revoked.

Security (password mode): one password, a signed HttpOnly SameSite=Strict cookie (Secure when `PUBLIC_BASE_URL`
is https) that lasts 12 hours, a CSRF token on every form, no CORS. Logging out clears the cookie
in that browser; changing `SESSION_SECRET` ends every session. After 5 failed logins from one
address (or 20 in all) within 15 minutes, logins are refused until the window passes. Behind a
reverse proxy every request comes from the proxy unless `FORWARDED_ALLOW_IPS` names it, and then
someone guessing passwords can keep the login locked for you too, so set it and preferably
restrict `/configure` at the proxy (IP allow-list, VPN or its login). In both modes every form
carries a CSRF token, nothing has CORS, and forms are refused when the browser's `Origin`
matches neither the `Host` header nor `PUBLIC_BASE_URL`; behind a proxy that rewrites `Host`,
set `PUBLIC_BASE_URL` or every form (login included) answers 403.

## Operations: `/ops`

Set `OPS_TOKEN` in `.env` to enable it (unset, every `/ops` path answers 404). Every request
needs the header `Authorization: Bearer <OPS_TOKEN>`; the token is never accepted in a URL.

```bash
curl -X POST -H "Authorization: Bearer $OPS_TOKEN" https://catalogs.example.org/ops/catalogs/my_list/refresh
```

| Request | Result |
|---|---|
| `POST /ops/catalogs/<id>/refresh` | Queues a refresh: 202, or 409 (a job is already queued), 429 (`max_runs_per_day` reached), 404 |
| `POST /ops/catalogs/<id>/dry-run` | Queues a dry run (everything except publishing) |
| `GET /ops/status` | JSON: per catalog the active list's age and size, recent runs with stage counts and reject reasons, definition errors, next run; model calls used today; worker heartbeat; latest backup |
| `GET /ops/status` with `Accept: text/html` (or `?format=html`) | The same as a small HTML page, e.g. behind a reverse proxy that adds the header |

The webhooks only queue work; the worker runs it. `/ops` sends no CORS headers, so browsers on
other sites cannot read it.

## Backups

The worker writes `nightly-<time>.sqlite` once a day and the database writes
`pre-migration-v<N>-<time>.sqlite` before every schema upgrade, both into `BACKUP_DIR` (default
`/data/backups` on the volume); the newest 7 of each kind are kept. For a copy outside the
volume:

```bash
docker compose exec worker python -m app.curation backup /data/backups/manual.sqlite
```

```bash
docker compose cp worker:/data/backups/manual.sqlite ./ai-catalogs-backup.sqlite
```

Never copy the live `.sqlite` file itself: recent changes live in its `-wal` file. To restore:
`docker compose stop`, replace `/data/ai-catalogs.sqlite` with the backup and delete
`ai-catalogs.sqlite-wal` and `-shm` next to it, then `docker compose start`.

What to keep where: `catalogs.d/` is configuration (keep it in your own git repository or back
it up as files); the database is the full state (published lists, history, runs, budget).

## Add it to AIOMetadata

Import `<PUBLIC_BASE_URL>/c/<ADDON_ACCESS_TOKEN>/manifest.json` (or `/c/public/...` without a
token) in AIOMetadata's Custom Manifest Integration and set the catalog's cache TTL to 1-2
hours, so a new daily list shows up within that time instead of up to 24 hours later.

The manifest id, catalog ids, token and base URL are frozen into AIOMetadata's config at
import; changing any of them means importing again. A catalog only appears in the manifest
once it has a list, so it cannot be imported empty.

[docs/aiometadata-integration.md](aiometadata-integration.md) is the end-to-end check
against a real AIOMetadata.

## Run

```bash
cp .env.example .env
```

Fill in `.env`, then:

```bash
docker compose up -d --build
```

The web service listens on port 8000 (`PORT` in `.env` changes the published port).

## Configuration

All settings are environment variables; [.env.example](../.env.example) lists them. API keys are
read from the environment only and are never shown on a page. Error logs record the type of
a failure and its HTTP status, not the provider's message, which can quote a key.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | OpenRouter (default) or another OpenAI-compatible API |
| `TMDB_API_KEY` | TMDB v4 read access token |
| `MDBLIST_API_KEY` | Optional. MDBList API key, used only by catalogs with `exclude_watched: true` (skip films you have watched) |
| `PUBLIC_BASE_URL` | Externally reachable URL of this server |
| `ADDON_ACCESS_TOKEN` | Secret path segment of the addon URLs (16+ characters); unset means `public` |
| `OPS_TOKEN` | Bearer token for `/ops`; unset disables it |
| `ENABLE_EDITOR`, `EDITOR_AUTH`, `EDITOR_PROXY_USER_HEADER`, `SESSION_SECRET`, `ADMIN_PASSWORD` | The browser editor at `/configure` (off by default; see below) |
| `BACKUP_DIR`, `BACKUP_KEEP` | Automatic backups; default `/data/backups`, 7 of each kind |
| `DATABASE_PATH` | SQLite file; default `/data/ai-catalogs.sqlite` on the compose volume, which must be a local disk |
| `PUID`, `PGID` | User and group the services run as (default `1000`). On start the container gives `/data` to them, so a bind-mounted data folder needs no `chown`; commands run with `docker compose exec` drop to them too |

## Development

```bash
python3.11 -m venv venv && ./venv/bin/pip install -r requirements-dev.txt
```

```bash
./venv/bin/pytest
```

To run the web server outside Docker, point `DATABASE_PATH` somewhere writable, for example
`DATABASE_PATH=./data/dev.sqlite`.

`sh scripts/compose_smoke.sh` builds the image and checks both services start, serve and stop
cleanly, that the worker holds the lease, turns healthy and releases the lease when stopped
(needs Docker; CI runs it).

## Licence

MIT; see [LICENSE](../LICENSE).
