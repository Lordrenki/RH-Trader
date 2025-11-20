"""SQLite persistence layer for the trading bot."""
from __future__ import annotations

import asyncio
import os
from typing import Iterable, List, Tuple

import aiosqlite


class Database:
    """Data access helper built on top of SQLite."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        async with self._connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    contact TEXT DEFAULT '',
                    rating_total INTEGER DEFAULT 0,
                    rating_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS inventories (
                    user_id INTEGER NOT NULL,
                    item TEXT NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    PRIMARY KEY (user_id, item)
                );

                CREATE TABLE IF NOT EXISTS offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    item TEXT NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    details TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    item TEXT NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    details TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS wishlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    item TEXT NOT NULL,
                    note TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    item TEXT NOT NULL,
                    status TEXT DEFAULT 'open'
                );
                """
            )
            await db.commit()

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    async def ensure_user(self, user_id: int) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,)
                )
                await db.commit()

    async def set_contact(self, user_id: int, contact: str) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "UPDATE users SET contact = ? WHERE user_id = ?",
                    (contact.strip(), user_id),
                )
                await db.commit()

    async def add_stock(self, user_id: int, item: str, quantity: int) -> None:
        await self.ensure_user(user_id)
        item = item.strip()
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT INTO inventories(user_id, item, quantity) VALUES (?, ?, ?)\n"
                    "ON CONFLICT(user_id, item) DO UPDATE SET quantity = quantity + excluded.quantity",
                    (user_id, item, quantity),
                )
                await db.commit()

    async def remove_stock(self, user_id: int, item: str) -> bool:
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "DELETE FROM inventories WHERE user_id = ? AND item = ?",
                    (user_id, item.strip()),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def clear_stock(self, user_id: int) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute("DELETE FROM inventories WHERE user_id = ?", (user_id,))
                await db.commit()

    async def get_stock(self, user_id: int) -> List[Tuple[str, int]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT item, quantity FROM inventories WHERE user_id = ? ORDER BY item",
                (user_id,),
            )
            return await cursor.fetchall()

    async def add_offer(self, user_id: int, item: str, quantity: int, details: str) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT INTO offers(user_id, item, quantity, details) VALUES (?, ?, ?, ?)",
                    (user_id, item.strip(), quantity, details.strip()),
                )
                await db.commit()

    async def add_request(self, user_id: int, item: str, quantity: int, details: str) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT INTO requests(user_id, item, quantity, details) VALUES (?, ?, ?, ?)",
                    (user_id, item.strip(), quantity, details.strip()),
                )
                await db.commit()

    async def list_offers(self, limit: int = 10) -> List[Tuple[int, str, int, str]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT user_id, item, quantity, details FROM offers ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return await cursor.fetchall()

    async def list_requests(self, limit: int = 10) -> List[Tuple[int, str, int, str]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT user_id, item, quantity, details FROM requests ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return await cursor.fetchall()

    async def search_stock(self, term: str) -> List[Tuple[int, str, int]]:
        like = f"%{term.strip()}%"
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT user_id, item, quantity FROM inventories WHERE item LIKE ? ORDER BY item",
                (like,),
            )
            return await cursor.fetchall()

    async def add_wishlist(self, user_id: int, item: str, note: str) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT INTO wishlist(user_id, item, note) VALUES (?, ?, ?)",
                    (user_id, item.strip(), note.strip()),
                )
                await db.commit()

    async def remove_wishlist(self, user_id: int, item: str) -> bool:
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "DELETE FROM wishlist WHERE user_id = ? AND item = ?",
                    (user_id, item.strip()),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def get_wishlist(self, user_id: int) -> List[Tuple[str, str]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT item, note FROM wishlist WHERE user_id = ? ORDER BY item",
                (user_id,),
            )
            return await cursor.fetchall()

    async def record_rating(self, user_id: int, rating: int) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "UPDATE users SET rating_total = rating_total + ?, rating_count = rating_count + 1\n"
                    "WHERE user_id = ?",
                    (rating, user_id),
                )
                await db.commit()

    async def create_trade(self, user_id: int, partner_id: int, item: str) -> int:
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "INSERT INTO trades(user_id, partner_id, item, status) VALUES (?, ?, ?, 'open')",
                    (user_id, partner_id, item.strip()),
                )
                await db.commit()
                return cursor.lastrowid

    async def set_trade_status(
        self, trade_id: int, status: str, *, create_if_missing: Tuple[int, int, str] | None = None
    ) -> None:
        async with self._lock:
            async with self._connect() as db:
                if create_if_missing:
                    user_id, partner_id, item = create_if_missing
                    await db.execute(
                        "INSERT INTO trades(id, user_id, partner_id, item, status) VALUES (?, ?, ?, ?, ?)\n"
                        "ON CONFLICT(id) DO UPDATE SET status = excluded.status",
                        (trade_id, user_id, partner_id, item, status),
                    )
                else:
                    await db.execute(
                        "UPDATE trades SET status = ? WHERE id = ?",
                        (status, trade_id),
                    )
                await db.commit()

    async def leaderboard(self, limit: int = 10) -> List[Tuple[int, float, int]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT user_id,\n"
                "CASE WHEN rating_count = 0 THEN 0 ELSE CAST(rating_total AS FLOAT) / rating_count END AS score,\n"
                "rating_count\n"
                "FROM users ORDER BY score DESC, rating_count DESC LIMIT ?",
                (limit,),
            )
            return await cursor.fetchall()

    async def profile(self, user_id: int) -> Tuple[str, float, int]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT contact,\n"
                "CASE WHEN rating_count = 0 THEN 0 ELSE CAST(rating_total AS FLOAT) / rating_count END AS score,\n"
                "rating_count FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return row or ("", 0.0, 0)

    async def clear_history(self, table: str, user_id: int) -> None:
        if table not in {"offers", "requests"}:
            raise ValueError("Invalid table name for clearing history")
        async with self._lock:
            async with self._connect() as db:
                await db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                await db.commit()

    async def dump_state(self) -> Iterable[Tuple[str, Tuple]]:
        """Used for debugging and tests to inspect stored state."""
        async with self._connect() as db:
            for table in ["users", "inventories", "offers", "requests", "wishlist", "trades"]:
                cursor = await db.execute(f"SELECT * FROM {table}")
                for row in await cursor.fetchall():
                    yield table, row
