# Vargus Telegram Bot

A production-ready Telegram bot to monitor your Vargus admin panel account health and report status in your group.

## Features
- Logs in to your admin panel using credentials (no database needed)
- Scrapes account health from the accounts page
- `/report` command: shows how many accounts are healthy/failed, with details
- `/status` command: quick healthy/failed count
- `/start` command: welcome/help
- All in-memory, no paid services

## Setup Instructions
1. Clone this repo and enter the `bot` directory:
   ```bash
   git clone <your-repo-url>
   cd Vargus/bot
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   # Edit .env with your values
   ```
4. Run the bot:
   ```bash
   python bot.py
   ```
5. Add the bot to your Telegram group as admin.

## Example `/report` Output
```
📈 Account Health Report
Total: 10
Healthy: 7
Failed: 3

Failed Accounts:
- account_3 (+251917813784)
- account_6 (+251913014380)
- account_9 (+593967608604)
```
Or:
```
📈 Account Health Report
Total: 10
Healthy: 10
✅ All accounts healthy!
```

## Notes
- No database is used; all checks are live and in-memory.
- You can extend the bot to monitor more endpoints or add more commands easily.
- Make sure your bot token and admin credentials are kept secret!
