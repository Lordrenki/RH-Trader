from pathlib import Path

import pytest

from rh_trader.database import Database

pytestmark = pytest.mark.asyncio


async def test_add_and_lookup_scam_report(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test.db"))
    await db.setup()

    inserted, normalized = await db.add_scam_report(
        discord_user_id=42,
        embark_id="RaiderPro#4821",
        added_by_discord_user_id=7,
    )
    assert inserted is True
    assert normalized == "raiderpro#4821"

    report = await db.get_scam_report_by_embark_id("raiderpro#4821")
    assert report == (42, "RaiderPro#4821", 7, report[3])


async def test_duplicate_embark_id_is_ignored(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test.db"))
    await db.setup()

    first_inserted, _ = await db.add_scam_report(
        discord_user_id=101,
        embark_id="User#1234",
        added_by_discord_user_id=1,
    )
    second_inserted, normalized = await db.add_scam_report(
        discord_user_id=102,
        embark_id=" user#1234 ",
        added_by_discord_user_id=2,
    )

    assert first_inserted is True
    assert second_inserted is False
    assert normalized == "user#1234"
