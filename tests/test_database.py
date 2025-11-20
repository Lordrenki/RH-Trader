import asyncio
from pathlib import Path

import pytest

from rh_trader.database import Database

pytestmark = pytest.mark.asyncio


async def init_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    await db.setup()
    return db


async def test_stock_crud(tmp_path: Path):
    db = await init_db(tmp_path)
    await db.add_stock(1, "Widget", 2)
    await db.add_stock(1, "Widget", 1)
    await db.add_stock(1, "Gadget", 3)

    items = await db.get_stock(1)
    assert ("Gadget", 3) in items
    assert ("Widget", 3) in items

    removed = await db.remove_stock(1, "Widget")
    assert removed
    items = await db.get_stock(1)
    assert ("Widget", 3) not in items

    await db.clear_stock(1)
    assert await db.get_stock(1) == []


async def test_wishlist(tmp_path: Path):
    db = await init_db(tmp_path)
    await db.add_wishlist(1, "Blue Shell", "Paying top price")
    await db.add_wishlist(1, "Red Shell", "")
    entries = await db.get_wishlist(1)
    assert ("Blue Shell", "Paying top price") in entries

    removed = await db.remove_wishlist(1, "Blue Shell")
    assert removed
    entries = await db.get_wishlist(1)
    assert all(item != "Blue Shell" for item, _ in entries)


async def test_ratings_and_leaderboard(tmp_path: Path):
    db = await init_db(tmp_path)
    await db.record_rating(1, 5)
    await db.record_rating(1, 3)
    await db.record_rating(2, 4)

    leaderboard = await db.leaderboard()
    assert leaderboard[0][0] == 1  # User 1 should have higher average
    contact, score, count = await db.profile(1)
    assert score == pytest.approx(4.0)
    assert count == 2


async def test_offers_and_requests(tmp_path: Path):
    db = await init_db(tmp_path)
    await db.add_offer(1, "Widget", 2, "Great price")
    await db.add_request(2, "Widget", 1, "Urgent")

    offers = await db.list_offers()
    requests = await db.list_requests()

    assert any(o[1] == "Widget" for o in offers)
    assert any(r[1] == "Widget" for r in requests)


async def test_trade_status(tmp_path: Path):
    db = await init_db(tmp_path)
    await db.set_trade_status(10, "completed", create_if_missing=(1, 2, "Widget"))
    await db.set_trade_status(10, "archived")

    rows = [row for table, row in [entry async for entry in db.dump_state()] if table == "trades"]
    assert rows[0][0] == 10
    assert rows[0][-1] == "archived"
