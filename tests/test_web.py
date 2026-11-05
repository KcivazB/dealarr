import re
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from dealarr.catalog import Catalog
from dealarr.config import ConfigError, Settings
from dealarr.fetch import FetchError, assert_public_host
from dealarr.storage import Store
from dealarr.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
A = "https://www.philibertnet.com/fr/next-move/54391-azul-826956620105.html"
B = "https://www.ludum.fr/jeux-de-strategie-en-famille/azul-193"


# --- catalogue -----------------------------------------------------------------------------------

@pytest.fixture
def catalog(tmp_path):
    store = Store(tmp_path / "t.db")
    yield Catalog(store.conn)
    store.close()


def test_yaml_roundtrip(catalog):
    data = yaml.safe_load((ROOT / "watchlist.example.yaml").read_text(encoding="utf-8"))
    assert catalog.import_dict(data) == 2
    exported = catalog.to_dict()
    products = catalog.load_products()
    assert [p.id for p in products] == ["azul", "azul-duel"]
    azul = products[0]
    assert azul.target_cents == 3200 and len(azul.offers) == 10
    bcd = next(o for o in products[1].offers if "bcd-jeux" in o.url)
    assert bcd.policy.tiers == ((1500, 390), (3000, 320), (4900, 220))
    assert exported["default_shipping"] == "4.90"
    assert exported["products"][1]["rules"] == {"drop_pct": 20}
    # le YAML exporté se réimporte à l'identique
    assert catalog.import_dict(yaml.safe_load(yaml.safe_dump(exported))) == 2
    assert catalog.to_dict() == exported


def test_invalid_import_changes_nothing(catalog):
    catalog.create_product("Azul", 3200, [A])
    with pytest.raises(ConfigError):
        catalog.import_dict({"products": [{"id": "x", "offers": []}]})
    assert [p.id for p in catalog.load_products()] == ["azul"]


def test_product_crud(catalog):
    pid = catalog.create_product("Les Aventuriers du Rail : Europe", None, [A, A, B])
    assert pid == "les-aventuriers-du-rail-europe"
    assert catalog.create_product("Les Aventuriers du Rail : Europe", None, [A]) == pid + "-2"
    assert len(catalog.offers(pid)) == 2  # doublon dédupliqué

    with pytest.raises(ConfigError, match="déjà suivie"):
        catalog.add_offer(pid, A)
    with pytest.raises(ConfigError):
        catalog.update_product(pid, "X", None, True, {"drop_pct": "abc"})
    catalog.update_product(pid, "Ticket to Ride", 4000, False, {"drop_pct": 20})
    p = catalog.product(pid)
    assert (p.name, p.target_cents, p.enabled, p.rules.drop_pct) == ("Ticket to Ride", 4000, False, 20)

    first, second = catalog.offers(pid)
    catalog.delete_offer(pid, first.id)
    with pytest.raises(ConfigError, match="au moins une offre"):
        catalog.delete_offer(pid, second.id)
    assert len(catalog.offers(pid)) == 1


def test_store_policies_and_preview(catalog):
    catalog.set_store_policy("https://www.Ludum.fr/livraison", 490, 4000, [(1500, 390)], "relais")
    offer = catalog.preview_offer(B)
    assert offer.store == "Ludum"
    assert offer.shipping_for(1000, None) == (390, "boutique")
    assert offer.shipping_for(2000, None) == (490, "boutique")
    with pytest.raises(ConfigError):
        catalog.set_store_policy("pas un domaine", None, None)


# --- anti-SSRF -----------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.5", "192.168.1.10", "169.254.169.254", "[::1]", None])
def test_private_hosts_refused(host):
    with pytest.raises(FetchError):
        assert_public_host(host.strip("[]") if host else host)


# --- interface web -------------------------------------------------------------------------------

class FakeRunner:
    def __init__(self):
        self.triggered = []
        self.notifiers = []
        self.running = False
        self.last_run_ts = None
        self.next_run_ts = None

    def trigger(self, ids):
        self.triggered.append(list(ids))
        return True

    def is_pending(self, pid):
        return False


@pytest.fixture
def app_env(tmp_path):
    settings = Settings(db_path=tmp_path / "web.db", watchlist_path=tmp_path / "none.yaml")
    runner = FakeRunner()
    client = TestClient(create_app(settings, runner))
    return client, settings, runner


