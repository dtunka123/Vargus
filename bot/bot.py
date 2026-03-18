import os
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()
# Environment variables (set these in your .env or environment)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_URL = os.getenv("ADMIN_URL", "https://admin.vargus.tech/login")
ACCOUNTS_URL = os.getenv("ACCOUNTS_URL", "https://admin.vargus.tech/accounts")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# In-memory cache for account status
data_cache = {
    "accounts": [],
    "last_checked": None
}

def login_and_get_session():
    session = requests.Session()
    # Get login page for CSRF token if needed
    resp = session.get(ADMIN_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    # Find CSRF token if present
    csrf = soup.find("input", {"name": "csrf_token"})
    payload = {
        "username": ADMIN_USERNAME,
        "password": ADMIN_PASSWORD
    }
    if csrf:
        payload["csrf_token"] = csrf["value"]
    # Post login
    session.post(ADMIN_URL, data=payload)
    return session

def fetch_accounts():
    session = login_and_get_session()
    resp = session.get(ACCOUNTS_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    accounts = []
    for row in soup.select("table tbody tr"):
        cols = row.find_all("td")
        if len(cols) < 3:
            continue
        name = cols[0].get_text(strip=True)
        phone = cols[1].get_text(strip=True)
        status = cols[2].get_text(strip=True)
        accounts.append({
            "name": name,
            "phone": phone,
            "status": status
        })
    return accounts

def analyze_accounts(accounts):
    healthy = [a for a in accounts if "Active" in a["status"]]
    failed = [a for a in accounts if "Disabled" in a["status"] or "Re-auth Required" in a["status"]]
    return healthy, failed

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = fetch_accounts()
    healthy, failed = analyze_accounts(accounts)
    msg = f"\U0001F4C8 <b>Account Health Report</b>\n"
    msg += f"<b>Total:</b> {len(accounts)}\n"
    msg += f"<b>Healthy:</b> {len(healthy)}\n"
    msg += f"<b>Failed:</b> {len(failed)}\n\n"
    if failed:
        msg += "<b>Failed Accounts:</b>\n"
        for a in failed:
            msg += f"- {a['name']} ({a['phone']})\n"
    else:
        msg += "\U00002705 All accounts healthy!"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Use /report to get account health.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = fetch_accounts()
    healthy, failed = analyze_accounts(accounts)
    await update.message.reply_text(f"Healthy: {len(healthy)}, Failed: {len(failed)}")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("status", status))
    app.run_polling()

if __name__ == "__main__":
    main()
