import json

from rh_trader.raider_market import parse_browse_items


def test_parse_browse_items_from_next_data() -> None:
    payload = {
        "props": {
            "pageProps": {
                "items": [
                    {
                        "slug": "alpha",
                        "name": "Alpha Blaster",
                        "tradeValue": 1500,
                        "gameValue": 1200,
                    },
                    {
                        "slug": "beta",
                        "name": "Beta Shield",
                        "trade_value": "2,500",
                        "game_value": "1,900",
                    },
                ]
            }
        }
    }
    html = (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}"
        "</script>"
        "</body></html>"
    )

    items = parse_browse_items(html)

    assert items["alpha"].trade_value == 1500
    assert items["alpha"].game_value == 1200
    assert items["beta"].trade_value == 2500
    assert items["beta"].game_value == 1900


def test_parse_browse_items_from_links() -> None:
    html = (
        "<html><body>"
        '<a href="/item/gamma">Gamma Core Trade Value 4,000 Game Value 3,500</a>'
        "</body></html>"
    )

    items = parse_browse_items(html)

    assert "gamma" in items
    assert items["gamma"].trade_value == 4000
    assert items["gamma"].game_value == 3500


def test_parse_browse_items_uses_market_value_as_trade_value() -> None:
    html = (
        "<html><body>"
        '<a href="/item/hullcracker_blueprint">'
        "Common 110×Hullcracker Blueprint Blueprint "
        "Game Value5,000 Market Value550,000 View Details"
        "</a>"
        "</body></html>"
    )

    items = parse_browse_items(html)

    item = items["hullcracker_blueprint"]
    assert item.name == "Hullcracker Blueprint"
    assert item.trade_value == 550000
    assert item.game_value == 5000


def test_parse_browse_items_from_next_flight_payload() -> None:
    import json

    payload = (
        '<a href="/item/burletta_blueprint">'
        "Burletta Blueprint Game Value5,000 Market Value200,000"
        "</a>"
    )
    html = f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"

    items = parse_browse_items(html)

    assert items["burletta_blueprint"].trade_value == 200000
    assert items["burletta_blueprint"].game_value == 5000
