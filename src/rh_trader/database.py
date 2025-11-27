"""SQLite persistence layer for the trading bot."""
from __future__ import annotations

import asyncio
import os
from typing import Iterable, List, Optional, Tuple

import aiosqlite
from rapidfuzz import fuzz


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
                    status TEXT DEFAULT 'open',
                    seller_id INTEGER,
                    buyer_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS trade_feedback (
                    trade_id INTEGER NOT NULL,
                    rater_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    PRIMARY KEY (trade_id, rater_id)
                );

                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    trade_channel_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS trade_posts (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS active_trades (
                    user_id INTEGER PRIMARY KEY,
                    trade_id INTEGER NOT NULL
                );
                """
            )
            await db.commit()
            await self._ensure_trade_columns(db)

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    async def _ensure_trade_columns(self, db: aiosqlite.Connection) -> None:
        """Add new trade columns when upgrading an existing database."""
        cursor = await db.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "seller_id" not in columns:
            await db.execute("ALTER TABLE trades ADD COLUMN seller_id INTEGER")
        if "buyer_id" not in columns:
            await db.execute("ALTER TABLE trades ADD COLUMN buyer_id INTEGER")
        await db.commit()

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
        """Search community inventories for fuzzy matches on item names."""

        term = term.strip()
        if not term:
            return []

        normalized_term = self._normalize_text(term)
        async with self._connect() as db:
            cursor = await db.execute("SELECT user_id, item, quantity FROM inventories")
            rows = await cursor.fetchall()

        scored = []
        for row in rows:
            user_id, item, quantity = row
            score = fuzz.WRatio(normalized_term, self._normalize_text(item))
            if score >= 60:
                scored.append((score, user_id, item, quantity))

        scored.sort(key=lambda entry: (-entry[0], entry[2].lower(), entry[1]))
        return [(user_id, item, quantity) for _, user_id, item, quantity in scored[:20]]

    async def search_wishlist(self, term: str) -> List[Tuple[int, str, str]]:
        """Search wishlist entries for fuzzy matches on item names."""

        term = term.strip()
        if not term:
            return []

        normalized_term = self._normalize_text(term)
        async with self._connect() as db:
            cursor = await db.execute("SELECT user_id, item, note FROM wishlist")
            rows = await cursor.fetchall()

        scored = []
        for row in rows:
            user_id, item, note = row
            score = fuzz.WRatio(normalized_term, self._normalize_text(item))
            if score >= 60:
                scored.append((score, user_id, item, note))

        scored.sort(key=lambda entry: (-entry[0], entry[2].lower(), entry[1]))
        return [(user_id, item, note) for _, user_id, item, note in scored[:20]]

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(value.lower().split())

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

    async def create_trade(self, seller_id: int, buyer_id: int, item: str) -> int:
        await self.ensure_user(seller_id)
        await self.ensure_user(buyer_id)
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "INSERT INTO trades(user_id, partner_id, item, status, seller_id, buyer_id)\n"
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (seller_id, buyer_id, item.strip(), seller_id, buyer_id),
                )
                await db.commit()
                return cursor.lastrowid

    async def accept_trade(self, trade_id: int, seller_id: int) -> bool:
        """Mark a pending trade as accepted by the seller."""

        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "UPDATE trades SET status = 'open' WHERE id = ? AND seller_id = ? AND status = 'pending'",
                    (trade_id, seller_id),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def reject_trade(self, trade_id: int, seller_id: int) -> bool:
        """Allow the seller to reject a pending trade offer."""

        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "UPDATE trades SET status = 'rejected' WHERE id = ? AND seller_id = ? AND status = 'pending'",
                    (trade_id, seller_id),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def set_active_trade(self, user_id: int, trade_id: int) -> bool:
        """Mark a trade as the active DM context for a participant."""

        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT status FROM trades WHERE id = ? AND (seller_id = ? OR buyer_id = ?)",
                    (trade_id, user_id, user_id),
                )
                row = await cursor.fetchone()
                if row is None or row[0] != "open":
                    return False

                await db.execute(
                    "INSERT INTO active_trades(user_id, trade_id) VALUES (?, ?)\n"
                    "ON CONFLICT(user_id) DO UPDATE SET trade_id = excluded.trade_id",
                    (user_id, trade_id),
                )
                await db.commit()
                return True

    async def clear_active_trade(self, user_id: int, trade_id: Optional[int] = None) -> None:
        async with self._lock:
            async with self._connect() as db:
                if trade_id is None:
                    await db.execute("DELETE FROM active_trades WHERE user_id = ?", (user_id,))
                else:
                    await db.execute(
                        "DELETE FROM active_trades WHERE user_id = ? AND trade_id = ?",
                        (user_id, trade_id),
                    )
                await db.commit()

    async def delete_trade(self, trade_id: int) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
                await db.execute("DELETE FROM trade_feedback WHERE trade_id = ?", (trade_id,))
                await db.commit()

    async def complete_trade(self, trade_id: int) -> bool:
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "UPDATE trades SET status = 'completed' WHERE id = ? AND status = 'open'",
                    (trade_id,),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def cancel_trade(self, trade_id: int) -> bool:
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "UPDATE trades SET status = 'cancelled' WHERE id = ? AND status IN ('open', 'pending')",
                    (trade_id,),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def get_trade(self, trade_id: int) -> Tuple[int, int, int, str, str] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, seller_id, buyer_id, item, status FROM trades WHERE id = ?",
                (trade_id,),
            )
            return await cursor.fetchone()

    async def get_active_trade_for_user(
        self, user_id: int
    ) -> Tuple[int, int, int, str, str] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT t.id, t.seller_id, t.buyer_id, t.item, t.status\n"
                "FROM active_trades a JOIN trades t ON a.trade_id = t.id\n"
                "WHERE a.user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        trade_id, seller_id, buyer_id, item, status = row
        if status != "open" or user_id not in (seller_id, buyer_id):
            await self.clear_active_trade(user_id, trade_id)
            return None
        return row

    async def latest_open_trade_for_user(self, user_id: int) -> Tuple[int, int, int, str, str] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, seller_id, buyer_id, item, status FROM trades\n"
                "WHERE status = 'open' AND (seller_id = ? OR buyer_id = ?)\n"
                "ORDER BY id DESC LIMIT 1",
                (user_id, user_id),
            )
            return await cursor.fetchone()

    async def list_open_trades_for_user(
        self, user_id: int
    ) -> List[Tuple[int, int, int, str, str]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, seller_id, buyer_id, item, status FROM trades\n"
                "WHERE status = 'open' AND (seller_id = ? OR buyer_id = ?) ORDER BY id DESC",
                (user_id, user_id),
            )
            return await cursor.fetchall()

    async def record_trade_rating(
        self, trade_id: int, rater_id: int, partner_id: int, score: int, role: str
    ) -> bool:
        if score < 1 or score > 5:
            raise ValueError("Score must be between 1 and 5")
        await self.ensure_user(rater_id)
        await self.ensure_user(partner_id)
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO trade_feedback(trade_id, rater_id, partner_id, role, score)\n"
                    "VALUES (?, ?, ?, ?, ?)",
                    (trade_id, rater_id, partner_id, role, score),
                )
                if cursor.rowcount == 0:
                    await db.commit()
                    return False
                await db.execute(
                    "UPDATE users SET rating_total = rating_total + ?, rating_count = rating_count + 1\n"
                    "WHERE user_id = ?",
                    (score, partner_id),
                )
                await db.commit()
                return True

    async def set_trade_status(
        self, trade_id: int, status: str, *, create_if_missing: Tuple[int, int, str] | None = None
    ) -> None:
        async with self._lock:
            async with self._connect() as db:
                if create_if_missing:
                    user_id, partner_id, item = create_if_missing
                    await db.execute(
                        "INSERT INTO trades(id, user_id, partner_id, item, status, seller_id, buyer_id) VALUES (?, ?, ?, ?, ?, ?, ?)\n"
                        "ON CONFLICT(id) DO UPDATE SET status = excluded.status",
                        (trade_id, user_id, partner_id, item, status, user_id, partner_id),
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

    async def trade_count(self, user_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM trades WHERE seller_id = ? OR buyer_id = ?",
                (user_id, user_id),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def clear_history(self, table: str, user_id: int) -> None:
        if table not in {"offers", "requests"}:
            raise ValueError("Invalid table name for clearing history")
        async with self._lock:
            async with self._connect() as db:
                await db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                await db.commit()

    async def get_trade_post(self, guild_id: int, user_id: int) -> Tuple[int, int] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT channel_id, message_id FROM trade_posts WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            return await cursor.fetchone()

    async def save_trade_post(self, guild_id: int, user_id: int, channel_id: int, message_id: int) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT INTO trade_posts(guild_id, user_id, channel_id, message_id) VALUES (?, ?, ?, ?)\n"
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET channel_id = excluded.channel_id, message_id = excluded.message_id",
                    (guild_id, user_id, channel_id, message_id),
                )
                await db.commit()

    async def delete_trade_post(self, guild_id: int, user_id: int) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "DELETE FROM trade_posts WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
                await db.commit()

    async def dump_state(self) -> Iterable[Tuple[str, Tuple]]:
        """Used for debugging and tests to inspect stored state."""
        async with self._connect() as db:
            for table in [
                "users",
                "inventories",
                "offers",
                "requests",
                "wishlist",
                "trades",
                "trade_feedback",
                "guild_settings",
            ]:
                cursor = await db.execute(f"SELECT * FROM {table}")
                for row in await cursor.fetchall():
                    yield table, row

    async def set_trade_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        """Persist the configured trade post channel for a guild."""

        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT INTO guild_settings(guild_id, trade_channel_id) VALUES (?, ?)\n"
                    "ON CONFLICT(guild_id) DO UPDATE SET trade_channel_id = excluded.trade_channel_id",
                    (guild_id, channel_id),
                )
                await db.commit()

    async def get_trade_channel(self, guild_id: int) -> Optional[int]:
        """Return the configured trade post channel, if present."""

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT trade_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None
