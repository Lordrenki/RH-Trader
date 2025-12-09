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
- `/storemenu` — open the trading control panel with buttons and modals
- `/search <item> <location>` — search member stock or wishlists for a keyword
- `/profile [user]` — view a trading profile with ratings, response stats, bio, and reviews
- `/leaderboard` — show the top-rated traders
- `/set_trade_channel <channel>` — pick where `/poststore` submissions are published
- `/poststore [image]` — post your store embeds to the configured channel

### Trading commands
- `/trade start <partner> <item>` — open a trade and move the conversation to DMs
- `/trade kudos <partner> <score>` — send a quick 1–5 star rating outside a trade with cooldown enforcement

### Stock commands
- `/stock add <item> [quantity]` — add inventory entries
- `/stock change <item> <quantity>` — update an existing item quantity (set to 0 to remove)
- `/stock view [user]` — view your own or another member's stock list
- `/stock remove <item>` — fuzzy-remove an item from your stock
- `/stock clear` — wipe your inventory list

### Wishlist commands
- `/wishlist add <item> [note]` — add an item you want with an optional note
- `/wishlist view [user]` — view your wishlist or another member's
- `/wishlist remove <item>` — fuzzy-remove a wishlist entry

### Alert commands
- `/alerts add <item>` — create an item alert; limits scale with premium tier
- `/alerts view` — list your alert entries
- `/alerts remove <item>` — fuzzy-remove an alert item

Profiles, stock, and wishlist viewing accept an optional `user` argument; targeting others requires users with manage guild/message permissions.

## Testing
Run the test suite with `pytest` from the repository root. The tests use temporary SQLite files and require no Discord connection.

```bash
pytest
```
