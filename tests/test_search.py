from dealarr import search as S
from dealarr.config import Offer
from dealarr.extract import extract, normalize_ean
from dealarr.search import Candidate, StoreSearch, _group, _store_links


def test_normalize_ean():
    assert normalize_ean("0826956620105") == normalize_ean("826956620105") == "826956620105"
    assert normalize_ean("12") is None and normalize_ean(None) is None


def test_ean_sources():
    jsonld = '<script type="application/ld+json">{"@type":"Product","name":"X","gtin13":"0826956620105","offers":{"price":"10","priceCurrency":"EUR"}}</script>'
    assert extract(jsonld).ean == "826956620105"
    embedded = '<h1>Azul</h1><span itemprop="price" content="41.5"></span><script>var p={&quot;ean13&quot;:&quot;826956620105&quot;}</script>'
    ex = extract(embedded)
    assert (ex.ean, ex.title) == ("826956620105", "Azul")
    in_url = '<span itemprop="price" content="25.10"></span>'
    assert extract(in_url, url="https://www.lepion.com/jeux-a-deux/2976-azul-duel-0826956611608.html").ean == "826956611608"


class FakeFetcher:
    def __init__(self, html):
        self.html = html

    def get(self, url, browser=False):
        return self.html


STORE = StoreSearch("ludum.fr", "Ludum", "https://www.ludum.fr/search?s={q}", r"https://www\.ludum\.fr/[a-z0-9-]+/[a-z0-9-]+-\d+")


def test_store_links_json_relative_and_filtering():
    html = (r'{"r":"<a href=\\"https:\/\/www.ludum.fr\/famille\/azul-193\\">"}'
            '<a href="/famille/azul-duel-9121?x=1">d</a><a href="/famille/catan-12">c</a>'
            '<a href="https://www.ludum.fr/famille/azul-193#reviews">dup</a>')
    links = _store_links(FakeFetcher(html), STORE, "Azul", strict=True)
    assert links == ["https://www.ludum.fr/famille/azul-193", "https://www.ludum.fr/famille/azul-duel-9121"]
    assert "https://www.ludum.fr/famille/catan-12" in _store_links(FakeFetcher(html), STORE, "x", strict=False)


def test_grouping_by_ean_then_title():
    cands = [
        Candidate("A", "https://a/1", "Azul", 4000, ean="826956620105"),
        Candidate("B", "https://b/1", "Azul - Jeu de société", 3900, ean="826956620105"),
        Candidate("C", "https://c/1", "Azul", 4100, ean=None),  # rattaché par titre
        Candidate("A", "https://a/2", "Azul Duel", 2500, ean="826956611608"),
        Candidate("D", "https://d/1", "Azul", None, error="HTTP 403"),  # ignoré
    ]
    groups = _group(cands, "Azul")
    first = groups[0]
    assert first.ean == "826956620105" and first.title == "Azul" and first.fuzzy
    assert {c.store for c in first.candidates} == {"A", "B", "C"}
    assert first.best.store == "B"
    assert groups[1].ean == "826956611608"


def test_search_marks_target_and_retries_by_ean(monkeypatch):
    calls = []

    def fake_store(store, query, strict, offer_for, browser):
        calls.append((store.name, query, strict))
        if store.name == "Philibert":
            return [Candidate("Philibert", "https://p/azul", "Azul", 4150, ean="826956620105")], None
        if store.name == "E.Leclerc" and not strict:
            return [Candidate("E.Leclerc", "https://l/azul", "Azul", 3516, ean="826956620105")], None
        return [], None

    monkeypatch.setattr(S, "_search_store", fake_store)
    result = S.search("Azul", lambda url: Offer(url, "x"), ean="0826956620105")
    assert result.target_ean == "826956620105"
    assert {c.store for c in result.groups[0].candidates} == {"Philibert", "E.Leclerc"}
    assert ("E.Leclerc", "0826956620105", False) in calls  # 2e passe par code-barres


def test_lego_mode_detection_and_filter(monkeypatch):
    assert S.is_set_number("75313") and S.is_set_number(" 10348 ")
    assert not S.is_set_number("0826956620105") and not S.is_set_number("Azul")
    assert S._is_lego_set("https://www.e.leclerc/fp/lego-botanicals-bonsai-10348-5702017814674", None, "10348")
    assert not S._is_lego_set("https://www.e.leclerc/fp/ernie-ball-10348-corde", "Corde Ernie Ball", "10348")
    assert not S._is_lego_set("https://x/lego-103480", None, "10348")

    def fake_store(store, query, strict, offer_for, browser, set_number=None):
        assert set_number == "10348"
        if store.name == "E.Leclerc":
            return [Candidate("E.Leclerc", "https://l/lego-bonsai-10348", "LEGO Bonsaï 10348", 4499, ean="5702017814674")], None
        return [Candidate("La Grande Récré", "https://g/bonsai-lego-10348.html", "Bonsaï LEGO 10348", 4999)], None

    monkeypatch.setattr(S, "_search_store", fake_store)
    result = S.search("10348", lambda url: Offer(url, "x"))
    assert result.set_number == "10348" and len(result.groups) == 1
    group = result.groups[0]
    assert group.best.store == "E.Leclerc" and len(group.candidates) == 2
    assert any(name == "LEGO.com" for name, _ in result.manual)
