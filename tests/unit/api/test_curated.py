"""
Curated serving routes and the AIOMetadata contract (PLAN.md section 9).

The contract test replays how AIOMetadata 491f128 reads a custom-manifest catalog:

- catalog URL: manifestUrl.replace('/manifest.json', '') + '/catalog/{type}/{encodeURIComponent(id)}.json'
  (configure/src/utils/catalogUtils.ts, createCustomManifestCatalog)
- page request: that URL for skip 0, else '{url without .json}/skip={n}.json', with
  '&genre=...' appended when a genre is selected (addon/utils/stremthru.js, fetchStremThruCatalog)
- it advances its offset by the number of items received and stops on an empty page
  (addon/lib/getCatalog.ts, getExternalAddonCatalog)

This only proves our side of the contract. The real integration matrix runs against an
AIOMetadata container: docs/aiometadata-integration.md.
"""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.core.config import settings
from app.store import db
from app.store.catalogs import SnapshotItem, accept_definition, publish_snapshot

TOKEN = "s3cret-token-0123456789"


def films(start, count):
    return [SnapshotItem(f"tt{2000000 + n}", f"Film {n}", 1990 + n % 30, 700 + n) for n in range(start, start + count)]


def publish(catalog_id, batch, name="Daily Example Catalog", definition=None, state="enabled"):
    with db.session() as conn, db.transaction(conn):
        rev = accept_definition(conn, catalog_id, name, definition or {"v": 1}, state=state)
        if batch:
            publish_snapshot(conn, catalog_id, batch, config_rev=rev)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "ADDON_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    return TestClient(create_app(enable_legacy=False))


def aiometadata_read(client, manifest_url, catalog, genre=None, max_pages=20):
    """Replay AIOMetadata's paging loop; return every item and the URLs requested."""
    base = manifest_url.replace("/manifest.json", "")
    catalog_url = f"{base}/catalog/{catalog['type']}/{quote(catalog['id'], safe='')}.json"
    offset, out, urls = 0, [], []
    for _ in range(max_pages):
        params = []
        if offset > 0:
            params.append(f"skip={offset}")
        if genre and genre.lower() != "none":
            params.append(f"genre={quote(genre, safe='')}")
        url = catalog_url if not params else catalog_url[: -len(".json")] + "/" + "&".join(params) + ".json"
        urls.append(url)
        response = client.get(url)
        metas = response.json().get("metas", []) if response.status_code == 200 else []
        if not metas:
            break
        out.extend(metas)
        offset += len(metas)
    return out, urls


# --- manifest ---------------------------------------------------------------------------


def test_manifest_shape(client):
    publish("daily_example", films(0, 100))
    response = client.get(f"/c/{TOKEN}/manifest.json")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    manifest = response.json()
    assert manifest["id"] == settings.CURATED_ADDON_ID
    assert manifest["resources"] == ["catalog"]
    assert manifest["types"] == ["movie"]
    assert manifest["idPrefixes"] == ["tt"]
    assert manifest["catalogs"] == [{"type": "movie", "id": "daily_example", "name": "Daily Example Catalog"}]
    assert "extra" not in manifest["catalogs"][0]  # no skip extra: single-response contract
    assert "logo" not in manifest


