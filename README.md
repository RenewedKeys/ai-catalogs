# AI Catalogs

Self-hosted Stremio catalog addon that serves curated, daily-rotating movie lists generated
by an LLM and checked against TMDB, meant to be imported into
[AIOMetadata](https://github.com/cedya77/aiometadata) as a custom manifest.

> **Status: milestone 2 of 8, first part.** The addon serves curated lists from SQLite, but
> nothing generates them yet: lists are loaded from fixture files. [PLAN.md](PLAN.md) has the
> design and the milestones.

## What works today

- The curated addon: `<base>/c/<token>/manifest.json` lists every enabled catalog that has a
  list, and each catalog URL returns its whole list in one response (`skip > 0` is always
  empty; see PLAN.md section 9 for why).
- `python -m app.curation load-fixture PATH` publishes a list from a JSON file
  ([fixtures/test_catalog_100.json](fixtures/test_catalog_100.json) is 100 test films).
- `GET /healthz`
- Two Compose services from one image: `web`, and a `worker` that is a placeholder until the
  scheduler is built (milestone 6).
- The upstream addon (configure page, AI search, feed catalogs), **off by default**. See
  [docs/LEGACY.md](docs/LEGACY.md).

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
