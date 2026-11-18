# Plan: Curated daily AI catalogs on a fork of stremio-ai-companion

Revision 5 (after Codex review rounds 1-3 of the plan, and a review of the milestone 1
implementation). Milestone 1 is implemented; see section 16 for its status.

## 0. Goal and fixed decisions

Self-hosted Stremio catalog addon: a tool for defining any number of catalogs, each a
daily-rotating, LLM-generated movie list with its own prompt and filters, served through a
manifest URL imported into AIOMetadata. The code carries no particular catalog's prompt,
preferences or exclusion lists; those live only in the operator's own catalog definitions
(and secrets only in `.env`). The owner has already decided:

- Base: fork of `willtho89/stremio-ai-companion` (Python, FastAPI, MIT; authors gave permission).
- Cherry-pick OpenRouter ideas from `itcon-pty-au/stremio-ai-search` (Node, MIT).
- Consumer: `cedya77/aiometadata` ("Custom Manifest Integration"). Its own AI catalog
  builder does not do what the owner needs, hence this addon.
- Required features: server-side catalog/prompt config, scheduled background refresh,
  rolling history exclusion, hard TMDB gatekeeper in code, OpenRouter options
  (`:online`, temperature, structured JSON).

Local read-only clones for reference:
- companion: `<SCRATCH>/companion/repo` (HEAD a49d450, 2025-12-06, "Archive repository")
- search: `<SCRATCH>/search/repo` (HEAD a51b668, 2026-11-07)
- aiometadata: `<SCRATCH>/aiometadata/repo` (HEAD 491f128, 2026-11-16)

## 1. What the code really does (verified by reading it)

These differ from the assumptions in the original spec and shape the plan.

Companion:
1. Upstream is archived. The fork is the maintained copy; dependency pins are ours to update.
2. No background generation exists. `app/api/stremio.py::_cached_catalog` calls the LLM
   inside the Stremio request; with Redis it appends 10 more items per request up to 100.
3. Pagination is not real: `skip` is never used to slice, and feed catalogs declare no
   `extra: skip`.
4. Prompts are not server-side: `app/services/__init__.py::CATALOG_PROMPTS` is a hardcoded
   dict; the manifest URL is `/config/{encrypted blob with API keys}/manifest.json`.
5. Redis is optional and off in the shipped compose file (`REDIS_HOST` never set ->
   per-process in-memory LRU). `RedisBackend.set` always uses `setex`; `clear` is
   `flushdb`; no Redis volume.
6. `entrypoint.sh` starts one uvicorn worker per CPU core (max 8); lifespan code runs per worker.
7. `LLMService` already does structured output (`chat.completions.parse` with a Pydantic
   model, JSON-mode fallback) but is wired to a search system prompt, temperature 0.4,
   max_tokens 5000, timeout 30s, and swallows all errors into `[]`.
8. `TMDBService.search_movie` returns `results[0]` unchecked (the "Modern" mismatch
   class); every request opens a new `httpx.AsyncClient`; searches fire unbounded.
9. `catalog_id.rstrip("_movie")` strips a character set, not a suffix; unknown ids fall
   back to `trending`.
10. Meta ids are IMDb ids with an `aic:<tmdb id>` fallback that nothing can resolve.
11. **Secret leak:** the unauthenticated `/configure` page renders `settings.OPENAI_API_KEY`,
    `settings.TMDB_READ_ACCESS_TOKEN` and `settings.RPDB_API_KEY` into input `value`
    attributes (`templates/configure.html` lines 219, 259, 298; `app/api/web.py` passes
    `settings` to the templates). Anyone who can reach the server can read the env keys.
12. `app/services/rpdb.py` builds poster URLs containing the RPDB API key.

stremio-ai-search: sends a plain prompt and parses text lines (no JSON schema), clamps
temperature to 0..1, treats `:online` as part of the model string. Worth porting from
`utils/aiProvider.js`: empty reply with `finish_reason == "length"` detection, default
output budget of 8000, configurable timeout, OpenRouter-key / base-URL mismatch hint.

AIOMetadata (custom manifest path):
- Import (`configure/src/components/sections/CustomManifestIntegration.tsx`): the browser
  fetches the manifest (CORS needed); internal Docker URLs go through `/api/proxy-manifest`.
  Needs `id`, `name`, `catalogs[{type,id,name}]`.
- Catalog URL (`configure/src/utils/catalogUtils.ts::createCustomManifestCatalog`):
  `manifestUrl.replace('/manifest.json','') + /catalog/{type}/{encodeURIComponent(id)}.json`,
  frozen into its config at import. Internal id `custom.{manifest.id}.{type}.{catalog.id}`
  with non-alphanumerics replaced by `_`.
- Fetch (`addon/utils/stremthru.js::fetchStremThruCatalog`,
  `addon/lib/getCatalog.ts::getExternalAddonCatalog`): requests `.../{id}/skip={n}.json`
  only for n > 0; advances its upstream offset by the number of items returned; with Redis
  it keeps a cursor, and falls back to the raw Stremio skip when the cursor is missing or
  mismatched; stops on empty `metas`. Each upstream offset is cached separately for the
  catalog's `cacheTTL` (set at import, 0..7 days, default `CATALOG_TTL` = 24h) with error
  caching on; failures become an empty list.
- Items (`parseStremThruItems`): `tt...` ids are treated as IMDb and AIOMetadata rebuilds
  metadata and posters itself.

## 2. Scope: v1 and later

Codex suggested a smaller first version; adopted. The owner's spec allows "JSON/YAML or
... via the web UI" for configuration.

**v1 (this plan, milestones 1-7):** YAML catalog definitions, SQLite state, separate
worker service, curated serving routes, TMDB gatekeeper, history, token-protected
refresh webhook + read-only status page, CLI.

**v2 (milestone 8, after v1 has run for a week):** browser prompt editor, dry-run UI,
snapshot rollback UI. Designed in section 10 so v1 does not block it.

Dropped: companion-side RPDB for curated catalogs (AIOMetadata applies its own posters,
and RPDB URLs would leak the key).

## 3. Architecture

```
 AIOMetadata --> web service (uvicorn, N workers)      worker service (1 process)
                 curated read-only routes               scheduler + refresh pipeline
                 refresh webhook, status page  -------> (job rows in SQLite)
                          \                             /
                           SQLite (WAL) on a shared named volume
```

- Two Compose services built from the same image: `web` and `worker`
  (`python -m app.worker`). No process supervision inside one container.
- Durable state (snapshots, history, runs, jobs, budget ledger) in SQLite. Deviation from
  the spec's "Redis list/set": this data must survive restarts with no TTL and needs
  transactions. Redis is not needed by curated catalogs at all.
- Catalog definitions are authored in `catalogs.d/*.yaml` (mounted read-only into both
  services) but **SQLite holds the authoritative copy**. A filesystem edit and a database
  commit cannot be made atomic, so a definition only takes effect when it is *accepted*:
  one loader (the worker, at start and on SIGHUP / file-mtime change; or the CLI `apply`
  command) validates the YAML with Pydantic and, in one transaction, writes it to
  `catalog_defs` with its `config_rev` (sha256 of the canonical YAML). Invalid YAML is
  rejected, the accepted definition stays in force, and the error is recorded for the
  status page. A removed file marks the catalog `removed`. Web workers, the pipeline and
  the commit check all read `catalog_defs`, never the files, so "current revision" has one
  meaning and is checked transactionally. (This is also the storage the milestone 8 editor
  writes to.) Tests: a worker with stale in-memory state, file deletion, invalid
  replacement YAML, and a revision accepted immediately before a commit.
- Web workers never call the LLM for curated catalogs.
- **Legacy surface is off by default.** `ENABLE_LEGACY=false` means the upstream routers
  (`/configure`, `/save-config`, `/config/{blob}/...`, preview, AI search, feed catalogs)
  are not mounted. When enabled they are for trusted networks only, and independent of
  the flag the templates and `web.py` are changed so env secrets are never rendered
  (fields left blank; server-side fallback to env values on empty submit, which
  `Config.__init__` already does for missing keys). Upstream tests are updated where they
  assert the old behaviour. Regression test: boot with sentinel values for every secret
  and assert no unauthenticated route's response body or headers contain them
  (walk all GET routes, both flag settings).

## 4. Data model (SQLite, `app/store/`, migrations via numbered SQL files + `schema_version`)

- `catalog_defs(catalog_id PRIMARY KEY, config_rev, definition_json, state, accepted_at,
  last_error, epoch)`; state `enabled | disabled | removed`. `epoch` is bumped by every
  maintenance operation (rollback, history clear) and by every accepted revision.
- `snapshots(id, catalog_id, created_at, run_id, config_rev, status, metas_json, item_count)`
  with a partial unique index `UNIQUE(catalog_id) WHERE status='active'`. Payload retention:
  keep the last 10 per catalog. Pruning a snapshot never touches history.
- `activations(id INTEGER PRIMARY KEY AUTOINCREMENT, catalog_id, snapshot_id, run_id,
  activated_at, revoked_at)`; one row each time a run publishes. `id` order is the
  activation order ("last N runs" means the last N non-revoked activations), independent
  of any snapshot's `created_at`.
- `history(activation_id, catalog_id, tmdb_id, imdb_id, title, year)`; one row per item per
  activation, referencing `activations` (not `snapshots`), with no cascade from snapshot
  pruning. **Activation and history rows are never pruned**; only snapshot payloads are
  (10 per catalog). At 100 compact rows per daily run this is about 36,500 rows per catalog
  per year, which SQLite handles without effort, and it means any window size, any later
  increase of `history_window_runs`, and the window of any rollback target are always
  backed by data. (`history clear` revokes; it does not delete.)
- `runs(id, job_id, catalog_id, config_rev, epoch, seed, trigger, dry_run, started_at,
  finished_at, status, stage_counts_json, reject_reasons_json, prompt_hash, model_calls,
  tokens_in, tokens_out, relaxed_window, repeats, error_category)`. Status:
  `ok | failed | superseded | abandoned`. `job_id` ties a run to the exact job attempt, so
  recovery marks the right run. `error_category` is a controlled value (e.g.
  `provider_auth`, `provider_rate_limit`, `provider_http_5xx`, `timeout`, `shortfall`,
  `budget`, `config`) plus an HTTP status; provider message text is never stored or logged
  (it can quote keys), so the status page and backups cannot disclose it.
- `jobs(id, catalog_id, kind, state, requested_at, not_before, attempt, claimed_by,
  claim_gen, heartbeat_at)` with partial unique index
  `UNIQUE(catalog_id) WHERE state IN ('pending','running')`.
