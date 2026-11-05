import pytest

from dealarr.extract import extract, looks_blocked
from dealarr.money import fmt, parse_price
from dealarr.sites import amazon_asin, canonical_url, profile_for, store_name


@pytest.mark.parametrize("raw, cents", [
    ("29,99 €", 2999), ("29.99", 2999), (29.99, 2999), (30, 3000), ("1 299,00 €", 129900),
    ("1 299,90 €", 129990), ("1.299,90", 129990), ("1,299.90", 129990), ("29€99", 2999),
    ("Prix : 45 €", 4500), ("", None), ("gratuit", None), ("0,00", None), (None, None), (True, None),
])
def test_parse_price(raw, cents):
    assert parse_price(raw) == cents


def test_fmt():
    assert fmt(2999) == "29,99 €"
    assert fmt(129990) == "1 299,90 €"
    assert fmt(None) == "—"


JSONLD = """<html><head><title>Arcs - Boutique</title>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"BreadcrumbList"},
 {"@type":"Product","name":"Arcs","offers":[
   {"@type":"Offer","price":"42.90","priceCurrency":"EUR","availability":"https://schema.org/OutOfStock"},
   {"@type":"Offer","price":"44.50","priceCurrency":"EUR","availability":"https://schema.org/InStock"}
 ]}]}</script></head><body></body></html>"""


def test_jsonld_prefers_available_offer():
    ex = extract(JSONLD)
    assert (ex.price_cents, ex.in_stock, ex.title, ex.method) == (4450, True, "Arcs", "jsonld")


def test_jsonld_aggregate_and_pricespec():
    html = """<script type="application/ld+json">{"@type":["Product"],"name":"X",
      "offers":{"@type":"AggregateOffer","lowPrice":"19,90","priceCurrency":"EUR"}}</script>"""
    assert extract(html).price_cents == 1990
    html = """<script type="application/ld+json">{"@type":"Product","name":"X",
      "offers":{"@type":"Offer","priceSpecification":{"price":12.5,"priceCurrency":"EUR"}}}</script>"""
    assert extract(html).price_cents == 1250


def test_jsonld_ignores_other_currency_and_broken_json():
    html = """<script type="application/ld+json">{broken</script>
    <script type="application/ld+json">{"@type":"Product","offers":{"price":"10","priceCurrency":"USD"}}</script>"""
    assert extract(html).price_cents is None


def test_microdata_fallback():
    html = """<html><body><div itemscope itemtype="https://schema.org/Product">
      <span itemprop="price" content="27.50">27,50 €</span>
      <link itemprop="availability" href="https://schema.org/InStock"></div></body></html>"""
    ex = extract(html)
    assert (ex.price_cents, ex.in_stock, ex.method) == (2750, True, "microdata")


def test_explicit_selector_wins():
    html = JSONLD.replace("<body></body>", '<body><span class="promo">39,00 €</span></body>')
    assert extract(html, selector=".promo").price_cents == 3900


def test_amazon_profile():
    html = """<html><body><span id="productTitle"> Dixit  </span>
      <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">24,99&nbsp;€</span></span></div>
      <div id="availability"><span>En stock</span></div></body></html>"""
    ex = extract(html, profile_for("https://www.amazon.fr/dp/B000000001"))
    assert (ex.price_cents, ex.in_stock, ex.title, ex.method) == (2499, True, "Dixit", "profile")
    oos = html.replace("En stock", "Actuellement indisponible.")
    assert extract(oos, profile_for("https://www.amazon.fr/dp/B000000001")).in_stock is False


def test_prefer_selectors_ignores_related_products():
    html = """<html><body>
      <div class="related"><span itemprop="price" content="13.5">13,50 €</span></div>
      <span class="nq-c-ProductPrices-current">46,00 €</span>
      <link itemprop="availability" href="https://schema.org/InStock"></body></html>"""
    ex = extract(html, profile_for("https://www.le-passe-temps.com/abstrait-tactique/5960-azul.html"))
    assert (ex.price_cents, ex.in_stock, ex.method) == (4600, True, "profile")


def test_title_html_entities_unescaped():
    html = """<script type="application/ld+json">{"@type":"Product","name":"Ada&#039;s Library",
      "offers":{"price":"24","priceCurrency":"EUR"}}</script>"""
    assert extract(html).title == "Ada's Library"


def test_jsonld_shipping_details():
    html = """<script type="application/ld+json">{"@type":"Product","name":"X","offers":{"price":"41.50",
      "priceCurrency":"EUR","shippingDetails":[
        {"@type":"OfferShippingDetails","shippingRate":{"value":"9.90","currency":"EUR"},
         "shippingDestination":{"addressCountry":"BE"}},
        {"@type":"OfferShippingDetails","shippingRate":{"value":"4.22","currency":"EUR"},
         "shippingDestination":{"addressCountry":"FR"}},
        {"@type":"OfferShippingDetails","shippingRate":{"value":"6.50","currency":"EUR"}}]}}</script>"""
    assert extract(html).shipping_cents == 422
    free = html.replace('"4.22"', '"0"')
    assert extract(free).shipping_cents == 0
    no_rate = """<script type="application/ld+json">{"@type":"Product","offers":{"price":"10","priceCurrency":"EUR",
      "shippingDetails":{"shippingDestination":{"addressCountry":"FR"}}}}</script>"""
    assert extract(no_rate).shipping_cents is None


def test_amazon_shipping_attribute():
    tpl = """<span id="productTitle">X</span><div id="corePrice_feature_div"><span class="a-price">
      <span class="a-offscreen">24,99 €</span></span></div>
      <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
      <span data-csa-c-delivery-price="{v}">Livraison {v} mercredi 11 novembre</span></div>"""
    profile = profile_for("https://www.amazon.fr/dp/B000000001")
    assert extract(tpl.replace("{v}", "GRATUITE"), profile).shipping_cents == 0
    assert extract(tpl.replace("{v}", "4,99 €"), profile).shipping_cents == 499
    assert extract(tpl.replace("{v}", "GRATUITE").replace("mir-layout", "other"), profile).shipping_cents is None


def test_amazon_urls():
    url = "https://www.amazon.fr/Asmodee-Dixit/dp/b07ab1c2d3/ref=sr_1_1?tag=xyz"
    assert amazon_asin(url) == "B07AB1C2D3"
    assert canonical_url(url) == "https://www.amazon.fr/dp/B07AB1C2D3"
    assert canonical_url("https://www.philibertnet.com/fr/x.html?utm=1") == "https://www.philibertnet.com/fr/x.html?utm=1"
    assert store_name("https://www.philibertnet.com/fr/x.html") == "Philibert"
    assert store_name("https://boutique.inconnue.fr/p") == "boutique.inconnue.fr"


def test_blocked_detection():
    assert looks_blocked('<script src="https://ct.captcha-delivery.com/c.js"></script>')
    assert not looks_blocked("<html>ok</html>")
