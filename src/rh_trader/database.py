"""SQLite persistence layer for the trading bot."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Iterable, List, Optional, Tuple

import aiosqlite
from rapidfuzz import fuzz


class Database:
    """Data access helper built on top of SQLite."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = asyncio.Lock()
        self.max_rep_level = 200
        self.rep_xp_per_positive = 10
        self.rep_xp_penalty = 2
        self.rep_xp_base = 10

    async def setup(self) -> None:
        async with self._connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    contact TEXT DEFAULT '',
                    rating_total INTEGER DEFAULT 0,
                    rating_count INTEGER DEFAULT 0,
                    response_total INTEGER DEFAULT 0,
                    response_count INTEGER DEFAULT 0,
                    rep_positive INTEGER DEFAULT 0,
                    rep_negative INTEGER DEFAULT 0,
                    timezone TEXT DEFAULT '',
                    bio TEXT DEFAULT '',
                    is_premium INTEGER DEFAULT 0
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
                    buyer_id INTEGER,
                    created_at INTEGER DEFAULT 0,
                    accepted_at INTEGER,
                    closed_at INTEGER,
                    response_recorded INTEGER DEFAULT 0,
                    thread_id INTEGER,
                    last_activity_at INTEGER,
                    inactivity_warning_sent INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS trade_feedback (
                    trade_id INTEGER NOT NULL,
                    rater_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    PRIMARY KEY (trade_id, rater_id)
                );

                CREATE TABLE IF NOT EXISTS trade_reviews (
                    trade_id INTEGER NOT NULL,
                    reviewer_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    review TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (trade_id, reviewer_id)
                );

                CREATE TABLE IF NOT EXISTS quick_ratings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rater_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quick_ratings_pair_time
                    ON quick_ratings(rater_id, target_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    trade_channel_id INTEGER,
                    trade_thread_channel_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS store_posts (
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

                CREATE TABLE IF NOT EXISTS alerts (
                    user_id INTEGER NOT NULL,
                    item TEXT NOT NULL,
                    PRIMARY KEY (user_id, item)
                );

                CREATE TABLE IF NOT EXISTS raidermarket_panels (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    watchlist TEXT DEFAULT '',
                    extra_message_ids TEXT DEFAULT ''
                );
                """
            )
            await db.commit()
            await self._ensure_trade_columns(db)
            await self._ensure_user_columns(db)
            await self._ensure_guild_settings_columns(db)
            await self._ensure_raidermarket_columns(db)

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
        if "created_at" not in columns:
            await db.execute("ALTER TABLE trades ADD COLUMN created_at INTEGER DEFAULT 0")
        if "accepted_at" not in columns:
            await db.execute("ALTER TABLE trades ADD COLUMN accepted_at INTEGER")
        if "closed_at" not in columns:
            await db.execute("ALTER TABLE trades ADD COLUMN closed_at INTEGER")
        if "response_recorded" not in columns:
            await db.execute(
                "ALTER TABLE trades ADD COLUMN response_recorded INTEGER DEFAULT 0"
            )
        if "thread_id" not in columns:
            await db.execute("ALTER TABLE trades ADD COLUMN thread_id INTEGER")
        if "last_activity_at" not in columns:
            await db.execute("ALTER TABLE trades ADD COLUMN last_activity_at INTEGER")
        if "inactivity_warning_sent" not in columns:
            await db.execute(
                "ALTER TABLE trades ADD COLUMN inactivity_warning_sent INTEGER DEFAULT 0"
            )
        await db.execute(
            "UPDATE trades SET last_activity_at = COALESCE(NULLIF(last_activity_at, 0), CASE WHEN created_at > 0 THEN created_at ELSE ? END)",
            (int(time.time()),),
        )
        await db.execute(
            "UPDATE trades SET inactivity_warning_sent = COALESCE(inactivity_warning_sent, 0)"
        )
        await db.commit()

    async def _ensure_user_columns(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "response_total" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN response_total INTEGER DEFAULT 0")
        if "response_count" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN response_count INTEGER DEFAULT 0")
        if "rep_positive" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN rep_positive INTEGER DEFAULT 0")
        if "rep_negative" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN rep_negative INTEGER DEFAULT 0")
        if "timezone" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT ''")
        if "bio" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")
        if "is_premium" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
        await db.commit()

    async def _ensure_guild_settings_columns(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(guild_settings)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "trade_channel_id" not in columns:
            await db.execute("ALTER TABLE guild_settings ADD COLUMN trade_channel_id INTEGER")
        if "trade_thread_channel_id" not in columns:
            await db.execute(
                "ALTER TABLE guild_settings ADD COLUMN trade_thread_channel_id INTEGER"
            )

    async def _ensure_raidermarket_columns(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(raidermarket_panels)")
        columns = {row[1] for row in await cursor.fetchall()}
        if not columns:
            return
        if "watchlist" not in columns:
            await db.execute(
                "ALTER TABLE raidermarket_panels ADD COLUMN watchlist TEXT DEFAULT ''"
            )
        if "extra_message_ids" not in columns:
            await db.execute(
                "ALTER TABLE raidermarket_panels ADD COLUMN extra_message_ids TEXT DEFAULT ''"
            )
        await db.commit()

    async def upsert_raidermarket_panel(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        watchlist: List[str],
        extra_message_ids: List[int] | None = None,
    ) -> None:
        payload = json.dumps(watchlist)
        extra_payload = json.dumps(extra_message_ids or [])
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO raidermarket_panels (
                    guild_id,
                    channel_id,
                    message_id,
                    watchlist,
                    extra_message_ids
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    watchlist = excluded.watchlist,
                    extra_message_ids = excluded.extra_message_ids
                """,
                (guild_id, channel_id, message_id, payload, extra_payload),
            )
            await db.commit()

    async def update_raidermarket_watchlist(
        self, guild_id: int, watchlist: List[str]
    ) -> None:
        payload = json.dumps(watchlist)
        async with self._connect() as db:
            await db.execute(
                "UPDATE raidermarket_panels SET watchlist = ? WHERE guild_id = ?",
                (payload, guild_id),
            )
            await db.commit()

    async def get_raidermarket_panel(
        self, guild_id: int
    ) -> Optional[Tuple[int, int, int, List[str], List[int]]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT guild_id, channel_id, message_id, watchlist, extra_message_ids
                FROM raidermarket_panels
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        watchlist = json.loads(row[3]) if row[3] else []
        extra_ids = json.loads(row[4]) if row[4] else []
        return row[0], row[1], row[2], watchlist, extra_ids

    async def list_raidermarket_panels(
        self,
    ) -> List[Tuple[int, int, int, List[str], List[int]]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT guild_id, channel_id, message_id, watchlist, extra_message_ids
                FROM raidermarket_panels
                """
            )
            rows = await cursor.fetchall()
        results: List[Tuple[int, int, int, List[str], List[int]]] = []
        for guild_id, channel_id, message_id, watchlist, extra_message_ids in rows:
            parsed = json.loads(watchlist) if watchlist else []
            extra_ids = json.loads(extra_message_ids) if extra_message_ids else []
            results.append((guild_id, channel_id, message_id, parsed, extra_ids))
        return results

    async def update_raidermarket_panel_messages(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        extra_message_ids: List[int],
    ) -> None:
        payload = json.dumps(extra_message_ids)
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE raidermarket_panels
                SET channel_id = ?, message_id = ?, extra_message_ids = ?
                WHERE guild_id = ?
                """,
                (channel_id, message_id, payload, guild_id),
            )
            await db.commit()

    async def clear_raidermarket_panel(self, guild_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM raidermarket_panels WHERE guild_id = ?", (guild_id,)
            )
            await db.commit()

    async def ensure_user(self, user_id: int) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,)
                )
                await db.commit()

    async def list_known_users(self) -> List[int]:
        """Return all users that have interacted with the bot."""

        async with self._connect() as db:
            cursor = await db.execute("SELECT user_id FROM users ORDER BY user_id")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

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

    async def update_stock_quantity(self, user_id: int, item: str, quantity: int) -> bool:
        """Set the quantity for a stock item, removing it when the count hits zero."""

        await self.ensure_user(user_id)
        item = item.strip()
        async with self._lock:
            async with self._connect() as db:
                if quantity <= 0:
                    cursor = await db.execute(
                        "DELETE FROM inventories WHERE user_id = ? AND item = ?",
                        (user_id, item),
                    )
                    await db.commit()
                    return cursor.rowcount > 0

                await db.execute(
                    "INSERT INTO inventories(user_id, item, quantity) VALUES (?, ?, ?)\n"
                    "ON CONFLICT(user_id, item) DO UPDATE SET quantity = excluded.quantity",
                    (user_id, item, quantity),
                )
                await db.commit()
                return True

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

    async def add_alert(self, user_id: int, item: str) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO alerts(user_id, item) VALUES (?, ?)",
                    (user_id, item.strip()),
                )
                await db.commit()

    async def remove_alert(self, user_id: int, item: str) -> bool:
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "DELETE FROM alerts WHERE user_id = ? AND item = ?",
                    (user_id, item.strip()),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def get_alerts(self, user_id: int) -> List[str]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT item FROM alerts WHERE user_id = ? ORDER BY item", (user_id,)
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def matching_alerts_for_items(
        self, items: List[str], *, threshold: int = 80
    ) -> List[Tuple[int, str, str]]:
        """Find alert subscribers whose terms fuzzy-match the provided items.

        Returns tuples of (user_id, alert_item, matched_item).
        """

        normalized_items = [
            (item, self._normalize_text(item)) for item in items if item.strip()
        ]
        if not normalized_items:
            return []

        async with self._connect() as db:
            cursor = await db.execute("SELECT user_id, item FROM alerts")
            alerts = await cursor.fetchall()

        matches: list[Tuple[int, str, str]] = []
        for user_id, alert_item in alerts:
            normalized_alert = self._normalize_text(alert_item)
            for original_item, normalized_item in normalized_items:
                score = fuzz.WRatio(normalized_alert, normalized_item)
                if score >= threshold:
                    matches.append((user_id, alert_item, original_item))
                    break

        return matches

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

    async def record_quick_rating(
        self,
        rater_id: int,
        target_id: int,
        score: int,
        cooldown_seconds: int,
        *,
        now: int | None = None,
    ) -> tuple[bool, int | None]:
        """Record a +/- rep action outside of trades with per-user cooldowns.

        Returns a tuple of (recorded, retry_after_seconds). When the rating is rejected
        because of cooldown, ``recorded`` is False and ``retry_after_seconds`` contains
        the time remaining until another rating is allowed.
        """

        if score not in (-1, 1):
            raise ValueError("Score must be either -1 or 1")
        await self.ensure_user(rater_id)
        await self.ensure_user(target_id)

        timestamp = int(now or time.time())
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT created_at FROM quick_ratings WHERE rater_id = ? AND target_id = ?\n"
                    "ORDER BY created_at DESC LIMIT 1",
                    (rater_id, target_id),
                )
                row = await cursor.fetchone()
                if row:
                    elapsed = timestamp - row[0]
                    if elapsed < cooldown_seconds:
                        return False, cooldown_seconds - elapsed

                await db.execute(
                    "INSERT INTO quick_ratings(rater_id, target_id, score, created_at) VALUES (?, ?, ?, ?)",
                    (rater_id, target_id, score, timestamp),
                )
                if score > 0:
                    await db.execute(
                        "UPDATE users SET rep_positive = rep_positive + 1 WHERE user_id = ?",
                        (target_id,),
                    )
                else:
                    await db.execute(
                        "UPDATE users SET rep_negative = rep_negative + 1 WHERE user_id = ?",
                        (target_id,),
                    )
                await db.commit()
        return True, None

    def _score_time_window(self, seconds: int) -> int:
        """Convert a response time window into a 1-10 score."""

        if seconds <= 60 * 60:
            return 10
        if seconds <= 3 * 60 * 60:
            return 9
        if seconds <= 6 * 60 * 60:
            return 8
        if seconds <= 12 * 60 * 60:
            return 7
        if seconds <= 24 * 60 * 60:
            return 6
        if seconds <= 48 * 60 * 60:
            return 4
        if seconds <= 72 * 60 * 60:
            return 3
        if seconds <= 96 * 60 * 60:
            return 2
        return 1

    def _response_score(self, created_at: int, accepted_at: int | None, closed_at: int | None) -> int:
        base_start = created_at or int(time.time())
        end_time = closed_at or accepted_at or base_start
        accept_time = accepted_at or end_time

        response_seconds = max(0, accept_time - base_start)
        completion_seconds = max(0, end_time - accept_time)
        accept_score = self._score_time_window(response_seconds)
        completion_score = self._score_time_window(completion_seconds)
        return max(1, round((accept_score + completion_score) / 2))

    async def _record_response_for_trade(self, db: aiosqlite.Connection, trade_id: int) -> None:
        cursor = await db.execute(
            "SELECT seller_id, buyer_id, created_at, accepted_at, closed_at, response_recorded\n"
            "FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return

        seller_id, buyer_id, created_at, accepted_at, closed_at, recorded = row
        if recorded:
            return

        created_at = created_at or int(time.time())
        closed_at = closed_at or int(time.time())
        score = self._response_score(created_at, accepted_at, closed_at)
        for user_id in (seller_id, buyer_id):
            await db.execute(
                "INSERT OR IGNORE INTO users(user_id) VALUES (?)",
                (user_id,),
            )
            await db.execute(
                "UPDATE users SET response_total = response_total + ?, response_count = response_count + 1\n"
                "WHERE user_id = ?",
                (score, user_id),
            )

        await db.execute(
            "UPDATE trades SET response_recorded = 1 WHERE id = ?",
            (trade_id,),
        )

    async def create_trade(
        self, seller_id: int, buyer_id: int, item: str, *, thread_id: int | None = None
    ) -> int:
        await self.ensure_user(seller_id)
        await self.ensure_user(buyer_id)
        async with self._lock:
            async with self._connect() as db:
                now_ts = int(time.time())
                cursor = await db.execute(
                    "INSERT INTO trades(user_id, partner_id, item, status, seller_id, buyer_id, created_at, thread_id, last_activity_at)\n"
                    "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (
                        seller_id,
                        buyer_id,
                        item.strip(),
                        seller_id,
                        buyer_id,
                        now_ts,
                        thread_id,
                        now_ts,
                    ),
                )
                await db.commit()
                return cursor.lastrowid

    async def accept_trade(self, trade_id: int, seller_id: int) -> bool:
        """Mark a pending trade as accepted by the seller."""

        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "UPDATE trades SET status = 'open', accepted_at = COALESCE(accepted_at, ?)\n"
                    "WHERE id = ? AND seller_id = ? AND status = 'pending'",
                    (int(time.time()), trade_id, seller_id),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def reject_trade(self, trade_id: int, seller_id: int) -> bool:
        """Allow the seller to reject a pending trade offer."""

        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "UPDATE trades SET status = 'rejected', closed_at = COALESCE(closed_at, ?)\n"
                    "WHERE id = ? AND seller_id = ? AND status = 'pending'",
                    (int(time.time()), trade_id, seller_id),
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
                    "UPDATE trades SET status = 'completed', closed_at = COALESCE(closed_at, ?)\n"
                    "WHERE id = ? AND status = 'open'",
                    (int(time.time()), trade_id),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def cancel_trade(self, trade_id: int) -> bool:
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "UPDATE trades SET status = 'cancelled', closed_at = COALESCE(closed_at, ?)\n"
                    "WHERE id = ? AND status IN ('open', 'pending')",
                    (int(time.time()), trade_id),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def attach_trade_thread(
        self, trade_id: int, thread_id: int, *, timestamp: Optional[int] = None
    ) -> None:
        async with self._lock:
            async with self._connect() as db:
                ts = timestamp or int(time.time())
                await db.execute(
                    "UPDATE trades SET thread_id = ?, last_activity_at = ?, inactivity_warning_sent = 0 WHERE id = ?",
                    (thread_id, ts, trade_id),
                )
                await db.commit()

    async def record_trade_activity(
        self, thread_id: int, *, timestamp: Optional[int] = None
    ) -> None:
        async with self._lock:
            async with self._connect() as db:
                ts = timestamp or int(time.time())
                await db.execute(
                    "UPDATE trades SET last_activity_at = ?, inactivity_warning_sent = 0\n"
                    "WHERE thread_id = ? AND status IN ('open', 'pending')",
                    (ts, thread_id),
                )
                await db.commit()

    async def list_active_trade_threads(
        self,
    ) -> list[tuple[int, int, int, int, str, int, int]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, thread_id, seller_id, buyer_id, item, last_activity_at, inactivity_warning_sent\n"
                "FROM trades WHERE status IN ('open', 'pending') AND thread_id IS NOT NULL",
            )
            rows = await cursor.fetchall()
            return [tuple(row) for row in rows]

    async def mark_inactivity_warning_sent(self, trade_id: int) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "UPDATE trades SET inactivity_warning_sent = 1 WHERE id = ?",
                    (trade_id,),
                )
                await db.commit()

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

    async def list_trades_by_status(
        self, statuses: Iterable[str]
    ) -> List[Tuple[int, int, int, str, str]]:
        if not statuses:
            return []

        placeholders = ",".join("?" for _ in statuses)
        query = (
            "SELECT id, seller_id, buyer_id, item, status FROM trades\n"
            f"WHERE status IN ({placeholders})"
        )
        async with self._connect() as db:
            cursor = await db.execute(query, tuple(statuses))
            return await cursor.fetchall()

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

    async def record_trade_rep(
        self, trade_id: int, rater_id: int, partner_id: int, score: int, role: str
    ) -> bool:
        if score not in (-1, 1):
            raise ValueError("Score must be either -1 or 1")
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
                if score > 0:
                    await db.execute(
                        "UPDATE users SET rep_positive = rep_positive + 1 WHERE user_id = ?",
                        (partner_id,),
                    )
                else:
                    await db.execute(
                        "UPDATE users SET rep_negative = rep_negative + 1 WHERE user_id = ?",
                        (partner_id,),
                    )
                await db.commit()
                return True

    async def has_trade_rep(self, trade_id: int, rater_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT 1 FROM trade_feedback WHERE trade_id = ? AND rater_id = ?",
                (trade_id, rater_id),
            )
            return await cursor.fetchone() is not None

    async def record_trade_review(
        self, trade_id: int, reviewer_id: int, target_id: int, review: str
    ) -> bool:
        """Store or update an optional written review for a completed trade."""

        cleaned = review.strip()
        if not cleaned:
            return False

        await self.ensure_user(reviewer_id)
        await self.ensure_user(target_id)
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT 1 FROM trade_feedback WHERE trade_id = ? AND rater_id = ?",
                    (trade_id, reviewer_id),
                )
                if await cursor.fetchone() is None:
                    return False

                now_ts = int(time.time())
                await db.execute(
                    "INSERT INTO trade_reviews(trade_id, reviewer_id, target_id, review, created_at)\n"
                    "VALUES (?, ?, ?, ?, ?)\n"
                    "ON CONFLICT(trade_id, reviewer_id) DO UPDATE SET\n"
                    "review = excluded.review,\n"
                    "target_id = excluded.target_id,\n"
                    "created_at = excluded.created_at",
                    (trade_id, reviewer_id, target_id, cleaned, now_ts),
                )
                await db.commit()
                return True

    async def latest_review_for_user(self, user_id: int) -> Tuple[int, str, int] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT reviewer_id, review, created_at\n"
                "FROM trade_reviews WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            )
            return await cursor.fetchone()

    async def set_trade_status(
        self, trade_id: int, status: str, *, create_if_missing: Tuple[int, int, str] | None = None
    ) -> None:
        async with self._lock:
            async with self._connect() as db:
                if create_if_missing:
                    user_id, partner_id, item = create_if_missing
                    now_ts = int(time.time())
                    await db.execute(
                        "INSERT INTO trades(id, user_id, partner_id, item, status, seller_id, buyer_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n"
                        "ON CONFLICT(id) DO UPDATE SET status = excluded.status",
                        (
                            trade_id,
                            user_id,
                            partner_id,
                            item,
                            status,
                            user_id,
                            partner_id,
                            now_ts,
                        ),
                    )
                else:
                    now_ts = int(time.time())
                    if status in {"completed", "cancelled", "rejected"}:
                        await db.execute(
                            "UPDATE trades SET status = ?, closed_at = COALESCE(closed_at, ?) WHERE id = ?",
                            (status, now_ts, trade_id),
                        )
                    else:
                        await db.execute(
                            "UPDATE trades SET status = ? WHERE id = ?",
                            (status, trade_id),
                        )
                await db.commit()

    def _rep_xp(self, positive: int, negative: int) -> int:
        xp = positive * self.rep_xp_per_positive - negative * self.rep_xp_penalty
        return max(0, xp)

    def _rep_xp_required(self, level: int) -> int:
        clamped = max(0, min(self.max_rep_level, level))
        return self.rep_xp_base * clamped * (clamped + 1) // 2

    def _rep_level(self, positive: int, negative: int) -> int:
        xp = self._rep_xp(positive, negative)
        if xp <= 0:
            return 0
        scaled = xp * 2 // self.rep_xp_base
        level = (math.isqrt(1 + 4 * scaled) - 1) // 2
        return max(0, min(self.max_rep_level, level))

    async def set_rep_level(self, user_id: int, level: int) -> None:
        await self.ensure_user(user_id)
        clamped = max(0, min(self.max_rep_level, level))
        required_xp = self._rep_xp_required(clamped)
        positive = math.ceil(required_xp / self.rep_xp_per_positive) if required_xp else 0
        negative = 0
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "UPDATE users SET rep_positive = ?, rep_negative = ? WHERE user_id = ?",
                    (positive, negative, user_id),
                )
                await db.commit()

    async def adjust_rep(
        self,
        user_id: int,
        *,
        positive_delta: int = 0,
        negative_delta: int = 0,
    ) -> tuple[int, int]:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT rep_positive, rep_negative FROM users WHERE user_id = ?",
                    (user_id,),
                )
                row = await cursor.fetchone()
                current_positive, current_negative = row or (0, 0)
                new_positive = max(0, current_positive + positive_delta)
                new_negative = max(0, current_negative + negative_delta)
                await db.execute(
                    "UPDATE users SET rep_positive = ?, rep_negative = ? WHERE user_id = ?",
                    (new_positive, new_negative, user_id),
                )
                await db.commit()
        return new_positive, new_negative

    async def leaderboard(self, limit: int = 10) -> List[Tuple[int, int, int, int, bool]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT user_id, rep_positive, rep_negative, is_premium\n"
                "FROM users\n"
                "ORDER BY (rep_positive * ? - rep_negative * ?) DESC,\n"
                "     rep_positive DESC, rep_negative ASC\n"
                "LIMIT ?",
                (self.rep_xp_per_positive, self.rep_xp_penalty, limit),
            )
            rows = await cursor.fetchall()
            results: list[tuple[int, int, int, int, bool]] = []
            for user_id, rep_positive, rep_negative, is_premium in rows:
                level = self._rep_level(rep_positive, rep_negative)
                results.append((user_id, level, rep_positive, rep_negative, bool(is_premium)))
            return results

    async def profile(self, user_id: int) -> Tuple[str, int, int, int, str, str, bool]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT contact, rep_positive, rep_negative, timezone, bio, is_premium\n"
                "FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return "", 0, 0, 0, "", "", False

            contact, rep_positive, rep_negative, timezone, bio, is_premium = row
            level = self._rep_level(rep_positive, rep_negative)
            return (
                contact,
                level,
                rep_positive,
                rep_negative,
                timezone,
                bio,
                bool(is_premium),
            )

    async def trade_count(self, user_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM trades WHERE seller_id = ? OR buyer_id = ?",
                (user_id, user_id),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def set_timezone(self, user_id: int, timezone: str) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "UPDATE users SET timezone = ? WHERE user_id = ?",
                    (timezone.strip(), user_id),
                )
                await db.commit()

    async def set_bio(self, user_id: int, bio: str) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "UPDATE users SET bio = ? WHERE user_id = ?",
                    (bio.strip(), user_id),
                )
                await db.commit()

    async def set_premium_status(self, user_id: int, is_premium: bool) -> None:
        await self.ensure_user(user_id)
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "UPDATE users SET is_premium = ? WHERE user_id = ?",
                    (int(is_premium), user_id),
                )
                await db.commit()

    async def recent_reviews_for_user(self, user_id: int, limit: int = 3) -> List[Tuple[int, str, int]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT reviewer_id, review, created_at\n"
                "FROM trade_reviews WHERE target_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
            return await cursor.fetchall()

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
            for table in [
                "users",
                "inventories",
                "offers",
                "requests",
                "wishlist",
                "trades",
                "trade_feedback",
                "trade_reviews",
                "guild_settings",
            ]:
                cursor = await db.execute(f"SELECT * FROM {table}")
                for row in await cursor.fetchall():
                    yield table, row

    async def set_trade_thread_channel(
        self, guild_id: int, channel_id: Optional[int]
    ) -> None:
        """Persist the configured trade thread channel for a guild."""

        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT INTO guild_settings(guild_id, trade_thread_channel_id) VALUES (?, ?)\n"
                    "ON CONFLICT(guild_id) DO UPDATE SET trade_thread_channel_id = excluded.trade_thread_channel_id",
                    (guild_id, channel_id),
                )
                await db.commit()

    async def set_trade_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        """Persist the configured trade channel for a guild."""

        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "INSERT INTO guild_settings(guild_id, trade_channel_id) VALUES (?, ?)\n"
                    "ON CONFLICT(guild_id) DO UPDATE SET trade_channel_id = excluded.trade_channel_id",
                    (guild_id, channel_id),
                )
                await db.commit()

    async def get_trade_channel(self, guild_id: int) -> Optional[int]:
        """Return the configured trade channel, if present."""

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT trade_channel_id FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_store_post(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    """
                    INSERT INTO store_posts(guild_id, user_id, channel_id, message_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        channel_id = excluded.channel_id,
                        message_id = excluded.message_id
                    """,
                    (guild_id, user_id, channel_id, message_id),
                )
                await db.commit()

    async def get_store_post(self, guild_id: int, user_id: int) -> Optional[Tuple[int, int]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT channel_id, message_id
                FROM store_posts
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else None

    async def clear_store_post(self, guild_id: int, user_id: int) -> None:
        async with self._lock:
            async with self._connect() as db:
                await db.execute(
                    "DELETE FROM store_posts WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
                await db.commit()

    async def get_trade_thread_channel(self, guild_id: int) -> Optional[int]:
        """Return the configured trade thread channel, if present."""

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT trade_thread_channel_id FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None