- `schedule(catalog_id, next_run_at)`; persisted so restarts neither skip nor double-run.
- `lease(name PRIMARY KEY, owner, generation, expires_at)`; single row `worker`.
- `llm_ledger(id, day, catalog_id, run_id, at, kind, model, tokens_in, tokens_out)`; one
  row per outbound model attempt, written **before** the call (reservation) and updated after.
- `tmdb_cache(key, tmdb_id, details_json, fetched_at)`.
- `watched_cache(key, fetched_at, item_count, payload_json)` (migration 4, section 20): the
  owner's watched films from MDBList, one row, 6 h TTL.

Transactions and recovery:
- **Lease + fencing:** acquisition/takeover is
  `UPDATE lease SET owner=?, generation=generation+1, expires_at=? WHERE expires_at < now`
  inside `BEGIN IMMEDIATE` (a restarted worker also waits out or takes over its own old
  lease this way, so every process lifetime has a distinct generation). The worker
  remembers its `generation`. **Renewal** only extends expiry:
  `UPDATE lease SET expires_at=? WHERE owner=? AND generation=?`; zero rows updated means
  ownership is lost.
- **Heartbeat task:** an independent asyncio task, started with the worker and running
  for its whole life (including during a 20-minute pipeline run), renews the lease and
  the current job's `heartbeat_at` every 20s (lease TTL 90s). All pipeline I/O is async
  and SQLite calls run in a thread executor so the loop is never blocked. If renewal
  reports lost ownership (or fails for longer than the TTL), the task cancels the
  in-flight run; the run cannot commit anyway because of the fencing check.
- **Job claim:** `BEGIN IMMEDIATE; UPDATE jobs SET state='running', claimed_by, claim_gen,
  heartbeat_at WHERE id=(SELECT id ... state='pending' AND not_before<=now ORDER BY requested_at LIMIT 1)`.
- **Abandoned jobs:** on start and every minute, any `running` job whose `claim_gen` is
  older than the current lease generation, or whose heartbeat is older than 5 minutes, is
  set back to `pending` with `attempt+1` and backoff (`not_before`), or `failed` after 3
  attempts; its run row becomes `abandoned`. This frees the one-open-job index.
- **Commit (single `BEGIN IMMEDIATE` transaction):** verify `lease.generation` equals
  the worker's remembered generation and the lease is unexpired; verify in `catalog_defs`
  that the catalog is `enabled` and its `config_rev` **and `epoch`** equal the run's; then
  mark old snapshot `superseded`, insert new `active`, insert the `activations` row and its
  history rows, prune old snapshot payloads (never history), set job `done`.
- **Every terminal outcome finalizes its job, in one transaction, fenced.** All
  job-state writes use `... WHERE id=? AND claim_gen=? AND state='running'` and are
  preceded by the lease-generation check, so a worker that lost ownership cannot touch a
  job that another worker has since reclaimed (zero rows -> it just stops). Outcomes:
  - success: commit as above, job `done`, run `ok`.
  - shortfall / non-transient error: job `failed`, run `failed`.
  - transient error: job back to `pending` with `attempt+1` and `not_before` (or `failed`
    after 3 attempts), run `failed`.
  - config changed, catalog disabled or removed: nothing published; job `done`
    (state detail `superseded`), run `superseded`; and, if the catalog still exists and is
    enabled, the replacement `pending` job is inserted **in the same transaction**, after
    the old job leaves the open-job index. Exactly one runnable replacement results,
    without waiting for abandoned-job recovery.
  - SIGTERM / cancelled: job back to `pending` (no attempt penalty), run `abandoned`.
  - lost lease: no writes; the new owner's abandoned-job recovery handles the job.
- **Maintenance operations (rollback, history clear; v1 CLI, v2 UI)** each run in one
  `BEGIN IMMEDIATE` transaction and bump the catalog's `epoch`. An in-flight refresh
  started under the old epoch therefore fails its commit check and ends `superseded` with
  one replacement job queued, so a rollback cannot be overwritten by a list that was
  selected against the pre-rollback history. They do not need the worker lease.
  - Rollback to snapshot S. Preconditions, otherwise the command refuses with a clear
    message and changes nothing: S still has its payload, **and S's activation is not
    revoked**. (After A -> B -> C and a rollback to A, B and C are revoked and are no longer
    rollback targets; likewise no older snapshot is eligible for rollback after
    `history clear`. To get
    such a list back the owner runs a refresh.) Effect: S becomes `active`; every
    activation after S's own gets `revoked_at` (their titles become eligible again; rows
    are kept for audit); no new activation row is written, so S's activation keeps its
    place and the active snapshot's titles are always inside the history window.
  - History clear: sets `revoked_at` on all of the catalog's activations except the one
    of the currently active snapshot, so the list being served is still excluded from the
    next refresh.
  Invariant (asserted in tests after every operation): the active snapshot's activation
  is non-revoked. Tests: a 14-run and a 30-run window after 50 publications; the window
  after a rollback to an old retained snapshot; repeated rollbacks (A -> B -> C, to A, then
  to B is refused); rollback after `history clear` (refused); increasing
  `history_window_runs` afterwards; rollback during an in-flight run.
- **Run admission:** `max_runs_per_day` and the one-open-job rule are checked in the same
  transaction that inserts the job, so CLI, webhook and scheduler cannot race past them.
- Retry times live in `jobs.not_before`, so they survive restarts.
- Tests kill the process (subprocess + SIGKILL) between claim and commit, and inside the
  commit transaction, and assert the next start recovers with the previous snapshot intact.
  Further tests: a healthy run lasting longer than the lease TTL keeps the lease and
  commits; a second worker cannot take over during it; a config change mid-run yields
  exactly one runnable replacement job immediately; a fenced-out worker's finalize and
  commit write nothing.

## 5. Refresh pipeline (`app/curation/pipeline.py`)

The pipeline keeps one candidate pool for the run; every stage is idempotent over it.

1. **Render prompt** (section 6) for the first batch.
2. **Generate** via `CuratedLLMService` (section 7): ask for
   `target_count * overgenerate_factor` (default 100 x 1.6) in batches of <= 60, each
   batch told the titles already produced this run.
3. **Normalise + dedupe** on (casefolded, punctuation-stripped title, year).
4. **Static + history pre-filter** by normalised title/year (cheap; saves TMDB calls).
   Suggestions dropped here for history are not discarded: they are kept in a
   `held_for_history` list with the activation they matched. In `relax` mode this
   pre-filter still runs, and the held list is what relaxation draws on (stage 11).
5. **Resolve on TMDB** (section 8) with validated matching.
6. **Dedupe on resolved ids**: one entry per `tmdb_id` and per `imdb_id` within the run
   (aliases and adjacent-year guesses collapse here).
7. **Gatekeeper** (section 8): hard thresholds, hard blacklist by `tmdb_id`, franchise
   denylist. Every rejection recorded with a reason.
8. **History filter** on `tmdb_id` / `imdb_id` against the last `history_window_runs`
   non-revoked activations (authoritative).
9. **Select** (deterministic given the seed): apply caps and quotas to the eligible pool —
   max 1 per `belongs_to_collection.id` (films with no collection are independent),
   optional max 2 per director, optional per-category minimums and per-decade minimums
   (section 6 says which are hard) — choosing greedily by quota need, then seeded shuffle.
   Output: `selected` (<= target_count) plus `unmet_hard` (hard quotas not satisfied).
10. **Top-up loop**: `shortfall = target_count - len(selected)`, computed **after**
    selection. While (shortfall > 0 **or** `unmet_hard` is non-empty) and budget remains
    (section 7), generate more — `max(shortfall, sum of unmet hard-quota gaps) x 3`, with
    the prompt naming the unmet decades/categories and everything already seen — then
    repeat stages 3-9 over the whole pool. Selection may swap out already-selected items
    to satisfy a hard quota.
11. **Publish decision** (explicit contract, per catalog):
    - `history_mode: strict` (default): never include an item from the history window.
    - `on_shortfall: fail` (default): the run **fails**, the previous snapshot stays
      active and no history is written, unless the final assertion holds. The assertion,
      checked in code immediately before commit: exactly `target_count` items; unique by
      `tmdb_id` and `imdb_id`; every item passes the gatekeeper and blacklists; collection
      and director caps hold; **every hard quota (decade minimums and any quota marked
      hard) is met**; no item is in the effective history window.
    - `on_shortfall: publish_partial` with `min_accept`: opt-in.
    - `history_mode: relax` : opt-in. Only after the top-up budget is spent, the effective
      window shrinks one run at a time (oldest activation first, ranked by each title's
      **most recent** activation). After each shrink, the `held_for_history` suggestions
      that are no longer inside the window go through stages 5-9 (TMDB resolution,
      resolved-id dedupe, gatekeeper, id-based history filter against the shrunken
      window, selection) together with the existing pool, so re-admitted items are
      resolved and validated against the current filters, caps and quotas; one that now
      fails a hard filter is rejected like any other. `publish_partial` relaxes only the
      count (to `min_accept`); hard quotas scale proportionally and everything else in
      the assertion still applies. `runs.relaxed_window` and `runs.repeats`
      record the effective window and repeat count. The docs state plainly that relaxing
      means "the model stopped producing new qualifying titles within the call budget",
      not "the pool is exhausted".
12. **Commit** per section 4.

Sizing note for the owner: 100 items x 7-run window needs 700 distinct qualifying films
from the model within the daily call budget; x14 needs 1,400. Strict + fail means a day
where the model cannot deliver keeps yesterday's list (visible on the status page) rather
than silently repeating. `history_window_runs` default 7; raise it once run logs show headroom.

Failure handling: errors propagate to the run, are logged in `runs`, and the job is
retried via `not_before` (5 / 15 / 45 min) for transient causes only (network, 429, 5xx,
timeout). Non-transient causes (401/402/403, schema of the catalog invalid, budget
exhausted) fail immediately with no retry.

## 6. Prompt templating and catalog definitions (`app/curation/prompt.py`, `catalogs.d/`)

- Jinja2 `SandboxedEnvironment`, `StrictUndefined`. A YAML file fails validation (and the
  old definition stays in force) unless the template renders against a sample context.
- Variables: `today`, `weekday`, `day_of_month`, `month_name`, `year`, `seed`,
  `target_count`, `request_count`, `focus`, `categories` (shuffled), `anchors` (shuffled),
  `exclusions` (rendered block), `already_this_run`, `unmet` (top-up rounds), and
  `limits` (below).
