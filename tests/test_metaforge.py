from rh_trader.metaforge import build_price_embed_chunks, parse_blueprint_prices


def test_parse_blueprint_prices_from_json_script() -> None:
    html = '''
    <html><body>
      <script type="application/json">{"items":[{"name":"Alpha Blueprint","medianPrice":"12,345"},{"name":"Not It","medianPrice":"99"}]}</script>
    </body></html>
    '''
    items = parse_blueprint_prices(html)
    assert len(items) == 1
    assert items[0].name == "Alpha Blueprint"
    assert items[0].median_price == 12345


def test_build_price_embed_chunks() -> None:
    html = '<script type="application/json">{"items":[{"name":"A Blueprint","medianPrice":"100"},{"name":"B Blueprint","medianPrice":"90"}]}</script>'
    items = parse_blueprint_prices(html)
    chunks = build_price_embed_chunks(items, chunk_size=1)
    assert len(chunks) == 2
    assert "A Blueprint" in chunks[0]
