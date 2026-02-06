from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rh_trader.bot import (
    EMBED_FIELD_CHAR_LIMIT,
    DEFAULT_LISTING_LIMIT,
    MIN_TRADE_ACCOUNT_AGE_DAYS,
    _listing_limit_for_interaction,
    _is_under_min_trade_account_age,
    _paginate_field_entries,
)
from rh_trader.embeds import format_stock


class _DummyMember:
    def __init__(self, created_at: datetime) -> None:
        self.created_at = created_at

def test_listing_limit_uses_default_limit() -> None:
    """Listing limits should be consistent for all users."""

    dummy = object()
    assert _listing_limit_for_interaction(dummy) == DEFAULT_LISTING_LIMIT


def test_paginate_field_entries_splits_on_embed_limit() -> None:
    """Pagination should kick in when a single page would exceed Discord's limits."""

    long_name_items = [(f"Item {i:02} " + "x" * 40, 1) for i in range(30)]

    pages = _paginate_field_entries(
        long_name_items, format_stock, DEFAULT_LISTING_LIMIT
    )

    assert len(pages) > 1
    assert all(len(page) <= EMBED_FIELD_CHAR_LIMIT for page in pages)


def test_account_age_gate_flags_member_under_minimum_age() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    too_new = _DummyMember(now - timedelta(days=MIN_TRADE_ACCOUNT_AGE_DAYS - 1))

    assert _is_under_min_trade_account_age(too_new, now=now)


def test_account_age_gate_allows_member_at_minimum_age() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_enough = _DummyMember(now - timedelta(days=MIN_TRADE_ACCOUNT_AGE_DAYS))

    assert not _is_under_min_trade_account_age(old_enough, now=now)
