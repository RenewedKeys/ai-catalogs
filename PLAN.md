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
| `category_min` (LLM-reported `category`) | selection | soft quota: ranks selection and steers top-ups, never fails a run, because the label is the model's own claim |
| shuffled flat list | JSON schema + code shuffle | hard |

Exclusion names (`blacklist_titles`, `franchise_denylist`) are resolved to TMDB ids
automatically when a definition is applied, with the same resolver as the gatekeeper;
`scripts/resolve_blacklist.py` exposes that resolution for inspection only.

The YAML also carries: `model`, `temperature`, `max_tokens`, `timeout_s`,
`target_count` (default 100), `overgenerate_factor` (default 1.6), `schedule` (cron +
timezone), `history_window_runs` (default 7), `history_mode` (default `strict`),
`on_shortfall` (default `fail`), `max_model_calls_per_run` (default 8),
`max_runs_per_day` (default 4), `weekday_focus`. Today's date is injected into the
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
  `runs` row (`dry_run=1`) and ledger rows.
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
- Outbound requests go only to the configured OpenRouter base URL and
  `api.themoviedb.org`; `OPENAI_BASE_URL` is env-only (not editable through any route).

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
- New dependencies, pinned: `PyYAML`, `croniter`, `tzdata` (Alpine has no system zone
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
