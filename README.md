# RH-Trader Discord Bot

A Discord bot built with `discord.py` that tracks trading inventories, offers, requests, trades, and wishlists with emoji-rich embeds.

## Features
- Text-based trading reputation commands: `+rep @user` to give rep and `rep @user` to check rep.
- Modal-driven trade menu that bundles stock, wishlist, and store posting actions behind buttons.
- SQLite persistence via `aiosqlite` to store trading reputation, scam reports, and blueprint price caches.
- Permission-aware profile/stock viewing when targeting other users.
- Emoji-enhanced embeds for friendly UX and clear feedback.
- Store posts are the entry point for deals; continue conversations in your server's trade channel instead of DMs.

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

## Reputation Commands
- `+rep @user` — give a member +1 trading reputation.
- `rep @user` — check a member's trading reputation.

Only trading reputation is tracked. Reputation milestone roles are still awarded automatically when members reach the configured trading-rep thresholds.

## Testing
Run the test suite with `pytest` from the repository root. The tests use temporary SQLite files and require no Discord connection.

```bash
pytest
```