- **Limits feed the prompt as well as the gate.** The purpose of the tool is accurate
  catalogs from complex free-form prompts plus adjustable limits (sliders in the v2
  editor). Every limit a catalog sets (rating and vote floor, year range, runtime,
  languages, excluded genres, caps, quotas) is (1) rendered into a plain-language
  constraints block appended to the prompt, so the model aims inside the limits and fewer
  generations are wasted, and (2) enforced in code by the gatekeeper and selection, which
  remain the guarantee. One source of truth: both are generated from the same definition
  fields, and the template can also reference them directly (e.g. `{{ limits.year_min }}`).
- `seed` = hash(date, catalog id, per-run random nonce); it drives the category/anchor
  shuffle, the per-category "angle" hint, and the final shuffle, and is stored in `runs`.
- Prompt-side history exclusion: at most `history_prompt_limit` (default 150) most recent
  titles, in lines of <= 12 titles each starting with "DO NOT INCLUDE:". The code filter
  is the guarantee; this block only reduces wasted generations.
- Rendered prompt size is capped (default 24k chars); over the cap, the exclusion block
  is truncated first.

A catalog definition (one YAML file per catalog in `catalogs.d/`, written by the operator
and not committed with the tool) carries everything specific to that catalog. Every field
except the prompt is optional and off or neutral unless set:

