# RH-Trader Discord Bot

A Discord bot built with `discord.py` that tracks trading inventories, offers, requests, trades, and wishlists with emoji-rich embeds.

## Features
- Slash commands for searching, alerts, profiles, and a consolidated store workflow.
- Modal-driven trade menu that bundles stock, wishlist, and store posting actions behind buttons.
- SQLite persistence via `aiosqlite` to store user inventories, ratings, and wishlists.
- Permission-aware profile/stock viewing when targeting other users.
- Emoji-enhanced embeds for friendly UX and clear feedback.

## Setup
1. **Python environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\\Scripts\\activate
   pip install -r requirements.txt
   ```
2. **Environment variables**
   - Update the bundled `.env` file with your Discord bot token.
   - Required:
     - `DISCORD_TOKEN`: Bot token from the Discord Developer Portal.
   - Optional:
     - `TRADER_DB_PATH`: Path to the SQLite database file (default: `data/trader.db`).
3. **Run the bot**
   ```bash
   python bot.py
   ```

## Commands
- `/store` — open the all-in-one trading control panel with buttons and modals for stock, wishlist, and posting
- `/search <item> <location>` — search member stock or wishlists for a keyword
- `/alerts add|view|remove` — manage item alerts; limits scale with premium tier
- `/profile [user]` — view a trading profile with ratings, response stats, bio, and reviews
- `/leaderboard` — show the top-rated traders
- `/set_trade_channel <channel>` — pick where `/store` submissions are published
- `/poststore [image]` — post your store embeds to the configured channel (also available via `/store`)

Profile viewing accepts an optional `user` argument; targeting others requires users with manage guild/message permissions.

## Testing
Run the test suite with `pytest` from the repository root. The tests use temporary SQLite files and require no Discord connection.

```bash
pytest
```
