from __future__ import annotations

from types import SimpleNamespace

from rh_trader.bot import (
    EMBED_FIELD_CHAR_LIMIT,
    PREMIUM_STORE_LISTING_LIMIT,
    DEFAULT_STORE_LISTING_LIMIT,
    STORE_TIER_BY_SKU,
    _listing_limit_for_interaction,
    _paginate_field_entries,
)
from rh_trader.embeds import format_stock


class _DummyClient:
    def __init__(self, tier):
        self._tier = tier

    def _has_store_premium(self, _interaction):
        return self._tier


def test_listing_limit_respects_premium_status() -> None:
    """Non-premium users get the default cap and premium users get the upgraded cap."""

    no_premium = SimpleNamespace(client=_DummyClient(None))
    premium = SimpleNamespace(client=_DummyClient(STORE_TIER_BY_SKU[1_447_683_957_981_319_169]))

    assert _listing_limit_for_interaction(no_premium) == DEFAULT_STORE_LISTING_LIMIT
    assert _listing_limit_for_interaction(premium) == PREMIUM_STORE_LISTING_LIMIT


def test_paginate_field_entries_splits_on_embed_limit() -> None:
    """Pagination should kick in when a single page would exceed Discord's limits."""

    long_name_items = [(f"Item {i:02} " + "x" * 40, 1) for i in range(30)]

    pages = _paginate_field_entries(
        long_name_items, format_stock, PREMIUM_STORE_LISTING_LIMIT
    )

    assert len(pages) > 1
    assert all(len(page) <= EMBED_FIELD_CHAR_LIMIT for page in pages)
