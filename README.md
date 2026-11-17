# AI Catalogs

Self-hosted Stremio catalog addon that serves curated, daily-rotating movie lists generated
by an LLM and checked against TMDB, meant to be imported into
[AIOMetadata](https://github.com/cedya77/aiometadata) as a custom manifest.

> **Status: milestone 5 of 8.** Catalogs are defined in YAML and refreshed on demand with
> `python -m app.curation run`; the scheduled worker arrives in milestone 6.
> [PLAN.md](PLAN.md) has the design and the milestones.

## What works today

- The curated addon: `<base>/c/<token>/manifest.json` lists every enabled catalog that has a
  list, and each catalog URL returns its whole list in one response (`skip > 0` is always
  empty; see PLAN.md section 9 for why).
- Catalog definitions in `catalogs.d/*.yaml` (see [catalogs.d/README.md](catalogs.d/README.md)
  and [docs/catalog-definition.example.yaml](docs/catalog-definition.example.yaml)): a
  free-form prompt plus optional limits, each rendered into the prompt and enforced in code.
- The refresh pipeline: generate with overgeneration and top-ups, resolve and gate every
  title on TMDB, never repeat the last `history_window_runs` lists, apply caps and quotas,
  and publish exactly `target_count` items, or keep the previous list (PLAN.md section 5).
- `python -m app.curation load-fixture PATH` publishes a list from a JSON file
  ([fixtures/test_catalog_100.json](fixtures/test_catalog_100.json) is 100 test films).
- `GET /healthz`
- Two Compose services from one image: `web`, and a `worker` that is a placeholder until the
  scheduler is built (milestone 6).
- The upstream addon (configure page, AI search, feed catalogs), **off by default**. See
  [docs/LEGACY.md](docs/LEGACY.md).

## Catalogs and commands

Run these inside the worker container (`docker compose exec worker python -m app.curation ...`):

| Command | What it does |
|---|---|
| `validate` | Check every definition file; changes nothing |
| `apply` | Accept the definition files into the database (exclusion names are resolved on TMDB automatically) |
| `run ID [--dry-run]` | Apply, then refresh one catalog now (`--dry-run`: everything except publishing) |
| `rollback ID SNAPSHOT` | Serve an earlier list again |
| `history clear ID` | Let previously shown titles appear again |

A run that cannot deliver `target_count` qualifying, unrepeated films keeps the previous list
(`history_mode` and `on_shortfall` in the definition change that). Model calls are limited per
run (`max_model_calls_per_run`, default 8) and per day across all catalogs
(`MAX_MODEL_CALLS_PER_DAY`, default 40).

## Add it to AIOMetadata

Import `<PUBLIC_BASE_URL>/c/<ADDON_ACCESS_TOKEN>/manifest.json` (or `/c/public/...` without a
token) in AIOMetadata's Custom Manifest Integration and set the catalog's cache TTL to 1-2
hours, so a new daily list shows up within that time instead of up to 24 hours later.

The manifest id, catalog ids, token and base URL are frozen into AIOMetadata's config at
import; changing any of them means importing again. A catalog only appears in the manifest
once it has a list, so it cannot be imported empty.

[docs/aiometadata-integration.md](docs/aiometadata-integration.md) is the end-to-end check
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

All settings are environment variables; [.env.example](.env.example) lists them. API keys are
read from the environment only and are never shown on a page. Error logs record the type of
a failure and its HTTP status, not the provider's message, which can quote a key. One caveat
applies with `ENABLE_LEGACY=true`: an addon URL generated there carries the keys in encrypted
form, so whoever holds that URL can spend them through this server.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | OpenRouter (default) or another OpenAI-compatible API |
| `TMDB_API_KEY` | TMDB v4 read access token |
| `PUBLIC_BASE_URL` | Externally reachable URL of this server |
| `ADDON_ACCESS_TOKEN` | Secret path segment of the addon URLs (16+ characters); unset means `public` |
| `DATABASE_PATH` | SQLite file; default `/data/ai-catalogs.sqlite` on the compose volume, which must be a local disk |
| `ENABLE_LEGACY` | `true` mounts the upstream addon routes; needs `ENCRYPTION_KEY`. Trusted networks only |

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
cleanly (needs Docker; CI runs it).

## Credits and licence

This is a fork of [willtho89/stremio-ai-companion](https://github.com/willtho89/stremio-ai-companion)
(MIT, archived upstream), started from its commit `a49d450` with the full history kept.
Licensed under the MIT License; see [LICENSE](LICENSE), which keeps the upstream copyright.
