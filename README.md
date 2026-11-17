# AI Catalogs

Self-hosted Stremio catalog addon that serves curated, daily-rotating movie lists generated
by an LLM and checked against TMDB, meant to be imported into
[AIOMetadata](https://github.com/cedya77/aiometadata) as a custom manifest.

> **Status: milestone 1 of 8.** The fork is cleaned up and secured, but the curated catalogs
> themselves are not built yet. [PLAN.md](PLAN.md) has the design and the milestones.

## What works today

- `GET /healthz`
- Two Compose services from one image: `web`, and a `worker` that is a placeholder until the
  scheduler is built (milestone 6).
- The upstream addon (configure page, AI search, feed catalogs), **off by default**. See
  [docs/LEGACY.md](docs/LEGACY.md).

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
read from the environment only and are never shown on a page or written to a log. One caveat
applies with `ENABLE_LEGACY=true`: an addon URL generated there carries the keys in encrypted
form, so whoever holds that URL can spend them through this server.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | OpenRouter (default) or another OpenAI-compatible API |
| `TMDB_API_KEY` | TMDB v4 read access token |
| `PUBLIC_BASE_URL` | Externally reachable URL of this server |
| `ENABLE_LEGACY` | `true` mounts the upstream addon routes; needs `ENCRYPTION_KEY`. Trusted networks only |

## Development

```bash
python3.11 -m venv venv && ./venv/bin/pip install -r requirements-dev.txt
```

```bash
./venv/bin/pytest
```

`sh scripts/compose_smoke.sh` builds the image and checks both services start, serve and stop
cleanly (needs Docker; CI runs it).

## Credits and licence

This is a fork of [willtho89/stremio-ai-companion](https://github.com/willtho89/stremio-ai-companion)
(MIT, archived upstream), started from its commit `a49d450` with the full history kept.
Licensed under the MIT License; see [LICENSE](LICENSE), which keeps the upstream copyright.
