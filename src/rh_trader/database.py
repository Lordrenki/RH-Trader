"""SQLite persistence for thread + reputation features."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import aiosqlite

REP_CATEGORIES = ("trading", "knowledge", "skill", "trials")


@dataclass(slots=True)
class Profile:
    user_id: int
    trading: int
    knowledge: int
    skill: int
    trials: int

    @property
    def total(self) -> int:
        return self.trading + self.knowledge + self.skill + self.trials


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
                    skill INTEGER NOT NULL DEFAULT 0,
                    trials INTEGER NOT NULL DEFAULT 0
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

                CREATE TABLE IF NOT EXISTS scam_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_user_id INTEGER NOT NULL,
                    embark_id TEXT NOT NULL,
                    normalized_embark_id TEXT NOT NULL UNIQUE,
                    added_by_discord_user_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            await self._migrate_legacy_rep_to_trading(db)
            await self._ensure_trials_column(db)
            await self._ensure_seasons_tables(db)
            await db.commit()


    async def _ensure_trials_column(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(rep_totals)")
        columns = {str(row[1]).lower() for row in await cursor.fetchall()}
        if "trials" not in columns:
            await db.execute("ALTER TABLE rep_totals ADD COLUMN trials INTEGER NOT NULL DEFAULT 0")

    async def _ensure_seasons_tables(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS trial_seasons (
                season_number INTEGER PRIMARY KEY,
                started_at INTEGER NOT NULL,
                ended_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS trial_season_rep (
                season_number INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rep INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (season_number, user_id)
            );
            """
        )

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
                INSERT INTO rep_totals(user_id, trading, knowledge, skill, trials)
                VALUES (?, ?, 0, 0, 0)
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
                INSERT INTO rep_totals(user_id, trading, knowledge, skill, trials)
                VALUES (?, 0, 0, 0, 0)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (target_id,),
            )
            await db.execute(
                f"UPDATE rep_totals SET {normalized} = {normalized} + 1 WHERE user_id = ?",
                (target_id,),
            )
            if normalized == "trials":
                season = await self._get_active_season_number_db(db)
                if season is not None:
                    await db.execute(
                        """
                        INSERT INTO trial_season_rep(season_number, user_id, rep)
                        VALUES (?, ?, 1)
                        ON CONFLICT(season_number, user_id) DO UPDATE SET rep = rep + 1
                        """,
                        (season, target_id),
                    )
            await db.commit()

    async def get_profile(self, user_id: int) -> Profile:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT user_id, trading, knowledge, skill, trials
                FROM rep_totals
                WHERE user_id = ?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return Profile(user_id=user_id, trading=0, knowledge=0, skill=0, trials=0)

        return Profile(user_id=int(row[0]), trading=int(row[1]), knowledge=int(row[2]), skill=int(row[3]), trials=int(row[4]))

    @staticmethod
    def normalize_embark_id(embark_id: str) -> str:
        return embark_id.strip().lower()

    async def add_scam_report(
        self,
        discord_user_id: int,
        embark_id: str,
        added_by_discord_user_id: int,
    ) -> tuple[bool, str]:
        normalized = self.normalize_embark_id(embark_id)
        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO scam_reports
                (
                    discord_user_id,
                    embark_id,
                    normalized_embark_id,
                    added_by_discord_user_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    discord_user_id,
                    embark_id.strip(),
                    normalized,
                    added_by_discord_user_id,
                    int(time.time()),
                ),
            )
            await db.commit()
            inserted = cursor.rowcount > 0
        return inserted, normalized

    async def get_scam_report_by_embark_id(
        self,
        embark_id: str,
    ) -> tuple[int, str, int, int] | None:
        normalized = self.normalize_embark_id(embark_id)
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT discord_user_id, embark_id, added_by_discord_user_id, created_at
                FROM scam_reports
                WHERE normalized_embark_id = ?
                LIMIT 1
                """,
                (normalized,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return int(row[0]), str(row[1]), int(row[2]), int(row[3])


    async def _get_active_season_number_db(self, db: aiosqlite.Connection) -> int | None:
        cursor = await db.execute(
            "SELECT season_number FROM trial_seasons WHERE ended_at IS NULL ORDER BY season_number DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return None if row is None else int(row[0])

    async def get_active_season_number(self) -> int | None:
        async with self._connect() as db:
            return await self._get_active_season_number_db(db)

    async def start_new_trial_season(self) -> int:
        async with self._connect() as db:
            active = await self._get_active_season_number_db(db)
            if active is not None:
                raise ValueError("A trial season is already active")
            cursor = await db.execute("SELECT COALESCE(MAX(season_number), 0) + 1 FROM trial_seasons")
            next_num = int((await cursor.fetchone())[0])
            await db.execute(
                "INSERT INTO trial_seasons(season_number, started_at, ended_at) VALUES (?, ?, NULL)",
                (next_num, int(time.time())),
            )
            await db.commit()
            return next_num

    async def end_active_trial_season(self) -> int:
        async with self._connect() as db:
            active = await self._get_active_season_number_db(db)
            if active is None:
                raise ValueError("No active trial season")
            await db.execute("UPDATE trial_seasons SET ended_at = ? WHERE season_number = ?", (int(time.time()), active))
            await db.execute("UPDATE rep_totals SET trials = 0")
            await db.commit()
            return active

    async def get_total_rep_leaderboard(self, limit: int = 10) -> list[tuple[int, int]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT user_id, (trading + knowledge + skill + trials) AS total
                FROM rep_totals
                WHERE total > 0
                ORDER BY total DESC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]

    async def get_trial_season_leaderboard(self, season_number: int, limit: int = 10) -> list[tuple[int, int]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT user_id, rep
                FROM trial_season_rep
                WHERE season_number = ? AND rep > 0
                ORDER BY rep DESC, user_id ASC
                LIMIT ?
                """,
                (season_number, limit),
            )
            rows = await cursor.fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]