def test_manifest_artwork_uses_public_base_url(client, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://catalogs.example.org")
    manifest = client.get(f"/c/{TOKEN}/manifest.json").json()
    assert manifest["logo"] == "https://catalogs.example.org/static/logo2_256.png"


def test_empty_database_serves_an_empty_manifest(client):
    assert client.get(f"/c/{TOKEN}/manifest.json").json()["catalogs"] == []


def test_catalog_without_snapshot_or_disabled_is_not_listed(client):
    publish("no_snapshot", [])
    publish("was_on", films(0, 3))
    publish("was_on", [], state="disabled")
    assert client.get(f"/c/{TOKEN}/manifest.json").json()["catalogs"] == []


# --- catalog ----------------------------------------------------------------------------


def test_offset_zero_returns_the_whole_snapshot(client):
    publish("daily_example", films(0, 100))
    response = client.get(f"/c/{TOKEN}/catalog/movie/daily_example.json")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    metas = response.json()["metas"]
    assert [m["id"] for m in metas] == [f"tt{2000000 + n}" for n in range(100)]
    assert metas[0] == {"id": "tt2000000", "type": "movie", "name": "Film 0", "releaseInfo": "1990"}


@pytest.mark.parametrize("extra", ["skip=0", "genre=Action", "skip=0&genre=Action"])
def test_extras_at_offset_zero_return_the_whole_snapshot(client, extra):
    publish("daily_example", films(0, 100))
    assert len(client.get(f"/c/{TOKEN}/catalog/movie/daily_example/{extra}.json").json()["metas"]) == 100


@pytest.mark.parametrize("extra", ["skip=1", "skip=60", "skip=100", "skip=100&genre=Action", "skip=999999999"])
def test_any_positive_skip_is_empty(client, extra):
    publish("daily_example", films(0, 100))
    response = client.get(f"/c/{TOKEN}/catalog/movie/daily_example/{extra}.json")
    assert response.status_code == 200
    assert response.json() == {"metas": []}


@pytest.mark.parametrize("extra, status", [("skip=-1", 400), ("skip=abc", 400), ("search=heist", 404), ("x", 404)])
def test_bad_extras(client, extra, status):
    publish("daily_example", films(0, 3))
    assert client.get(f"/c/{TOKEN}/catalog/movie/daily_example/{extra}.json").status_code == status


@pytest.mark.parametrize(
    "path",
    [
        "/c/{t}/catalog/movie/unknown.json",
        "/c/{t}/catalog/movie/Daily_Cult_Picks.json",
        "/c/{t}/catalog/series/daily_example.json",
        "/c/{t}/catalog/movie/disabled_list.json",
    ],
)
def test_unknown_or_disabled_catalog_is_404(client, path):
    publish("daily_example", films(0, 3))
    publish("disabled_list", films(0, 3))
    publish("disabled_list", [], state="disabled")
    assert client.get(path.format(t=TOKEN)).status_code == 404


def test_known_catalog_with_no_snapshot_is_503_and_empty(client):
    publish("lost_state", [])
    response = client.get(f"/c/{TOKEN}/catalog/movie/lost_state.json")
    assert response.status_code == 503
    assert response.json() == {"metas": []}


# --- token ------------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["public", "wrong-token-0123456789", TOKEN[:-1], TOKEN + "x"])
def test_wrong_token_is_indistinguishable_from_a_missing_route(client, token):
    publish("daily_example", films(0, 3))
    for path in (f"/c/{token}/manifest.json", f"/c/{token}/catalog/movie/daily_example.json"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


def test_without_a_token_the_segment_is_public(monkeypatch):
    monkeypatch.setattr(settings, "ADDON_ACCESS_TOKEN", None)
    publish("daily_example", films(0, 3))
    client = TestClient(create_app(enable_legacy=False))
    assert client.get("/c/public/manifest.json").status_code == 200
    assert client.get(f"/c/{TOKEN}/manifest.json").status_code == 404


@pytest.mark.parametrize("value", ["short", "has spaces in it!!", "x" * 15])
def test_weak_tokens_are_rejected_at_startup(value):
    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, ADDON_ACCESS_TOKEN=value)


def test_curated_routes_send_cors_headers(client):
    publish("daily_example", films(0, 3))
    response = client.get(f"/c/{TOKEN}/manifest.json", headers={"Origin": "https://aiometadata.example"})
    assert response.headers["access-control-allow-origin"] == "*"


def test_curated_routes_work_with_legacy_on(monkeypatch):
    monkeypatch.setattr(settings, "ADDON_ACCESS_TOKEN", None)
    publish("daily_example", films(0, 3))
    client = TestClient(create_app(enable_legacy=True))
    assert len(client.get("/c/public/catalog/movie/daily_example.json").json()["metas"]) == 3


# --- the AIOMetadata contract -----------------------------------------------------------


@pytest.mark.parametrize("count", [1, 60, 100, 250])
def test_aiometadata_reads_the_whole_list_in_one_request(client, count):
    publish("daily_example", films(0, count))
    manifest_url = f"http://testserver/c/{TOKEN}/manifest.json"
    catalog = client.get(manifest_url).json()["catalogs"][0]
    got, urls = aiometadata_read(client, manifest_url, catalog)
    assert len(got) == count
    assert len({m["id"] for m in got}) == count
    # One data request, then one empty page that ends the loop.
    assert urls == [
        f"http://testserver/c/{TOKEN}/catalog/movie/daily_example.json",
        f"http://testserver/c/{TOKEN}/catalog/movie/daily_example/skip={count}.json",
    ]


def test_aiometadata_with_a_genre_filter_also_terminates(client):
    publish("daily_example", films(0, 100))
    manifest_url = f"http://testserver/c/{TOKEN}/manifest.json"
    catalog = client.get(manifest_url).json()["catalogs"][0]
    got, urls = aiometadata_read(client, manifest_url, catalog, genre="Action")
    assert len(got) == 100 and len(urls) == 2
    assert urls[1].endswith("/skip=100&genre=Action.json")


@pytest.mark.parametrize("first, second", [(60, 100), (100, 60)])
def test_a_swap_between_pages_cannot_mix_snapshots(client, first, second):
    """
    PLAN.md section 9: snapshot A had 60 items, AIOMetadata cached offset 0, then asks for
    skip=60 after snapshot B went live. It must get nothing, not items from B.
    """
    publish("daily_example", films(0, first))
    page0 = client.get(f"/c/{TOKEN}/catalog/movie/daily_example.json").json()["metas"]
    publish("daily_example", films(1000, second))
    later = client.get(f"/c/{TOKEN}/catalog/movie/daily_example/skip={len(page0)}.json").json()["metas"]
    assert len(page0) == first and later == []
    fresh = client.get(f"/c/{TOKEN}/catalog/movie/daily_example.json").json()["metas"]
    assert len(fresh) == second and fresh[0]["id"] == "tt2001000"


def test_catalog_id_survives_aiometadata_id_mangling(client):
    import re

    publish("daily_example", films(0, 3))
    manifest = client.get(f"/c/{TOKEN}/manifest.json").json()
    for c in manifest["catalogs"]:
        assert re.sub(r"[^a-zA-Z0-9]", "_", c["id"]) == c["id"]
        assert quote(c["id"], safe="") == c["id"]
