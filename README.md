# RH-Trader Discord Bot

A Discord bot built with `discord.py` that tracks trading inventories, offers, requests, trades, and wishlists with emoji-rich embeds.

## Features
- Slash commands for stock, offers, requests, search, contact, profile, trades, leaderboards, and wishlists.
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
   - Copy `.env.example` to `.env` and fill in your token.
   ```bash
   cp .env.example .env
   ```
   - Required:
     - `DISCORD_TOKEN`: Bot token from the Discord Developer Portal.
   - Optional:
     - `TRADER_DB_PATH`: Path to the SQLite database file (default: `data/trader.db`).
3. **Run the bot**
   - Directly from the repo (no install needed):
     ```bash
     python bot.py
     ```
     The entrypoint adds the local `src` directory to `PYTHONPATH` so imports like `rh_trader.bot` resolve.
   - Or install in editable mode and run the module:
     ```bash
     pip install -e .
     python -m rh_trader.bot
     ```

## Commands
- `/stock add <item> [quantity]`
- `/stock view [user]`
- `/stock remove <item>`
- `/stock clear`
- `/offer <item> [quantity] [details]`
- `/request <item> [quantity] [details]`
- `/search <term>`
- `/contact <contact>`
- `/profile [user]`
- `/trade rate <user> <score 1-5>`
- `/trade complete <trade_id> <partner> <item>`
- `/leaderboard`
- `/wishlist add <item> [note]`
- `/wishlist view [user]`
- `/wishlist remove <item>`

Profiles, stock, and wishlist viewing accepts an optional `user` argument; targeting others requires users with manage guild/message permissions.

## Testing
Run the test suite with `pytest` from the repository root. The tests use temporary SQLite files and require no Discord connection.

```bash
pytest
```
