import pytest

from src.rh_trader.bot import _summary_with_optional_boost_text
from src.rh_trader.embeds import rating_summary


def test_summary_helper_uses_show_flag_when_supported():
    result = _summary_with_optional_boost_text(
        rating_summary,
        score=4.2,
        count=10,
        premium_boost=True,
        show_premium_boost_text=False,
    )

    assert "Premium boost" not in result
    assert result.startswith("⭐ 4.41")


def test_summary_helper_handles_legacy_signature():
    def legacy_summary(score, count, *, premium_boost=False, boost_percent=0.05):
        adjusted = min(5.0, score * (1 + boost_percent)) if premium_boost else score
        suffix = " (Premium boost)" if premium_boost else ""
        return f"⭐ {adjusted:.2f} average from {count} ratings{suffix}"

    result = _summary_with_optional_boost_text(
        legacy_summary,
        score=4.5,
        count=12,
        premium_boost=True,
        show_premium_boost_text=False,
    )

    assert result == "⭐ 4.73 average from 12 ratings"
