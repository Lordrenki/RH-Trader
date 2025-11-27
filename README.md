# RH-Trader Discord Bot

A Discord bot built with `discord.py` that tracks trading inventories, offers, requests, trades, and wishlists with emoji-rich embeds.

## Features
- Slash commands for managing stock, wishlists, search, profiles, trades, and leaderboards.
- Modal-driven trade menu that bundles common actions with Discord Components.
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
- `/trademenu` — open the trading control panel with buttons and modals
- `/stock add <item> [quantity]`
- `/stock view [user]`
- `/stock remove` (modal-driven)
- `/stock clear`
- `/search <item>`
- `/profile [user]`
- `/trade start <partner> <item> <role>`
- `/leaderboard`
- `/wishlist add <item> [note]`
- `/wishlist view [user]`
- `/wishlist remove` (modal-driven)
- `/set_trade_channel <channel>`
- `/tradepost [image]`

Profiles, stock, and wishlist viewing accepts an optional `user` argument; targeting others requires users with manage guild/message permissions.

## Testing
Run the test suite with `pytest` from the repository root. The tests use temporary SQLite files and require no Discord connection.

```bash
pytest
```
