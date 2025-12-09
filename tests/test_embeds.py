import discord

from rh_trader import embeds


def test_format_helpers_render_text():
    stock = embeds.format_stock([("Widget", 2)])
    assert "Widget" in stock

    wishlist = embeds.format_wishlist([("Widget", "Need soon")])
    assert "Need soon" in wishlist

    offers = embeds.format_offers([(1, "Widget", 2, "Great price")])
    assert "<@1>" in offers

    requests = embeds.format_requests([(2, "Widget", 1, "ASAP")])
    assert "ASAP" in requests

    summary = embeds.rating_summary(4.5, 10)
    assert "4.50" in summary


def test_info_embed_sets_footer():
    embed = embeds.info_embed("Title", "Body")
    assert isinstance(embed, discord.Embed)
    assert embed.footer.text.startswith("Scrap Market")