| Field | Where enforced | Kind |
|---|---|---|
| `prompt` (Jinja2 template; may name examples to avoid, "taste anchors", focus) | LLM | advisory |
| `blacklist_titles` (titles, optional years) -> resolved to `blacklist_tmdb_ids` | gatekeeper | hard (code) |
| `franchise_denylist` (franchise/collection names, company names, keyword names, title regexes) -> resolved to TMDB ids | gatekeeper | hard (code), best-effort completeness |
| `languages` (original_language allow-list) | gatekeeper | hard |
| `excluded_genre_ids` | gatekeeper | hard |
| `min_runtime` (missing runtime rejects when set) | gatekeeper | hard |
| `min_vote_average`, `min_vote_count` (TMDB rating floor; a slider in the v2 editor) | gatekeeper | hard |
| `year_min`, `year_max` | gatekeeper | hard |
| `max_per_collection` | selection | hard |
| `director_cap` (costs one cached credits call per candidate) | selection | hard |
| `decade_min` | selection | hard quota |
| `exclude_watched` (the owner's watched films from MDBList; section 20) | pipeline + final assertion | hard (code) |
| `category_min` (LLM-reported `category`) | selection | soft quota: ranks selection and steers top-ups, never fails a run, because the label is the model's own claim |
| shuffled flat list | JSON schema + code shuffle | hard |

Exclusion names (`blacklist_titles`, `franchise_denylist`) are resolved to TMDB ids
automatically when a definition is applied, with the same resolver as the gatekeeper;
`scripts/resolve_blacklist.py` exposes that resolution for inspection only.

The YAML also carries: `model`, `temperature`, `max_tokens`, `timeout_s`,
`target_count` (default 100), `overgenerate_factor` (default 1.6), `schedule` (cron +
timezone), `history_window_runs` (default 7), `history_mode` (default `strict`),
`on_shortfall` (default `fail`), `max_model_calls_per_run` (default 8),
`max_runs_per_day` (default 4), `weekday_focus`, `exclude_watched` (default false) and
`watched_prompt_limit` (default 50; section 20). Today's date is injected into the
template, so prompts need no search grounding for it.

## 7. LLM layer and spend control (`app/curation/llm.py`, `app/curation/budget.py`)

- New class separate from the legacy `LLMService`; `openai.AsyncOpenAI` against
  `OPENAI_BASE_URL` (OpenRouter by default); SDK auto-retries disabled (`max_retries=0`)
  so every attempt is visible to the budget.
- Per-catalog `model` (any string incl. `:online`), `temperature` (0..2), `max_tokens`
  (default 8000, hard ceiling 16000), `timeout_s` (default 120).
- Structured output: strict JSON schema `{"movies":[{"title","year","category"}]}`.
  Downgrade to `json_object` mode, then to tolerant JSON extraction, **only** when the
  error is an identified capability error (HTTP 400/404/422 whose body mentions
  `response_format` / `json_schema` / "structured output" / "not supported", or the
  provider's no-endpoint-supports-parameters error); the working mode is remembered per
  model for 24h. Any other 4xx fails the run. Rows failing Pydantic validation are
  dropped individually. Titles are length-capped and control characters stripped before
  use in TMDB queries or later prompts.
- Ported from stremio-ai-search: empty content with `finish_reason == "length"` raises a
  clear "raise max tokens / reasoning used N tokens" error; key-prefix / base-URL hint on 401.
- **One budget gate for every outbound model attempt** (`budget.reserve()`), regardless of
  path: generation batches, top-ups, format-downgrade attempts, transient retries (max 2
  per call, only for timeout/429/5xx), whole-run retries, manual refreshes, CLI runs and
  dry runs. Limits: `max_model_calls_per_run`, `max_model_calls_per_day` (global, default
  40) and `max_runs_per_day` per catalog. Reservations are rows in `llm_ledger` written
  before the call, so limits hold across restarts and across web/worker/CLI.
- **Dry run** = full pipeline, no snapshot/history/schedule mutation; it still writes a
  `runs` row (`dry_run=1`) and ledger rows (and refreshes `tmdb_cache` / `watched_cache`).
- Run wall-clock cap (default 20 min) enforced by the worker.
- `scripts/probe_model.py`: checks that the chosen model accepts the configured
  temperature with strict JSON schema, with and without `:online`, and reports tokens.
  Default model is chosen from its output; if `:online` degrades JSON compliance, use the
  plain model (grounding is unnecessary once the date is injected).

## 8. TMDB resolver and gatekeeper (`app/curation/tmdb_gate.py`)

- Shared `httpx.AsyncClient`, semaphore (8), retry on 429 honouring `Retry-After` and on
  5xx/timeout with backoff; results cached in `tmdb_cache` (resolution 30 days, details 7
  days so ratings/vote counts stay fresh).
- Matching: search with `primary_release_year`; if no acceptable candidate, retry year
  +/- 1, then without year. Acceptable = normalised `title` or `original_title` equals
  the LLM title or similarity >= 0.9, and release year within +/- 1. Among acceptable
  candidates take the highest `vote_count`. Otherwise reject `no_match`; never `results[0]`.
- Hard filters on details (`append_to_response=external_ids,keywords[,credits]`), each a
  per-catalog parameter that is off unless the catalog sets it (section 6): minimum
  runtime (missing -> reject when set), minimum `vote_average` and `vote_count`,
  `original_language` allow-list, excluded genres, release-year range,
  `blacklist_tmdb_ids`, `franchise_denylist`. Always enforced, because the addon cannot
  serve the film otherwise: `adult == false`, `status == "Released"`, IMDb id present and
  matching `^tt\d+$`.
- Tests use fixtures including "Modern (2014)", same-title remakes
  (Total Recall 1990/2012, The Thing 1982/2011), alias pairs, and a film with no collection.

## 9. Serving for AIOMetadata (`app/api/curated.py`)

- Routes:
  - `GET /c/{token}/manifest.json` — all enabled curated catalogs
  - `GET /c/{token}/catalog/movie/{catalog_id}.json` — the whole active snapshot
  - `GET /c/{token}/catalog/movie/{catalog_id}/skip={n}.json` — see below
  One manifest only (AIOMetadata lets the owner tick individual catalogs at import), so
  no per-catalog manifest routes.
- **Single-response mode (the integration contract):** the base URL returns the entire
  active snapshot; any request with `skip > 0` returns `{"metas": []}`. Positive offsets
  therefore always terminate and can never return items from a different snapshot than
  the one AIOMetadata cached at offset 0 (this closes the "snapshot A had 60, `skip=60`
  served from snapshot B" case). The manifest declares no `skip` extra. `target_count` is
  capped at 250 to keep the single response reasonable.
- Remaining limitation, documented: AIOMetadata caches the offset-0 batch for its
  `cacheTTL`, so a new snapshot appears up to one TTL late, and its own Redis cursor /
  filter refill logic governs how it pages our 100 items to Stremio. README: import with
  a 1-2h TTL and schedule our refresh a few hours before viewing time.
- Never return an error or empty list at offset 0 while any snapshot exists.
- No-snapshot state: a catalog is **not listed in the manifest until its first snapshot
  exists**, so it cannot be imported empty. If its URL is requested anyway (imported
  earlier, then state wiped), we answer 503. That is only an operational signal for our
  logs and status page: AIOMetadata catches upstream errors and converts them to `[]`
  (`fetchStremThruCatalog`), so `Retry-After` is not honoured, and how long it caches
  that empty result is governed by its own empty-result TTL, not by us. The integration
  matrix measures how long an empty result sticks; the README tells the owner to restore
  from backup or run a refresh before importing.
- Manifest: fixed id (`CURATED_ADDON_ID`, default `community.curated-catalogs`),
  `resources:["catalog"]`, `types:["movie"]`, `idPrefixes:["tt"]`, catalogs
  `{type:"movie", id, name}`. Catalog ids `^[a-z0-9]+(_[a-z0-9]+)*$` so they are unchanged
  by AIOMetadata's `_` replacement and stay unique. README warns that manifest id, catalog
  ids, token and base URL are frozen into AIOMetadata's config at import.
- Metas: `id` (IMDb), `type`, `name`, `poster` (TMDB image URL), `releaseInfo`,
  `description`, `imdbRating`-free (we only have TMDB scores; omit rather than mislabel).
- Unknown catalog -> 404. `Cache-Control: no-store` on catalog responses (AIOMetadata does
  its own caching).
- Fix in passing for legacy code: `rstrip` -> `removesuffix`, unknown id -> 404.

Integration test matrix (milestone 2, against a local AIOMetadata container, repeated at
the end): import via browser fetch and via proxy for an internal URL; 100-item snapshot
pages correctly to the end in Stremio; 60-item -> 100-item swap; 100 -> 60 swap; cursor
missing/expired; AIOMetadata running without Redis; AIOMetadata catalog filters enabled
(its refill loop requesting further offsets); swap visible after TTL; no-snapshot state.

## 10. Operations surface (`app/api/ops.py`)

v1:
- `POST /ops/catalogs/{id}/refresh` and `POST /ops/catalogs/{id}/dry-run`: require
  `Authorization: Bearer <OPS_TOKEN>` (constant-time compare; routes 404 when `OPS_TOKEN`
  is unset). They only enqueue a job (one open job per catalog; subject to the budget).
  No cookies are involved, so there is no CSRF surface.
- `GET /ops/status` (same bearer token, or `?` none — header only): JSON with, per
  catalog, active snapshot age and size, last runs with stage counts and reject reasons,
  next run, budget used today, worker heartbeat. A minimal HTML view of the same data is
  served to requests carrying the header (for use behind a reverse proxy that injects it).
- CLI (`python -m app.curation ...`): `run <id> [--dry-run]`, `rollback <id> <snapshot>`,
  `history clear <id>`, `validate`, `backup <path>`, `status`.

v2 (milestone 8): browser editor writing catalog definitions to SQLite (YAML becomes
import/export), with: login form, `SESSION_SECRET` from env (required, >= 32 bytes,
startup fails without it when the editor is enabled), signed HttpOnly SameSite=Strict
cookies, `Secure` when `PUBLIC_BASE_URL` is https, 12h expiry, CSRF token on every POST,
login rate limit.

CORS, structurally (built in milestone 1): the parent app has no CORS middleware. The
Stremio routes live in a sub-app with
`CORSMiddleware(allow_origins=["*"], allow_credentials=False, allow_methods=["GET"])`
that is mounted **last, at the empty prefix**, so it is a catch-all: anything registered
on the parent after `create_app()` returns is shadowed and answered by it (404 with a
CORS header). Therefore `/ops` (and later `/admin`) are each a dedicated sub-app with no
CORS middleware, mounted at their prefix **inside `create_app()` before the catch-all**,
so their unknown paths and OPTIONS requests also stay outside Stremio CORS. Tests per
sub-app: an authorised request, an unauthorised one, a missing path and a preflight, each
with an `Origin` header and none returning `Access-Control-Allow-Origin`.

Secrets and tokens:
- `OPENAI_API_KEY`, `TMDB_API_KEY` only from env; never in SQLite, YAML, responses or logs.
  Errors from providers and HTTP clients are logged and stored as type + status only
  (`describe_error`, `runs.error_category`); the sentinel tests capture logs as well as
  responses during forced provider failures.
- `ADDON_ACCESS_TOKEN` in `/c/{token}/` is a bearer credential that only grants read
  access to a movie list; it is optional (unset -> path segment `public`). It will appear
  in AIOMetadata's stored config and possibly its logs — stated in the README. On our
  side: uvicorn runs with `--no-access-log`, the app's own request logger masks the
  `/c/` and `/config/` segments, and the README includes the reverse-proxy log-format
  snippet (Caddy/nginx) that drops the path segment.
- `OPS_TOKEN` is header-only, never in URLs.
- `MDBLIST_API_KEY` (section 20) likewise: env only. MDBList takes it as a query
  parameter, so request URLs are never logged.
- Outbound requests go only to the configured OpenRouter base URL,
  `api.themoviedb.org` and (only for `exclude_watched` catalogs) `api.mdblist.com`; `OPENAI_BASE_URL` is env-only (not editable through any route).

## 11. Scheduler (`app/worker.py`)

- Lease renewal and job heartbeat run in the independent heartbeat task (section 4), not
  in this loop.
- Loop every 30s: recover abandoned jobs; enqueue jobs for catalogs whose
  `schedule.next_run_at <= now` (then advance `next_run_at` from the cron expression in
  the catalog's timezone); claim and run one job at a time.
- Catch-up: on start, a catalog whose active snapshot is older than its interval (or
  absent) gets one job.
- Catalog removed/disabled or `config_rev` changed mid-run -> commit check fails ->
  run `superseded` (section 4).
- SIGTERM: stop claiming, abandon the in-flight run without committing, release the lease.

## 12. Deployment

- `docker-compose.yml`: `web` and `worker` services from the locally built image, shared
  named volume at `/data`, `./catalogs.d` mounted read-only, env from `.env`
  (`OPENAI_API_KEY`, `TMDB_API_KEY`, `OPS_TOKEN`, `ADDON_ACCESS_TOKEN`, `PUBLIC_BASE_URL`,
  `ENABLE_LEGACY=false`). No Redis service by default (only needed with legacy enabled,
  and then without a published port). Both services may share a Docker network with
  AIOMetadata so the manifest can be imported by internal URL.
- SQLite: WAL, `busy_timeout=5000`, `synchronous=NORMAL`, foreign keys on. The volume must
  be a local filesystem (not NFS/SMB) — stated in the README.
- **Backup:** `python -m app.curation backup /data/backups/<ts>.sqlite` uses SQLite's
  online backup API (`sqlite3.Connection.backup`), safe while running; the worker also
  takes one automatically before each migration and nightly (keep 7). Never documented:
  copying the live `.sqlite` file. Restore = stop both services, replace the file (and
  remove `-wal`/`-shm`), start. `catalogs.d/` is config and is backed up as files
  (ideally in git); the README distinguishes "config" from "full state".
  Restore test: back up, mutate, restore, assert active snapshot and history are as backed up.
- New dependencies, pinned: `ruamel.yaml` (milestone 5: pure Python, so it installs on
  `python:3.15-alpine`, where PyYAML has no wheels), `croniter`, `tzdata` (Alpine has no system zone
  database for `zoneinfo`), `rapidfuzz` (title similarity). `jinja2` is already present.
- **Entrypoint:** the inherited `entrypoint.sh` always execs uvicorn and ignores its
  arguments, so it is rewritten as a dispatcher: with no arguments or `web` it computes
  the worker count and `exec`s uvicorn as today; with any other arguments it does
  `exec "$@"`. Compose sets `command: ["python","-m","app.worker"]` for the worker
  service. `exec` keeps each process as PID 1 so SIGTERM reaches it; compose
  `stop_grace_period: 30s`. Compose smoke test (CI, `docker compose up`): the web
  container's PID 1 is uvicorn and serves `/healthz`; the worker container's PID 1 is the
  scheduler, holds the lease and listens on no port; `docker compose stop` ends both
  within the grace period with exit code 0 and the worker releases its lease.
- Dockerfile: `/data` owned by the non-root user; healthchecks: web `/healthz`, worker
  heartbeat age via `python -m app.curation status --check`.
- Housekeeping: keep upstream MIT LICENSE/copyright, add fork notice and credit to both
  upstream projects; new addon id/name; logo/background URLs no longer point at the
  upstream repo; update pinned dependencies and run the test suite first. AIOMetadata is
  AGPL/FSL: we only read it to learn its HTTP behaviour and copy no code from it.

## 13. Tests

- Unit: prompt rendering; normalisation; match selection; every gatekeeper rule;
  resolved-id dedupe (aliases, adjacent years); selection with caps and quotas (pool
  concentrated in a few collections; missing collection ids are independent); top-up
  shortfall computed after selection; history strict/relax ordering by most recent
  activation; format-downgrade only on capability errors; `finish_reason=length`; budget
  gate counts every attempt and survives restart.
- Pipeline with fake LLM + fake TMDB: happy path = exactly 100 unique; shortfall + strict
  -> run fails, previous snapshot and history untouched; relax mode records window and
  repeats; two consecutive runs share zero ids; dry run mutates nothing but the ledger.
- Store/worker: lease takeover with fencing (old worker's commit rejected); SIGKILL
  between claim and commit and inside commit; abandoned-job recovery frees the index;
  config edited/disabled mid-run -> superseded; DST schedule; catch-up; backup/restore.
- API: manifest shape; whole snapshot at offset 0; `skip>0` empty; 404s; token checks;
  CORS isolation; secret-sentinel sweep (section 3).
- AIOMetadata integration matrix (section 9).

## 14. Milestones (each leaves a working addon)

1. Fork hygiene + security: seed the owner's empty private repo
   `RenewedKeys/ai-catalogs` with the upstream history (clone upstream, add it as the
   `upstream` remote, push `main` to `origin`; it is not a GitHub fork object, so the MIT
   LICENSE and a fork notice in the README carry the attribution); deps, rename, `ENABLE_LEGACY=false`, secret-rendering fix,
   sentinel test, CORS restructuring, compose with web/worker/volume.
2. Delivered in two pull requests:
   - **2a**: store (full schema incl. `catalog_defs`, `activations`, history retention;
     migrations safe under concurrent first start) + curated routes + manifest fed by a
     fixture snapshot (`load-fixture`); AIOMetadata contract test and the runbook for the
     integration matrix (`docs/aiometadata-integration.md`), the riskiest external
     dependency, tested first. Python 3.15 (the image's version) added to the CI matrix.
   - **2b**: lease, fenced job claim and fenced commit primitives with their tests,
     including SIGKILL between claim and commit and inside the commit.
   Milestone 2 is complete when 2b is merged and the integration matrix has been run
   against a real AIOMetadata (it needs Docker and a Stremio client, so the owner runs it).
3. TMDB resolver + gatekeeper + blacklist/franchise resolver script.
4. Curated LLM layer + budget ledger + prompt templating + `probe_model.py`.
5. Pipeline + history + definition loader (`apply`, `validate`) + the **shared leased job
   runner**: the one code path that admits a job, takes the lease, runs the pipeline and
   commits. The runner owns everything section 4 requires of a lease holder from this
   milestone on: the independent heartbeat task (lease renewal + job heartbeat),
   cancellation on lost ownership and on SIGTERM/SIGINT, the wall-clock cap, and
   abandoned-job recovery, which it performs at start before claiming. CLI `run` /
   `--dry-run` enqueue a job and execute it through that runner (taking the lease if no
   worker holds it, otherwise leaving it to the worker); nothing publishes outside it. CLI
   `rollback` / `history clear` as maintenance operations (section 4). Tests: a standalone
   CLI run longer than the lease TTL keeps the lease and commits; a CLI run killed with
   SIGKILL leaves a job that the next invocation recovers, with the previous snapshot intact.
6. Worker process: a long-lived loop around the same runner adding the schedule, catch-up
   and periodic (every minute) recovery; `/ops` sub-app (webhook + status); backup; the
   worker's real health check replaces the placeholder and the compose smoke test also
   asserts lease ownership and release.
7. Tune the operator's first catalogs over a week of real runs; rerun the integration matrix.
8. (v2) Browser editor, dry-run UI, rollback UI.

## 15. Open items for the owner

- SQLite instead of Redis for durable state (recommended).
- v1 edits prompts in a YAML file plus webhook/CLI; the browser editor is milestone 8.
- Defaults `history_mode: strict`, `on_shortfall: fail`, window 7: on a bad day the list
  stays as yesterday's instead of repeating or shrinking. Alternative: opt into `relax`.
- Rating floors are TMDB-based; an IMDb floor would need another data source (IMDb's
  daily ratings dataset is the candidate).
- Default model, chosen after the probe script.

## 16. Milestone 1 status

Implemented on branch `milestone-1-fork-hygiene` (see README and the commit messages).
Deviations from sections 3 and 12, all kept:

- Blank credential fields keep the stored value, but a stored LLM key is only reused with
  the base URL it was stored for (`/save-config` sends the key to the URL in the form), and
  the env `RPDB_API_KEY` is never a fallback (RPDB poster URLs embed the key).
- Legacy error responses and logs carry no provider text.
- Legacy manifest artwork is only emitted when `PUBLIC_BASE_URL` is set.
- The compose smoke test runs under its own project name, image tag and empty env file, so
  it cannot touch a real deployment's containers, volume or credentials.
- Removed: CODEOWNERS, the CD workflow, dependency-review, the CodeQL and Trivy jobs, the
  Codecov upload, the multi-arch build job, two stray root-level scripts.

Gates before milestone 1 counts as complete (none can be met on the development machine,
which has no Docker):

1. Seed push of upstream `main` to `RenewedKeys/ai-catalogs`, then the branch.
2. A green CI run, including `scripts/compose_smoke.sh` (image build, both services, clean stop).
3. The configure flow exercised once in a browser with `ENABLE_LEGACY=true`: fresh save
   with blank keys, edit with blank keys, provider change requiring a key.

## 17. Milestone 4 status

Implemented on branch `milestone-4-llm`:

- `app/curation/limits.py`: `Limits` parses the limit fields of a definition once and
  produces both `GateRules` (`gate_rules_from_definition`, for milestone 5) and the
  plain-language "HARD CONSTRAINTS" block / `limits` template variable. Unset limits
  produce no text and no rule; a parity test fails if a `GateRules` threshold gains no
  prompt counterpart.
- `app/curation/prompt.py`: sandboxed `StrictUndefined` rendering, section 6 variables plus
  `limits_block`. Blocks the template does not place itself (limits, unmet quotas,
  already-this-run, exclusions) are appended after it, so a plain free-form prompt still
  gets every limit. Size cap trims exclusions (oldest first), then already-this-run; the
  template and constraints are never cut. The per-category "angle" hint is left to the
  template (there is no definition field for angles yet).
- `app/curation/llm.py`: `CuratedLLMService` (built on the upstream `LLMService`
  approach, without its error swallowing, SDK retries and blanket downgrade). The working
  output mode is remembered per (base URL, model) for 24 h in table `llm_modes`
  (migration 2), so separate processes share it.
- `app/curation/budget.py`: ledger-row reservation per attempt; per-run and global daily
  limits (`MAX_MODEL_CALLS_PER_DAY`, default 40, UTC day). `max_runs_per_day` stays in
  `app.store.jobs.enqueue_job` (rolling 24 h, refreshes only), as built in milestone 2.
- `scripts/probe_model.py`: tested against a fake transport only; the live probe is
  pending until a session with `OPENAI_API_KEY` runs it.

## 18. Milestone 5 status

Implemented on branch `milestone-5-pipeline`:

- `app/curation/pipeline.py`: section 5 stages 1-12. Classification (stages 4-8) is
  recomputed from cached TMDB lookups on every pass, so stage counts describe the final pool.
  Top-ups are one batch per round (at most 60, `3 x gap`) and stop on budget, a provider
  error, or two rounds that add nothing eligible. After a relaxed window, selection ranks
  unseen films first, then repeats from the oldest activation. `publish_partial` scales hard
  quotas down (rounded down). With both `relax` and `publish_partial`, the window is relaxed
  towards the full count first.
- `app/curation/definitions.py` + `app/curation/exclusions.py`: the loader (`validate`,
  `apply`), `ruamel.yaml` (pure Python). Migration 3 adds `catalog_defs.exclusions_json`
  (resolved ids, outside `config_rev` so TMDB drift never bumps the epoch),
  `catalog_defs.source` (only `file` catalogs are marked removed) and `definition_errors`
  (errors of files that never validated). Automatic picks: a title with a year takes the
  most-voted exact match within +/- 1 year; a bare title takes every exact match (remakes
  too); fuzzy matches are never auto-picked. If TMDB fails, the definition is not accepted.
- `app/curation/runner.py`: the shared leased job runner (heartbeat task, cancellation on
  lost lease and SIGTERM/SIGINT, wall-clock cap `RUN_WALL_CLOCK_S`, recovery at start).
  The CLI waits at most one lease TTL for a dead holder's lease and leaves the job to a live
  worker. An explicit `run` claims its own catalog's job even inside a retry backoff.
- `app/store/maintenance.py`: rollback and history clear.
- Not done here (milestone 6): worker loop, schedule/catch-up, periodic recovery, `/ops`,
  `status`, `backup`.


## 19. Milestone 6 status

Implemented on branch `milestone-6-worker`:

- `app/worker.py`: the long-lived loop around `app.curation.runner.Runner` (no second job
  path). Start: apply `catalogs.d`, take the lease (waiting out at most one TTL), catch-up.
  Every `WORKER_TICK_S` (30 s): re-apply on SIGHUP or when a file's name/mtime/size changes
  (and every 10 minutes while the last apply left a file unapplied, e.g. TMDB down), recover
  abandoned jobs, queue due scheduled jobs, take the nightly backup when due, then run jobs
  one at a time. SIGTERM/SIGINT: stop claiming, cancel the run (also when the signal lands
  while a job is being claimed), stop the heartbeat, release the lease, exit 0. A lost lease
  stops claims; the loop re-acquires every tick. A live holder (a CLI run) is waited for.
- `app/curation/schedule.py`: `schedule: {cron, timezone}` validated on apply (five-field
  cron only, no `@` nicknames, seconds, random `R` or hashed `H` fields; IANA zone, default
  UTC; a cron that never fires is an error). Fire times are evaluated on local wall-clock time:
  a time in a spring gap fires after the gap, shifted by its length; a time in an autumn
  overlap fires once, at its first occurrence. `schedule.next_run_at` is persisted; a due fire
  is admitted and `next_run_at` advanced from now in **one transaction** (`jobs.admit_job`,
  split out of `enqueue_job`), whatever the admission result, so missed fires never pile up.
  No migration was needed: an edited schedule is detected because the stored time is no
  longer a fire time of the current expression. Catch-up (one job if there is no active
  snapshot or it is older than the interval between the next two fires) runs at start and
  for a catalog that just got a schedule row. Catalogs without a schedule (and fixture
  catalogs) are never scheduled or caught up: they refresh only via CLI or `/ops`.
- `app/api/ops.py`: the `/ops` sub-app without CORS, mounted inside `create_app()` before the
  Stremio catch-all. A middleware runs before routing: `OPS_TOKEN` unset -> 404 for every
  path; wrong/missing bearer -> 401 for every path, including unknown ones; constant-time
  compare of UTF-8 bytes. Webhooks only enqueue (202 / 409 open job / 429 daily limit / 404).
  `GET /ops/status` is JSON, or HTML with `Accept: text/html` / `?format=html`; every value is
  escaped and no definition content (prompts) is shown. Tests: authorised, unauthorised,
  missing path and preflight, each with `Origin`, none with an `Access-Control-*` header; the
  sentinel sweep now includes `OPS_TOKEN` and authorised `/ops` responses after a failed run.
- `app/curation/status.py`: shared by `/ops/status` and CLI `status [--json]`. The worker
  heartbeat is the lease renewal (`expires_at - TTL`), so no new table. `status --check`
  passes only while a process whose owner is `worker-<this hostname>-` holds an unexpired
  lease (a CLI run does not count); it is the worker's compose health check (start period
  120 s > lease TTL).
- `app/store/backup.py`: CLI `backup PATH` (`sqlite3.Connection.backup`, temp file + rename);
  `nightly-*` by the worker and `pre-migration-v<N>-*` by `db.migrate` itself (any process
  that upgrades a non-empty database; a failed backup stops the upgrade), `BACKUP_KEEP` (7)
  per kind in `BACKUP_DIR` (default `<database dir>/backups`); manual backups are never
  pruned. Restore test: back up, mutate, restore, same active snapshot and history.
- Dependencies (pure Python wheels, so they install on `python:3.15-alpine`): `croniter`,
  its dependency `python-dateutil`, `tzdata`.
- `scripts/compose_smoke.sh` also asserts the worker holds the lease, its health check turns
  healthy, and after `docker compose stop` the lease is released (read from a one-off
  container on the same volume).
- Next (milestone 7): tune the operator's first catalogs over a week of real runs and rerun
  the integration matrix.

## 20. Excluding watched films (MDBList)

Owner request: besides the addon's own history window, never list a film the owner has
already watched. The watched history lives in the owner's MDBList account (MDBList syncs it
from Trakt, Plex, Jellyfin and its own check-ins).

- **Opt-in per catalog**: `exclude_watched: true` (strict boolean, default false) and
  `watched_prompt_limit` (0-500, default 50). Validation does not depend on the environment.
- **Key**: `MDBLIST_API_KEY`, env / `.env` only; never in YAML, SQLite, logs or responses.
  A catalog with `exclude_watched: true` and no key fails its run with `config` ("exclude_watched
  is set but MDBLIST_API_KEY is not") before any model call.
- **Source** (`app/curation/watched.py`): `GET https://api.mdblist.com/sync/watched?
  mediatype=movie&limit=1000&apikey=...`, full payload (not `extended=ids_only`, which drops the
  IMDb id and title), cursor pagination via `pagination.next_cursor` (at most 200 pages; a
  repeated cursor is an error). Rows `movies[].movie.ids.{tmdb, imdb}`; rows with neither id
  are skipped. A 200 without a `movies` list is `mdblist_schema`, never "nothing watched". Free
  accounts allow 1,000 requests per day; a run needs `ceil(watched / 1000)` requests at most
  every 6 h. The endpoint is not documented as supporter-only. If a full page, or a
  `pagination.total` above the rows read, comes without a cursor, the rest is read by
  `offset`; fewer rows than `total` in the end is `mdblist_schema`.
- **Cache**: migration 4, table `watched_cache(key, fetched_at, item_count, payload_json)`, one
  row (`mdblist:movies`) shared by every catalog, written in one transaction only after a
  complete fetch, read while younger than 6 h. No key material in it, so a changed key or
  account keeps the old set for up to 6 h. Dry runs refresh it too. An expired cache whose
  refresh fails fails the run; there is no stale fallback.
- **Pipeline**: loaded once in setup, before generation. Stage 5-8: a resolved `tmdb_id` in the
  set is rejected as `watched` before its details lookup; the IMDb id from the details is
  checked too. Watched films are rejects, never "held", so `history_mode: relax` cannot
  re-admit them. `assert_publishable` re-checks both ids (and fails if the option is on but the
  set is missing). `stage_counts_json.watched_known` records the set size.
- **Prompt**: block `watched` ("ALREADY WATCHED BY THE VIEWER (do not include):", lines of 12),
  the `watched_prompt_limit` most recently watched titles; appended after the template unless
  the template places `{{ watched }}`; cut first when the prompt is over its cap. These titles
  go to the model provider; `watched_prompt_limit: 0` keeps them local.
- **Errors**: `mdblist_auth` (401/403, no retry), `mdblist_rate_limit` (429; a `Retry-After`
  above 30 s, i.e. the daily limit, is not waited out), `mdblist_http_5xx`, `mdblist_timeout`,
  `mdblist_transport` (three attempts, then a transient run failure: the job is retried with
  backoff and the previous snapshot stays active), `mdblist_http_4xx`, `mdblist_schema`. Only
  categories and HTTP statuses are logged or stored (`describe_error`).
- **Tests** (`tests/unit/curation/test_watched.py`, fake MDBList transport only): watched films
  never published, relax does not re-admit them, details are skipped for TMDB-id matches,
  cache reused within the TTL and refreshed after, a failed page caches nothing, missing key ->
  `config`, 5xx -> transient with the previous snapshot kept, 401/429 handling, schema errors,
  key and body sentinels absent from logs, the runs row and the report, definition validation.
- **Not verified live**: the response shape is from MDBList's published OpenAPI schema; no
  request was made with a real key.


## 21. Milestone 8 design: browser editor, dry-run UI, rollback UI

Addendum to sections 3 and 10 (v2). Off unless `ENABLE_EDITOR=true`.

**Storage: one owner per catalog, recorded in `catalog_defs.source`.**
- `source` gains the value `editor` (no CHECK constraint exists, so no table rebuild). A
  catalog is either *file-managed* (`file`) or *editor-managed* (`editor`); `fixture` is unchanged.
- The editor writes `catalog_defs` directly, through the same `accept_definition` call and the
  same exclusion resolution as `apply_dir` (TMDB failure -> not saved, error shown). Web
  workers, the pipeline and the schedule already read only `catalog_defs`, so a save takes
  effect at once (the worker's `sync_schedules` picks up a changed schedule on its next tick).
- `apply_dir` skips every catalog whose stored `source` is `editor`: a file with that id is
  ignored (status `ignored` in the apply report and the worker log; nothing is stored, so
  `/ops/status` does not list it as a definition error), and a missing file never marks it
  removed. The editor itself checks `catalogs.d` (mounted read-only in the web container too)
  and warns on the catalog page while such a file exists. So **the editor wins once a catalog
  has moved to it**; until then the file wins and the editor shows the catalog read-only.
- File -> editor: button "Move to editor" on a file-managed catalog sets `source='editor'`
  (definition unchanged, so no epoch bump). The page then tells the owner to delete or rename
  the file (it is inert from then on). The move is one-way in this milestone: the worker
  re-applies only when `catalogs.d` changes, so a "hand back" flag in SQLite would have no
  trigger. To go back, export the YAML to a file and delete the catalog in the editor first
  (a later addition if wanted).
- New catalogs can be created in the editor (id = same rule as file names). YAML is
  import/export: "Import YAML" pastes a whole definition into the form (validated like a
  save, not saved until the owner saves); "Export YAML" downloads the stored definition
  (comments are not kept; the stored form is canonical JSON).
- Delete in the editor marks the catalog `removed` (as a deleted file does); disable/enable
  toggles `state`.
- Lost-update guard: the form carries the `config_rev` it was loaded from; a save is refused
  if the stored revision changed meanwhile.

**Auth (`/admin`, its own sub-app, no CORS, mounted in `create_app()` before the Stremio catch-all).**
- `ENABLE_EDITOR` (default false). Disabled: `/admin` is a stub sub-app that answers 404 for
  every path (like `/ops` without a token), so the prefix never reaches Stremio CORS.
- Enabled: `SESSION_SECRET` (>= 32 bytes) and `ADMIN_PASSWORD` (>= 12 characters) from env;
  `create_app()` raises at startup if either is missing or too short. One user, no username.
  Password compared in constant time (HMAC of both values).
- Session cookie `aic_admin`: stateless, `base64(payload).HMAC-SHA256(SESSION_SECRET)`,
  payload = issued-at, expiry (12 h), random session id. HttpOnly, SameSite=Strict, Path=/admin,
  `Secure` when `PUBLIC_BASE_URL` starts with https. Logout clears the cookie; rotating
  `SESSION_SECRET` invalidates every session (stated in the README: no server-side revocation).
- CSRF: every POST (login included) carries a hidden token = HMAC(secret, "csrf" | session
  id) (a pre-login token for the login form, bound to a short-lived cookie); checked in
  constant time, plus an `Origin`/`Referer` same-origin check when the header is present.
  Missing/wrong -> 403, nothing changes.
- Login rate limit in SQLite (shared by all uvicorn workers; migration 5, table
  `admin_login_failures(client, at)`): at most 5 failures per client and 20 overall per 15
  minutes, then 429 until the window passes. The client is uvicorn's `request.client.host`;
  behind a reverse proxy that is the proxy unless `FORWARDED_ALLOW_IPS` names it (uvicorn then
  takes the real address from `X-Forwarded-For`). Without that, the limit is effectively global,
  and someone who keeps guessing keeps the login locked for everyone, the owner included. The
  README therefore recommends setting `FORWARDED_ALLOW_IPS` and, better, restricting `/admin`
  at the proxy (IP allow-list or VPN).
- Every `/admin` response: `Cache-Control: no-store`, `X-Frame-Options: DENY`,
  `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; form-action 'self'`
  (no inline script; one small static JS file under `/admin/static` for live slider values and
  polling), `Referrer-Policy: same-origin`. Unauthenticated requests: GET -> redirect to the
  login page, anything else -> 401; unknown paths 404 only after auth.

**Behaviour.**
- Form generated from `CatalogDefinition.model_fields` in declaration order; fields typed `Any`
  there take their type and bounds from the same-named field of `Limits`, `LLMSettings` or
  `BudgetLimits`. Widget by type: bounded number -> slider + number box with a "set" checkbox
  (unset = limit off); unbounded number -> number box; bool -> checkbox; `Literal` -> select;
  `prompt` -> large textarea; `list[str]` -> one item per line; anything else (mappings,
  `franchise_denylist`, `schedule`, a future field) -> a small YAML box. Section headings come
  from a name -> group map with an "Other" fallback, so a field added in milestone 7 shows up
  without editor changes (a test asserts every model field gets a widget). A "Raw YAML" tab
  edits the whole definition instead.
- Validate / Save both run `validate_definition` (pydantic + limits + model/budget settings +
  schedule + feasibility + prompt render check); errors are shown next to the form (our own
  messages, never input values echoed into logs) and nothing is stored. Validate also shows the
  prompt rendered against the sample context and any exclusion names TMDB could not match.
- Dry run: button on the catalog page -> `enqueue_job(kind='dry_run', trigger='admin')`, the
  same admission as `/ops` (409 open job, 429 daily limit); the worker runs it through the
  runner. The page polls a JSON endpoint and shows the run's status, stage counts, reject
  reasons and model calls when it ends. **Save is go-live**: a dry run uses the saved
  definition (the pipeline only reads `catalog_defs`), and a save bumps the epoch, so an
  in-flight run ends `superseded` with a replacement queued, and a scheduled catalog's next
  run uses the new text. A catalog saved *with* a schedule and no active list gets a real
  catch-up refresh on the worker's next tick. Tuning flow for a new catalog: save without a
  schedule, dry run until the counts look right, then add the schedule. (Disabling is no help:
  a disabled catalog can neither dry-run nor be served.) A dry run stores no list, so the
  result is the counts, not titles.
- Rollback: the catalog page lists its snapshots (id, created, items, status, whether it still
  has a payload / is revoked) with a Rollback button on each eligible one, a confirm page, then
  `app.store.maintenance.rollback`; a refusal shows its `MaintenanceError` message.
- Only controlled values are rendered besides the definition itself (which the owner wrote);
  everything is HTML-escaped. No env value is ever rendered.

**Tests:** login ok/bad/rate-limited; cookie flags (Secure toggled by `PUBLIC_BASE_URL`), expiry,
tampered cookie; CSRF missing/wrong on every POST route; `/admin` CORS isolation (authorised,
unauthorised, missing path, preflight, each with `Origin`, no `Access-Control-*`); disabled ->
404; startup fails without/with a short secret; the sentinel sweep extended with
`SESSION_SECRET` and `ADMIN_PASSWORD` and authenticated `/admin` GETs; validation errors shown and
nothing stored; save bumps rev/epoch; a stored definition loaded into the form and saved
untouched keeps its `config_rev` (no spurious epoch bump); file vs editor ownership in `apply_dir`; dry run enqueued
and its result shown; rollback ok and refused.

## 22. Milestone 8 status

Implemented on branch `milestone-8-editor`, as designed in section 21:

- `app/api/admin.py`: the `/admin` sub-app (moved to `/configure`, section 23) (no CORS, mounted in `create_app()` before the
  Stremio catch-all). `ENABLE_EDITOR` off -> 404 for every path; on -> `check_editor_settings()`
  stops startup unless `SESSION_SECRET` has >= 32 bytes and `ADMIN_PASSWORD` >= 12 characters.
  Stateless HMAC-SHA256 cookies (`aic_admin`, 12 h; a pre-login cookie binds the login form's
  CSRF token), CSRF token + Origin/Referer check on every POST, login rate limit in table
  `admin_login_failures` (migration 5). Without a session every path, unknown ones included,
  redirects (GET) or answers 401; only the login page and its stylesheet are public. Responses
  carry `no-store`, `X-Frame-Options: DENY` and a CSP without inline script or style.
- `app/curation/editor.py`: form fields generated from `CatalogDefinition` (types and bounds of
  `Any` fields from `Limits` / `LLMSettings` / `BudgetLimits`; a new field shows up in "Other",
  as a YAML box if it has no simpler widget), form <-> definition with an exact round trip,
  `check_definition` (`validate_definition` + prompt preview + exclusion resolution, reusing the
  stored resolution), `save_checked` (lost-update guard on `config_rev`, `source = 'editor'`),
  `move_to_editor`, `set_state`.
- `app/curation/definitions.py`: `apply_dir` ignores files of editor-managed catalogs (status
  `ignored`, nothing recorded) and never marks them removed.
- `/ops` enqueue logic shared (`app.api.ops.enqueue`, trigger `admin` for editor dry runs);
  `app.curation.status.catalog_status` for one catalog.
- Templates and the small JS/CSS in `templates/admin/` (Jinja2 with autoescape).
- Tests: `tests/unit/api/test_admin.py` (off by default, startup checks, cookie flags and
  tampering, login CSRF, rate limits, CSRF + Origin on every POST route, CORS isolation and
  headers, validation errors shown and nothing stored, untouched-form round trip keeps
  `config_rev` (also parsed from the rendered HTML), stale form refused, TMDB failure saves
  nothing, file -> editor ownership, dry run queued and its result shown, rollback and refusal,
  generated widgets); the sentinel sweep now includes `SESSION_SECRET`, `ADMIN_PASSWORD` and
  logged-in `/admin` pages and actions.
- Not done (possible later additions): handing a catalog back from the editor to a file (no
  trigger, see section 21), server-side session revocation, a history-clear button, showing a
  dry run's titles (dry runs store no list).

## 23. Editor at `/configure`, proxy sign-in, addon URL

Asked by the owner after milestone 8, to fit a Traefik + Authelia setup where Stremio addon
hosts share one Authelia rule that protects `/`, anything under `/configure` and paths ending in
`/admin`, and lets everything else through (the addon routes must stay reachable by AIOMetadata).
Under `/admin/...` the editor's inner pages would have had no proxy login at all.

- The editor moved from `/admin` to `/configure` (the usual Stremio addon settings path); `/`
  redirects there when `ENABLE_EDITOR` is on (404 otherwise). The cookies' path follows.
  `ENABLE_LEGACY` also owns `/` and `/configure`, so `check_editor_settings()` refuses both on.
- `EDITOR_AUTH=proxy` (default `password`, unchanged): no login of its own. The session is the
  user name in `EDITOR_PROXY_USER_HEADER` (default `Remote-User`, set by Authelia through Traefik
  forward-auth, which deletes a client-sent copy of every `authResponseHeaders` header before
  copying Authelia's: checked in Traefik v3.7 `pkg/middlewares/auth/forward.go`). No header (or a blank one) -> 403
  for every editor path, so the editor fails closed if the proxy rule disappears. `/login` GET
  redirects to the list, `/login` POST and `/logout` answer 404. CSRF tokens are the MAC of
  `proxy:<user>`; the Origin check stays. The MAC key is `SESSION_SECRET` when set (still >= 32
  bytes), otherwise a random key created once (O_EXCL, mode 0600, 64 hex characters) as
  `editor-csrf.key` next to the database, so every uvicorn worker uses the same one. Trust
  boundary: anything that reaches the container without the proxy (a published port, another
  container on its Docker network) can send the header; documented in the README and
  `.env.example`.
- The catalog list shows the addon manifest URL (`PUBLIC_BASE_URL`, else the request's host;
  `ADDON_ACCESS_TOKEN`, else `public`) with a copy button. It is the only editor page with the
  token; the sentinel sweep allows the token on that page alone and checks it is there.
- Tests in `tests/unit/api/test_admin.py`: root redirect and 404, legacy conflict, addon URL
  (and nowhere else), proxy mode starts without secrets, fails closed without the header, has no
  login/logout, custom header name, CSRF + Origin still required, token bound to the user, key
  file private and shared between workers, `SESSION_SECRET` preferred when set.

## 24. PUID / PGID (no manual chown)

Asked by the owner so another person can install the app without fixing permissions, the way
linuxserver images and the compose template they use work. Before, the image ran as `appuser`
(1000) and a bind-mounted data folder that Docker created (root-owned) stopped the web service
at startup ("Cannot open the database") until someone ran `chown`.

- The image starts as root. `entrypoint.sh` creates `/data`, the database's directory and
  `BACKUP_DIR`, hands anything in them not owned by `PUID:PGID` (default 1000:1000) to that
  user, then `exec su-exec PUID:PGID ...`, so the service stays PID 1 and gets SIGTERM directly.
  `PUID=0` runs as root; a container started as another user (compose `user:`) skips the chown
  and runs as that user. Non-numeric IDs stop the container with a message.
- `docker exec` bypasses the entrypoint and runs as root, so `python -m app.curation` and
  `app.worker` call `app.core.privileges.drop_root()` first (setgroups, setgid, setuid). The
  worker's health check and the README's `docker compose exec worker python -m app.curation ...`
  commands therefore never leave a root-owned SQLite `-wal`/`-shm` or backup on the volume.
- Tests: `tests/unit/test_privileges.py`; the Compose smoke test checks both services run as
  uid 1000, nothing on the volume is root-owned after a CLI run through `docker exec`, and a
  volume made root-owned is handed back on restart (database still served, lease retaken).

## 25. Upstream code removed, new look

Asked by the owner: nothing from the upstream fork should remain that this app does not use.

- Removed the legacy addon (`ENABLE_LEGACY`): `app/api/web.py`, `app/api/stremio.py`,
  `app/api/deps.py`, `app/services/`, `app/models/`, `app/utils/`, its templates, `docs/LEGACY.md`,
  `WORKER_CONFIGURATION.md`, the upstream `CHANGELOG.md` and `.github/cliff.toml`, the unused
  `.github/labeler.yml` and stale-issue workflow, their tests, and the settings only they read
  (`ENABLE_LEGACY`, `ENCRYPTION_KEY`, `RPDB_API_KEY`, `REDIS_*`, `STREMIO_ADDON_ID`,
  `PREFERRED_USER_SEARCH_LANGUAGE`, `MAX_CATALOG_*`, `SPLIT_MANIFESTS`, `ENABLE_FEED_CATALOGS`,
  `CACHE_SEARCH_QUERY_TTL`, `FOOTER_ENABLED`). Dependencies dropped: `cryptography`, `redis`,
  `python-slugify`, `python-dateutil`. `create_app()` takes no arguments; the logger is
  `ai_catalogs`. `LICENSE` keeps the upstream copyright line, as MIT requires.
- New artwork in `.assets/` (sources in `.assets/src/`): `logo_256.png`, `logo_1024.png`,
  `background.png` (1920x1080); the manifest points at `logo_256.png`.
- The image now contains an empty `/app/catalogs.d`, so a deployment that only uses the editor
  needs no mount and logs no "directory does not exist" warning.
- `README.md` is a short, non-technical tour with screenshots (placeholder catalogs only); the
  previous README is `docs/setup.md`.
- Fixed while taking the screenshots: the editor showed `<built-in method items ...>` instead of
  a list's size (Jinja resolves `x.items` to `dict.items`); templates now use `x["items"]`, with
  a regression test.
- Merged remote branches deleted on GitHub (their PRs keep a restore button).

## 26. Simple editor, dark redesign

Asked by the owner: the generated form was confusing (catalog id, position, focus, weekday focus,
categories …) and plain-looking; a normal person should only need a name and a description.

- **Simple form.** Up front: name, the description (`prompt`), list size, a refresh widget
  (Manually / Every day at / Every week on / Custom cron, plus time zone, filled from the
  browser), and the rules (rating, number of ratings, years, length, languages, skip watched,
  never include). Everything else is still generated from `CatalogDefinition` under
  **Advanced settings** with plain labels (`editor.LABELS`, `HELP`); a field added later lands
  there by itself. The YAML tab stays.
- **No template needed.** A description without `{{ ... }}` works as is: the system message
  already asks for `request_count` films, and `render_prompt` appends the limits, history and
  watched blocks the template does not place. So the "variables" are optional extras, not
  something an LLM has to extract from the description; they stay under Advanced.
- **Ids from names.** A new catalog's id is the name's words joined by `_`, made unique
  (`_2`, `_3` …); there is no id field. Ids still never change once created.
- **Schedule round trip.** The widget maps `M H * * *` / `M H * * D` to daily/weekly and
  everything else to custom; a stored schedule whose meaning is unchanged is kept byte-for-byte
  (spelling, omitted timezone), so an untouched save still keeps `config_rev`.
- **Activity tab**: **Refresh now** (new `POST /configure/catalogs/{id}/refresh`, a refresh job
  with trigger `admin`) next to **Test run** (the dry run); runs are shown as
  suggested → passed your rules → picked, with plain reject reasons and the raw counts under
  "Details". Redirects after runs, visibility changes and rollbacks open the Activity tab.
- **Look**: dark only, layered gradient background, cards, the logo in the top bar and as the
  favicon; the "Signed in as" label is gone (Log out stays in password mode).
- Tests: simple create (id from name, uniqueness), plain description preview, refresh widget
  (build, exact round trip, errors), Refresh now, basic vs advanced placement, a new field
  landing under Advanced, logo present and no signed-in label; the HTML round-trip test reads
  the refresh widget like a browser.

## 27. Pickers, dashboard actions, test-run picks, plain language

Owner feedback on the section 26 editor, all in one change:

- **Dashboard**: drag ⠿ to reorder (`POST /configure/order`, positions only: no new revision,
  no epoch bump; saving a catalog keeps its dashboard position, a new catalog's `position`
  is used once); each card has ▶ Run now, ⚗ Test run, Edit and delete (with a confirm). The
  logo is bigger, with a GitHub link in the top bar.
- **No YAML tab** on the page (the server still accepts `mode=yaml`; `export.yaml` stays).
  `position` and `blacklist_tmdb_ids` are no longer on the page (`editor.FORM_HIDDEN`); a stored
  value is carried over on save.
- **Pickers** (`editor.WIDGETS`, `templates/admin/admin.js`): each enhances the existing field and
  writes back the format the server already parses (one value per line, or JSON read as YAML),
  so a form without JavaScript still works and an untouched save keeps `config_rev`. Languages
  and genres from fixed lists (names, not codes or ids); Never include and Example movies search
  TMDB (`GET /configure/suggest?kind=movie|collection|company|keyword|model&q=`, behind the
  editor's auth, errors become an empty list); franchises/studios/keywords for
  `franchise_denylist`; one box per weekday; decade and theme minimums as rows; the AI model
  from the provider's `GET /models` (cached an hour).
- **Plain descriptions get every setting**: `render_prompt` now appends focus (per weekday),
  categories and anchors when the template does not place them, like the limit blocks. Before,
  they were silently ignored by a template-free description.
- **Labels and help** rewritten (`LABELS`, `HELP`, `OPTION_LABELS`), "catalog/version/movies"
  used consistently, slider ranges sized for people (`UI_BOUNDS`: years 1900 to next year,
  length up to 240, repeat window up to 30 …). Page notices are short ("Saved."), and validation
  errors are translated to on-page labels (`admin.friendly_error`).
- **Check vs test**: 👁 Preview (free: renders the request, no AI call) in the save bar with
  Cancel; ⚗ Test run (calls the AI with the saved settings, publishes nothing) and ▶ Run now at
  the top of the catalog and on its card, each with a one-line explanation.
- **Test runs keep their picks**: migration 6 adds `runs.preview_json`
  (`[[title, year, imdb_id], ...]`, dry runs only); the Activity tab shows them as a numbered
  grid, and each version under Versions can show its movies. Run details are plain phrases
  ("2 AI requests · 1,900 tokens sent, 2,700 received").
- Not done here, proposed separately: series (TV shows) next to movies; publishing a ready-built
  container image with numbered releases.
- **Fewer calls per run**: `pipeline.batch_sizes` folds a remainder under a third of a batch into
  the other calls and splits evenly (64 -> one call instead of 60 + 4; 160 -> 54/53/53). Calls
  stay under 80 movies, well within the default `max_tokens`.

## 28. Releases, published image, Gemini key

- **Releases**: a release is a `vX.Y.Z` tag on `main` (`.github/workflows/release.yml`). The
  workflow checks the tag equals `app.__version__`, builds `linux/amd64` + `linux/arm64`, pushes
  `ghcr.io/renewedkeys/ai-catalogs:X.Y.Z`, `:X.Y`, `:X` and `:latest` (only for the highest
  version, so rebuilding an old tag never moves `latest` back), then creates the GitHub release
  with notes generated from the merged pull requests. `workflow_dispatch` rebuilds an existing
  tag. Simpler than release-please (no conventional-commit titles, no bot PRs). First release:
  1.0.0. The page's top bar shows the running version.
- **Self-hosting docs**: the README has an image-based compose file in AIOMetadata's style, with
  the Traefik/Authelia labels, `expose` and profiles commented out for the Viren070 template; the
  repository's own `docker-compose.yml` stays the build-from-source file (CI's smoke test).
- **`GEMINI_API_KEY`**: with no `OPENAI_API_KEY`, it becomes the key and, unless set explicitly,
  `OPENAI_BASE_URL` becomes Google's OpenAI-compatible endpoint and `DEFAULT_MODEL` drops its
  `google/` prefix. Same code path otherwise; the model picker strips Google's `models/` prefix.
- **Hidden tuning knobs** (follow-up): `history_prompt_limit`, `watched_prompt_limit` and
  `max_prompt_chars` left the page too (`FORM_HIDDEN`); they are internal prompt-size tuning
  whose defaults fit everyone, and a stored value is kept on save. Editor emoji were replaced by
  inline SVG line icons.

## 29. TV shows (design, approved)

Each catalog is either movies or shows, picked with a **Movies / Shows** toggle at the top of the
catalog form. One catalog never mixes the two (Stremio catalogs have one type, and AIOMetadata
imports each catalog under one type).

### Data source: TMDB, not TVDB

TMDB is already wired in (client, retries, cache, key handling, pickers) and covers TV with the
same API shape: `/search/tv`, `/tv/{id}?append_to_response=external_ids,keywords`. The served id
is the IMDb id (`external_ids.imdb_id`), as for movies, so AIOMetadata resolves metadata the
same way, and can still use TVDB for show metadata on its own side. TVDB would add a second key,
client, cache and id space for no gap we found. The one weaker spot: obscure shows on TMDB
sometimes lack an IMDb id; they are rejected (`imdb_id_missing`) like movies are today. If that
proves too strict in practice, revisit then.

### Definition

- New key `type: movie | series`, default `movie`. Existing definitions are unchanged (no key =
  movie, same `config_rev`). Changing it is a normal new revision (epoch bump).
- For `series`, these keys are **rejected** when set (a clear validation error, and hidden in the
  editor): `min_runtime` (TMDB's episode length is often empty, so a hard rule would reject most
  shows), `max_per_collection` (TV has no collections), `director_cap` (shows have no director;
  a creator cap can come later if wanted), and `franchise_denylist.collections`.
- Ids stored for one type mean nothing for the other (TMDB movie and TV ids and genre ids are
  separate lists). Switching type in the editor clears `blacklist_tmdb_ids` and
  `excluded_genre_ids` and says so; a definition file whose `excluded_genre_ids` are not in the
  type's genre list is rejected. Name-based keys (`blacklist_titles`, franchise names) are
  resolved again for the new type.
- Everything else applies: rating, votes, languages, genres (TV genre list), year range and
  decade minimums (by **first-air year**), categories, blacklist, franchise companies /
  keywords / title patterns, history, schedule, budget.

### Validation (`app/curation/tmdb_gate.py`)

- `TMDBGate(rules, media_type="movie" | "tv")`. TV search uses `/search/tv` with
  `first_air_date_year`, hits read `name` / `original_name` / `first_air_date`; same matching
  (normalised title or ratio >= 0.9, year +/- 1, most votes wins).
- TV details: `/tv/{id}` with `external_ids,keywords`. `evaluate()` reads the TV fields: title
  from `name`, year from `first_air_date`, keywords from `keywords.results`, no runtime /
  collection / director checks. TV details carry `adult` like movies (checked in TMDB's docs).
  "Released" for a show = `first_air_date` set and not in the future, and `status` not one of
  Planned / In Production / Pilot (TMDB documents no enum, so known not-yet-aired values are
  refused rather than an allow-list that an unlisted value would break).
- Cache keys get a `tv:` part (`resolve:tv:...`, `details:tv:...`); movie keys stay as they are,
  so existing cache rows stay valid. TMDB movie and TV ids are separate number spaces, so they
  must never share a key.
- Exclusions (`app/curation/exclusions.py`): `blacklist_titles` search `/search/tv` for series;
  the type is part of `inputs_rev`, so switching type re-resolves.

### Watched (`exclude_watched`)

MDBList `/sync/watched` with `mediatype=show`, cached separately (`mdblist:shows`). A show counts
as watched when it appears there at all (any watched episode). The exact payload shape is checked
against MDBList while building; if it cannot be read reliably, the option is hidden for shows in
this change rather than guessed.

### Prompt (`app/curation/llm.py`, `limits.py`, `prompt.py`, `pipeline.py`)

- System message for series: "You are a TV curator ... real, existing TV series ... `year` is the
  year the show first aired". JSON key `series` (`{"series": [{title, year, category}]}`), schema
  name `series_list`; the reply parser already accepts any top-level list key.
- Constraint and shortfall lines say "show(s)" and "first aired" instead of "film(s)" and
  "released"; excluded genres are named from TMDB's TV genre list. Movie prompts do not change.

### Storage: migration 7

`ALTER TABLE snapshots ADD COLUMN media_type TEXT NOT NULL DEFAULT 'movie'`. The type a snapshot
was built for is what it is served as:

- the manifest lists each catalog with its **active snapshot's** type, so a catalog switched from
  movies to shows keeps serving its last movie list (as movies) until the first show list is
  published, never a movie list labelled series;
- the history window only counts activations whose snapshot has the catalog's current type, so a
  movie's TMDB id never blocks a show with the same number.

### Routes (`app/api/curated.py`)

- Manifest: `types` is the set of types served (`["movie"]`, `["series"]` or both; `["movie"]`
  when empty), each catalog entry carries its type, `idPrefixes` stays `["tt"]`.
- `/c/{token}/catalog/{type}/{id}.json` (and the `/{extra}` form) for `movie` and `series`; a
  type that does not match the catalog's served type is a 404. Metas carry `type: "series"`.
- AIOMetadata: its import code (`addon/utils/stremthru.js`) passes each meta's own `type` to its
  metadata lookup and maps `tt` ids to IMDb, so series metas with IMDb ids are handled. Where it
  builds the catalog URL from the imported manifest was not confirmed from source: assumed to be
  `{base}/catalog/{type}/{id}.json` (the Stremio convention). Checked end to end against a local
  AIOMetadata container before merging; if it differs, the route follows what it requests.
- After switching an existing catalog's type, the owner re-imports the manifest in AIOMetadata
  (it stores the type per catalog). The editor says so on save.

### Editor (`app/curation/editor.py`, `app/api/admin.py`, `templates/admin/`)

- A Movies / Shows segmented toggle at the top of the form (`type`), default Movies.
- Shows hides the movie-only fields (`min_runtime`, `max_per_collection`, `director_cap`, the
  collections part of the franchise picker); hidden values are dropped on save, so a series
  definition never fails on a leftover. Without JavaScript all fields show and validation
  explains.
- Labels and help say "shows" instead of "movies" for a series catalog (rendered server side from
  the stored type, swapped by the script when the toggle changes).
- Pickers: `/configure/suggest?kind=tv` for Never include / Example shows; the genre chips use
  the TV genre list. The dashboard card shows a small Movies / Shows tag.

Other movie wording to make type-aware: `app/api/admin.py` (`friendly_error`, run reason and
count labels, `_snapshot_movies`), `app/curation/prompt.py` (watched block, sample context),
`app/curation/runner.py`, `app/curation/__main__.py` (CLI output), `templates/admin/*.html`.

### Tests (`tests/unit`)

Gate (TV search params, TV details evaluation, release status, cache key separation), definition
validation (type default, movie-only keys rejected for series), limits/prompt wording, LLM system
message and schema for series, store (migration 7, served type, history split by type), routes
(manifest types, series route, wrong-type 404), editor (toggle, hidden fields dropped, TV
suggest). Placeholders only ("Show 1", category "alpha").

### Release

Minor version: `__version__` 1.1.0, tag `v1.1.0` after merge. README: replace "TV shows are
planned" with a short line; `docs/setup.md` gets the `type` key and the re-import note.

## 30. TV shows: status

Built as designed in section 29 (`app/curation/media.py` holds the type, genre lists and wording),
released as 1.1.0. Differences from the design:

- **Watched filter not for shows yet.** MDBList's `/sync/watched` show payload is not publicly
  documented and could not be checked without a key, so `exclude_watched` is refused for series
  (listed in `media.MOVIE_ONLY_KEYS`, hidden by the toggle) rather than guessed.
- **AIOMetadata's catalog URL is confirmed**: it builds `/catalog/{type}/{id}.json` from the
  imported manifest (`configure/src/utils/catalogUtils.ts`, already recorded in
  `tests/unit/api/test_curated.py`), and its `stremthru.js` passes each meta's own `type` on. The
  series contract test replays that loop.
- **Switching genres** keeps the genre ids both lists share (e.g. Comedy, Drama) and drops the
  rest, instead of clearing them all.
- `scripts/resolve_blacklist.py` still inspects movie titles only.
