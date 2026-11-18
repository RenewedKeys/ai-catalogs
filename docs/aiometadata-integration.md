# AIOMetadata integration matrix

PLAN.md section 9 makes one promise to AIOMetadata: the catalog URL returns the whole list,
and any `skip > 0` returns an empty list. `tests/unit/api/test_curated.py` replays
AIOMetadata's paging loop against our routes, but only a real AIOMetadata shows what its
caching, Redis cursor and filter refill actually do with that. This runbook is that check.
Run it once now (milestone 2) and again at milestone 7.

It needs Docker, a TMDB key for AIOMetadata, and a Stremio client (desktop or
web.stremio.com).

## Setup

**1. This addon, with the test fixture.** Leave `ADDON_ACCESS_TOKEN` unset for the test, so
the URLs use `/c/public/`.

```bash
docker compose up -d --build
```

```bash
docker compose exec web python -m app.curation load-fixture fixtures/test_catalog_100.json
```

```bash
curl -s http://localhost:8000/c/public/manifest.json
```

The manifest should list one catalog, `test_catalog`.

**2. AIOMetadata**, built from the commit this contract was read from (`491f128`), on the
same Docker network as this addon so it can also reach it by service name. Compose names
the network after the folder: `ai-catalogs_default` if the checkout is called `ai-catalogs`
(`docker network ls` shows it).

```bash
docker build -t aiometadata:491f128 "https://github.com/cedya77/aiometadata.git#491f128"
```

```bash
docker run -d --name aiom --network ai-catalogs_default -p 3232:3232 -e HOST_NAME=http://localhost:3232 -e TMDB_API_KEY=YOUR_TMDB_KEY -e REDIS_URL=redis://aiom-redis:6379 aiometadata:491f128
```

```bash
docker run -d --name aiom-redis --network ai-catalogs_default redis:7-alpine
```

Check AIOMetadata's `docs/self-hosting.md` at that commit if it needs more variables (for
example a database URI). Run the "without Redis" row by recreating the `aiom` container
without `REDIS_URL`.

**3. Import.** Open `http://localhost:3232/configure`, open Catalogs → Quick Add,
and paste the manifest URL for the row you are testing. Set the catalog's cache TTL as the
row says. Install the AIOMetadata addon in Stremio.

Swap the list between rows with:

```bash
docker compose exec web python -m app.curation load-fixture fixtures/test_catalog_100.json --limit 60
```

(and again without `--limit` for 100). Each load is a new snapshot, exactly as a refresh would be.

Follow our side of every request with:

```bash
docker compose logs -f web
```

## Matrix

| # | Case | How | Pass when |
|---|---|---|---|
| 1 | Import by public URL | Import `http://localhost:8000/c/public/manifest.json` (the browser fetches it) | Catalog appears in AIOMetadata's list |
| 2 | Import by internal URL | Import `http://web:8000/c/public/manifest.json` (goes through AIOMetadata's `/api/proxy-manifest`) | Catalog appears; later catalog requests reach us |
| 3 | Pages to the end | 100-item snapshot; scroll the catalog to the end in Stremio | All 100 films shown once, no duplicates; our log shows the base URL and then `skip=100`, nothing more |
| 4 | 60 → 100 | Load 60, view it, load 100, wait out the TTL, view again | First 60, then 100; never a mix |
| 5 | 100 → 60 | The reverse | First 100, then 60; no stale tail |
| 6 | Cursor missing/expired | With Redis: view, `docker exec aiom-redis redis-cli --scan --pattern 'catalog-cursor:*' \| xargs docker exec -i aiom-redis redis-cli del`, scroll again | No duplicates, no errors; extra requests are `skip>0` and get `[]` |
| 7 | Without Redis | Recreate `aiom` without `REDIS_URL`; repeat row 3 | Same as row 3 |
| 8 | Filters on | Enable a catalog filter in AIOMetadata (e.g. hide watched or a rating floor) so it refills | Shows the filtered subset; the refill loop's `skip>0` requests get `[]` and it stops |
| 9 | Swap after TTL | Import with a 1 h TTL; swap; check before and after the hour | Old list until the TTL ends, new list after |
| 10 | No snapshot | Import, then stop, delete the volume (`docker compose down -v`), start again without loading | We answer 503 (in our log); record how long AIOMetadata keeps showing an empty catalog after a fixture is loaded again |

Row 10 measures AIOMetadata's empty-result caching, which we do not control; the README
should quote the number you find.

## Results

Record the date, AIOMetadata commit, and outcome per row. Anything that fails changes the
serving contract in PLAN.md section 9 before milestone 3 starts.

| # | Date | Result | Notes |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |
| 6 | | | |
| 7 | | | |
| 8 | | | |
| 9 | | | |
| 10 | | | |
