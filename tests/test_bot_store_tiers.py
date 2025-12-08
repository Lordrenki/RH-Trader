"""Tests for store premium tier selection and limits."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import discord

from rh_trader.bot import GLOBAL_STORE_POST_SKU, STORE_TIER_BY_SKU, TraderBot


class DummyEntitlement:
    """Minimal entitlement stub used to exercise tier selection."""

    def __init__(self, sku_id: int, ends_at):
        self.sku_id = sku_id
        self.ends_at = ends_at


def test_store_premium_prefers_highest_tier() -> None:
    """Expert Trader should beat lower tiers and expose its higher limits."""

    bot = TraderBot.__new__(TraderBot)
    now = discord.utils.utcnow()
    interaction = SimpleNamespace(
        entitlements=[
            DummyEntitlement(1_447_683_957_981_319_169, now + timedelta(hours=1)),
            DummyEntitlement(1_447_725_003_956_293_724, now + timedelta(hours=1)),
            DummyEntitlement(1_447_725_110_529_102_005, now + timedelta(hours=2)),
        ]
    )

    tier = bot._has_store_premium(interaction)

    assert tier is not None
    assert tier is STORE_TIER_BY_SKU[1_447_725_110_529_102_005]
    assert tier.name == "Expert Trader"
    assert tier.post_limit == 5
    assert tier.listing_limit == 50


def test_store_premium_ignores_expired_entitlements() -> None:
    """Expired entitlements drop to the best remaining active tier."""

    bot = TraderBot.__new__(TraderBot)
    now = discord.utils.utcnow()
    interaction = SimpleNamespace(
        entitlements=[
            DummyEntitlement(1_447_683_957_981_319_169, now - timedelta(hours=1)),
            DummyEntitlement(1_447_725_003_956_293_724, now + timedelta(hours=1)),
        ]
    )

    tier = bot._has_store_premium(interaction)

    assert tier is STORE_TIER_BY_SKU[1_447_725_003_956_293_724]
    assert tier.rank == 2
    assert tier.post_limit == 4
    assert tier.listing_limit == 35


def test_global_store_post_consumable_is_not_a_tier() -> None:
    """The consumable global post SKU should not grant premium tier benefits."""

    bot = TraderBot.__new__(TraderBot)
    now = discord.utils.utcnow()
    interaction = SimpleNamespace(
        entitlements=[DummyEntitlement(GLOBAL_STORE_POST_SKU, now + timedelta(hours=1))]
    )

    tier = bot._has_store_premium(interaction)

    assert tier is None