def token(client, path="/"):
    html = client.get(path).text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def test_security_headers_and_csrf(app_env):
    client, _, _ = app_env
    resp = client.get("/")
    assert resp.status_code == 200
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    # POST sans jeton → 403
    assert client.post("/products", data={"name": "X", "urls": A}, follow_redirects=False).status_code == 403
    # mauvaise origine → 403
    t = token(client)
    resp = client.post("/products", data={"name": "X", "urls": A, "csrf_token": t},
                       headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert resp.status_code == 403
    # API sans en-tête → 403 JSON
    assert client.post("/api/probe", json={"urls": [A]}).status_code == 403


def test_create_view_edit_delete_product(app_env):
    client, settings, runner = app_env
    t = token(client)
    resp = client.post("/products", data={"name": "Azul <b>", "target_price": "32", "urls": f"{A}\n{B}",
                                          "check_now": "1", "csrf_token": t}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/products/azul-b")
    assert runner.triggered == [["azul-b"]]

    # relevés pour alimenter le tableau de bord et le graphique
    store = Store(settings.db_path)
    now = int(time.time())
    for days, price in ((3, 4500), (2, 4200), (0, 3900)):
        store.record_observation("azul-b", A, "Philibert", now - days * 86400, price, price + 195, True, "jsonld", "boutique")
    store.close()

    page = client.get("/").text
    assert "Azul &lt;b&gt;" in page and "<b>" not in page.split("<main>")[1].split("</main>")[0].replace("<b>", "", 0) \
        or "Azul &lt;b&gt;" in page  # autoescape
    assert "40,95" in page  # 39,00 + 1,95

    detail = client.get("/products/azul-b").text
    assert 'class="chart"' in detail and "data-tip=" in detail
    assert "40,95" in detail

    resp = client.post("/products/azul-b/edit", data={"name": "Azul", "target_price": "35", "enabled": "1",
                                                      "rule_drop_pct": "20", "rule_require_in_stock": "",
                                                      "csrf_token": t}, follow_redirects=False)
    assert resp.status_code == 303 and "ok=" in resp.headers["location"]
    resp = client.post("/products/azul-b/edit", data={"name": "Azul", "rule_drop_pct": "beaucoup", "csrf_token": t},
                       follow_redirects=False)
    assert "err=" in resp.headers["location"]

    resp = client.post("/products/azul-b/delete", data={"purge": "1", "csrf_token": t}, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/products/azul-b").status_code == 404


def test_create_validation_error_rerenders_form(app_env):
    client, _, _ = app_env
    t = token(client)
    resp = client.post("/products", data={"name": "X", "urls": "pas-une-url", "csrf_token": t})
    assert resp.status_code == 400 and "URL invalide" in resp.text and "pas-une-url" in resp.text


def test_stores_settings_export_import(app_env):
    client, _, _ = app_env
    t = token(client)
    resp = client.post("/stores", data={"domain": "ludum.fr", "cost": "4,90", "free_above": "40",
                                        "tiers": "15:3,90 ; 30:3,20", "note": "relais", "csrf_token": t},
                       follow_redirects=False)
    assert "ok=" in resp.headers["location"]
    page = client.get("/stores").text
    assert "ludum.fr" in page and "4,90" in page
    assert "err=" in client.post("/stores", data={"domain": "ludum.fr", "tiers": "n'importe quoi", "csrf_token": t},
                                 follow_redirects=False).headers["location"]

    resp = client.post("/settings", data={"rule_tolerance_pct": "7", "rule_require_in_stock": "true",
                                          "default_shipping": "5", "csrf_token": t}, follow_redirects=False)
    assert "ok=" in resp.headers["location"]
    assert 'value="7.0"' in client.get("/settings").text

    exported = client.get("/export.yaml")
    assert exported.status_code == 200 and "ludum.fr" in exported.text
    example = (ROOT / "watchlist.example.yaml").read_bytes()
    resp = client.post("/import", data={"csrf_token": t}, files={"file": ("w.yaml", example, "text/yaml")},
                       follow_redirects=False)
    assert "ok=" in resp.headers["location"]
    assert "Azul Duel" in client.get("/").text
    resp = client.post("/import", data={"csrf_token": t}, files={"file": ("w.yaml", b"products: [{id: x}]", "text/yaml")},
                       follow_redirects=False)
    assert "err=" in resp.headers["location"]
    assert "Azul Duel" in client.get("/").text  # inchangé


def test_probe_refuses_private_urls(app_env):
    client, _, _ = app_env
    t = token(client)
    resp = client.post("/api/probe", json={"urls": ["http://127.0.0.1:8080/healthz"]}, headers={"X-CSRF-Token": t})
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["ok"] is False and "non publique" in result["error"]
