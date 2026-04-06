"""SQLite persistence for thread + reputation features."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import aiosqlite

REP_CATEGORIES = ("trading", "knowledge", "skill")


@dataclass(slots=True)
class Profile:
    user_id: int
    trading: int
    knowledge: int
    skill: int

    @property
    def total(self) -> int:
        return self.trading + self.knowledge + self.skill


class Database:
    """Minimal data access layer used by the slimmed down bot."""

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    async def setup(self) -> None:
        async with self._connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS rep_totals (
                    user_id INTEGER PRIMARY KEY,
                    trading INTEGER NOT NULL DEFAULT 0,
                    knowledge INTEGER NOT NULL DEFAULT 0,
                    skill INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS reputation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rater_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_rep_cooldown
                    ON reputation_events(rater_id, target_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS migration_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            await self._migrate_legacy_rep_to_trading(db)
            await db.commit()

    async def _migrate_legacy_rep_to_trading(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute(
            "SELECT value FROM migration_state WHERE key = 'legacy_rep_to_trading_v1'"
        )
        if await cursor.fetchone():
            return

        table_check = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        )
        if await table_check.fetchone() is None:
            await db.execute(
                "INSERT OR REPLACE INTO migration_state(key, value) VALUES ('legacy_rep_to_trading_v1', 'done')"
            )
            return

        legacy_rows = await db.execute(
            "SELECT user_id, COALESCE(rep_positive, 0) FROM users WHERE COALESCE(rep_positive, 0) > 0"
        )
        rows = await legacy_rows.fetchall()
        for user_id, rep_positive in rows:
            await db.execute(
                """
                INSERT INTO rep_totals(user_id, trading, knowledge, skill)
                VALUES (?, ?, 0, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    trading = MAX(rep_totals.trading, excluded.trading)
                """,
                (int(user_id), int(rep_positive)),
            )

        await db.execute(
            "INSERT OR REPLACE INTO migration_state(key, value) VALUES ('legacy_rep_to_trading_v1', 'done')"
        )

    async def get_pair_cooldown_remaining(
        self, rater_id: int, target_id: int, cooldown_seconds: int
    ) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT created_at
                FROM reputation_events
                WHERE rater_id = ? AND target_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (rater_id, target_id),
            )
            row = await cursor.fetchone()

        if row is None:
            return 0

        elapsed = int(time.time()) - int(row[0])
        remaining = cooldown_seconds - elapsed
        return max(0, remaining)

    async def add_reputation(self, rater_id: int, target_id: int, category: str) -> None:
        normalized = category.lower().strip()
        if normalized not in REP_CATEGORIES:
            raise ValueError(f"Invalid category: {category}")

        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO reputation_events (rater_id, target_id, category, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (rater_id, target_id, normalized, int(time.time())),
            )
            await db.execute(
                """
                INSERT INTO rep_totals(user_id, trading, knowledge, skill)
                VALUES (?, 0, 0, 0)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (target_id,),
            )
            await db.execute(
                f"UPDATE rep_totals SET {normalized} = {normalized} + 1 WHERE user_id = ?",
                (target_id,),
            )
            await db.commit()

    async def get_profile(self, user_id: int) -> Profile:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT user_id, trading, knowledge, skill
                FROM rep_totals
                WHERE user_id = ?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return Profile(user_id=user_id, trading=0, knowledge=0, skill=0)

        return Profile(user_id=int(row[0]), trading=int(row[1]), knowledge=int(row[2]), skill=int(row[3]))
