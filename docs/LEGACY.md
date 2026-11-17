# Legacy addon (upstream Stremio AI Companion)

This is the README of [willtho89/stremio-ai-companion](https://github.com/willtho89/stremio-ai-companion)
as of the commit this fork started from. It describes the upstream configure page, AI search
and feed catalogs, which this fork only mounts when `ENABLE_LEGACY=true`.

Differences in this fork:

- The legacy surface is off by default. Enable it only on a trusted network: anyone who can
  reach `/configure` can generate an addon URL that spends the server's API keys.
- The configure page never shows API keys. Leave a key field blank to keep the stored value.
  A stored LLM key is only reused with the provider URL it was saved for.
- `RPDB_API_KEY` from the environment is no longer a form default.
- There is no published image; build locally with `docker compose build`.

---

🎬 Your AI-powered movie and tv series discovery companion for Stremio — powered by advanced natural language understanding using OpenAI-compatible APIs. Discover perfect films using intelligent recommendations and AI-curated collections.

![Demo Screenshot](.assets/stremio-ai-companion.gif)

---

## ✨ Features

- 🧠 Natural language movie & series search: e.g. “feel-good sci-fi from the 90s”
- 🎯 Smart AI recommendations based on mood and context
- 🎨 Detailed movie & TV data from TMDB + optional enhanced artwork from RPDB
- ⚡ Fast response time — typically 5–6 seconds per query
- 🔐 100% privacy-first: encrypted, stateless config via shareable URLs
- 🎞️ Split manifest support: Movies, Series, or both (toggleable)
- 🧺 AI-curated catalogs (predefined prompts)
- 🧩 Native Stremio support: works as a catalog addon (movies, series, and catalogs)

---

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Get API Keys

- 🔑 AI Provider Key (choose one):
  - OpenAI
  - OpenRouter — supports many models incl. Claude, Gemini, GPT-4 family
- 🎬 TMDB v4 Read Access Token
- 🖼️ RPDB API Key (optional)

### 3. Run the Server

```bash
python main.py
# or with uvicorn:
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4. Configure Your Companion

1. Visit http://localhost:8000
2. Click "Configure Your AI Companion"
3. Enter API keys and preferences
4. Copy the generated manifest URL
5. Paste it into Stremio as a new addon

---

## 🧠 How It Works

1. Describe what you want to watch.
2. AI understands your request, extracts intent.
3. Suggests clever matches using natural language & GPT-style models.
4. Metadata enriched with TMDB for movies and TV.
5. Optional artwork enhancement via RPDB.

Examples:

- “films like Inception and The Matrix”
- “tv shows like True Detective season 1”
- “lighthearted rom-coms from Europe”
- “space horror set on abandoned ships”

---

## 🌐 Deployment

### Docker (run prebuilt image)

```bash
docker run -d \
  -p 8000:8000 \
  -e ENCRYPTION_KEY="your-strong-key" \
  ghcr.io/willtho89/stremio-ai-companion:latest
```

Optional environment overrides (add -e for any you need):

- OPENAI_API_KEY, OPENAI_BASE_URL, DEFAULT_MODEL
- TMDB_API_KEY, RPDB_API_KEY
- ENABLE_FEED_CATALOGS, SPLIT_MANIFESTS, MAX_CATALOG_RESULTS, MAX_CATALOG_ENTRIES, CACHE_SEARCH_QUERY_TTL
- LOG_LEVEL, HOST, PORT
- REDIS_HOST, REDIS_PORT, REDIS_DB (enable shared caching if set)

### Docker Compose (recommended)

```bash
docker compose up -d
```

Create a .env file in the project root or export envs in your shell. Example .env:

```env
# Required
ENCRYPTION_KEY=replace-this-key

# AI Provider
OPENAI_API_KEY=your-openai-or-openrouter-key
OPENAI_BASE_URL=https://openrouter.ai/api/v1
DEFAULT_MODEL=openai/gpt-5-mini:online

# Data sources
TMDB_API_KEY=your-tmdb-v4-read-token
RPDB_API_KEY=optional

# Catalogs, manifests, caching
ENABLE_FEED_CATALOGS=true
SPLIT_MANIFESTS=false
MAX_CATALOG_RESULTS=10
MAX_CATALOG_ENTRIES=100
CACHE_SEARCH_QUERY_TTL=14400

# UI / Server
FOOTER_ENABLED=true
LOG_LEVEL=INFO
HOST=0.0.0.0
PORT=8000

# Redis (optional). If set, enables shared caching
# REDIS_HOST=redis
# REDIS_PORT=6379
# REDIS_DB=0
```

Note: The compose file also supports UVICORN_WORKERS for multi-worker deployments inside the container. If unset or 0, it auto-scales based on CPU.

---

## ⚙️ Environment Variables

These are loaded via Pydantic BaseSettings. Defaults shown are the in-code defaults unless stated otherwise.

| Variable                       | Description                                         | Default                      |
| ------------------------------ | --------------------------------------------------- | ---------------------------- |
| ENCRYPTION_KEY                 | Encryption key for secure, stateless configs (req.) | —                            |
| OPENAI_API_KEY                 | OpenAI/OpenRouter-compatible API key                | —                            |
| OPENAI_BASE_URL                | AI gateway base URL                                 | https://openrouter.ai/api/v1 |
| DEFAULT_MODEL                  | Default model identifier                            | openai/gpt-5-mini:online     |
| PREFERRED_USER_SEARCH_LANGUAGE | Preferred language for user search queries          | en-US                        |
| TMDB_API_KEY                   | TMDB v4 Read Access Token                           | —                            |
| RPDB_API_KEY                   | RatingPosterDB API key (optional)                   | —                            |
| MAX_CATALOG_RESULTS            | Max results returned per generation                 | 10                           |
| MAX_CATALOG_ENTRIES            | Max total cached entries per catalog (Redis)        | 100                          |
| CACHE_SEARCH_QUERY_TTL         | Cache TTL (seconds) for explicit search results     | 14400                        |
| SPLIT_MANIFESTS                | Enable separate movie/series manifests              | false                        |
| ENABLE_FEED_CATALOGS           | Expose predefined AI-curated catalogs in manifest   | true                         |
| FOOTER_ENABLED                 | Show footer on web pages                            | true                         |
| LOG_LEVEL                      | Logging level                                       | INFO                         |
| HOST                           | Bind host                                           | 0.0.0.0                      |
| PORT                           | Bind port                                           | 8000                         |
| STREMIO_ADDON_ID               | Base addon identifier in manifest                   | ai.companion.stremio         |
| REDIS_HOST                     | Redis host (enables shared cache if set)            | —                            |
| REDIS_PORT                     | Redis port                                          | 6379                         |
| REDIS_DB                       | Redis database index                                | 0                            |

Deployment-centric variable (container only):

- UVICORN_WORKERS: number of Uvicorn workers; 0 or unset = auto based on CPU (entrypoint.sh)

---

## 📡 Stremio Manifest & Catalog URLs

All URLs are derived from the encrypted config token and adult flag.

- Default combined manifest:
  - `/config/{CONFIG}/adult/{0|1}/manifest.json`
- Movies-only manifest:
  - `/config/{CONFIG}/adult/{0|1}/movie/manifest.json`
- Series-only manifest:
  - `/config/{CONFIG}/adult/{0|1}/series/manifest.json`
- AI-curated catalogs (movies & series):
  - `/config/{CONFIG}/adult/{0|1}/catalog/{movie|series}/{catalog_id}.json`
  - `/config/{CONFIG}/adult/{0|1}/catalog/{movie|series}/{catalog_id}/skip={N}.json`
  - `/config/{CONFIG}/adult/{0|1}/catalog/{movie|series}/{catalog_id}/search={QUERY}.json`

Split-manifest routes (when using movie- or series-specific manifests in Stremio) are also supported via a compatibility prefix segment and map to the same handlers:

- `/config/{CONFIG}/adult/{0|1}/{content_type_extra}/catalog/{movie|series}/{catalog_id}.json`
- `/config/{CONFIG}/adult/{0|1}/{content_type_extra}/catalog/{movie|series}/{catalog_id}/skip={N}.json`
- `/config/{CONFIG}/adult/{0|1}/{content_type_extra}/catalog/{movie|series}/{catalog_id}/search={QUERY}.json`

Use the preview page to copy exact URLs tailored to your config.

---

## 🛠️ Web UI & API Endpoints

Web UI:

- GET `/` — Home
- GET `/configure` — New configuration form
- GET `/configure?config=...` — Edit existing configuration
- POST `/save-config` — Validate and return manifest + preview URLs
- GET `/config/{config}/adult/{adult}/preview` — Human preview with all manifest URLs
- GET `/config/{config}` — Redirect to `/configure?config=...`
- GET `/config/{config}/adult/{adult}/{content_type}/configure` — Redirect to `/configure?config=...`

Stremio API:

- GET `/config/{config}/adult/{adult}/manifest.json` — Combined manifest
- GET `/config/{config}/adult/{adult}/movie/manifest.json` — Movie manifest
- GET `/config/{config}/adult/{adult}/series/manifest.json` — Series manifest
- GET `/config/{config}/adult/{adult}/catalog/{movie|series}/{catalog_id}.json` — Catalog entries
- GET `/config/{config}/adult/{adult}/catalog/{movie|series}/{catalog_id}/skip={N}.json` — Catalog pagination (Redis optimized)
- GET `/config/{config}/adult/{adult}/catalog/{movie|series}/{catalog_id}/search={QUERY}.json` — Catalog search (TTL cached)
- Split-manifest equivalents with the extra segment `{content_type_extra}` for the three catalog endpoints above

---

## 🔐 Security & Config Storage

- AES-256 GCM encryption + PBKDF2
- Configuration embedded in URL — no server-side storage
- Your API keys are not stored server-side

---

## 🎭 Split Manifest Support

Available via configuration. Choose a single addon or split into:

- Movies-only addon
- Series-only addon
- Combined addon

---

## 🧪 Testing

```bash
pip install -r requirements-dev.txt
pytest
pytest --cov=app
```

---

## 🤝 Contributing

Contributions welcome — follow the standard GitHub workflow!

1. Fork 📌
2. Create a new feature branch 🚧
3. Make changes and test thoroughly 🧪
4. Create a pull request 🔄

Please ensure test coverage for new features.

---

## 📄 License

MIT — See [LICENSE](LICENSE)

---

## 🙏 Acknowledgments

- [Stremio](https://www.stremio.com/) — media discovery reimagined
- [stremio-ai-search](https://github.com/itcon-pty-au/stremio-ai-search) - inspiration for this addon
- [TMDB](https://themoviedb.org) — open movie & TV metadata platform
- [OpenRouter](https://openrouter.ai) — model routing + GPT ecosystem
- [RatingPosterDB](https://ratingposterdb.com/) — cinematic posters
- [OpenAI](https://openai.com/) — large language models
