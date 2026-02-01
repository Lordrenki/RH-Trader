from __future__ import annotations

from rh_trader.bot import (
    EMBED_FIELD_CHAR_LIMIT,
    DEFAULT_LISTING_LIMIT,
    _listing_limit_for_interaction,
    _paginate_field_entries,
)
from rh_trader.embeds import format_stock

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
