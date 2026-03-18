import os
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)
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
    msg = (
        "<b>\U0001F4C8 Account Health Report</b>\n"
        f"<b>Total:</b> <code>{len(accounts)}</code>\n"
        f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
        f"<b>🔴 Failed:</b> <code>{len(failed)}</code>\n\n"
    )
    keyboard = []
    if healthy:
        keyboard.append([
            InlineKeyboardButton(f"🟢 Healthy ({len(healthy)})", callback_data="show_healthy")
        ])
    if failed:
        keyboard.append([
            InlineKeyboardButton(f"🔴 Failed ({len(failed)})", callback_data="show_failed")
        ])
    keyboard.append([
        InlineKeyboardButton("📋 All Accounts", callback_data="show_all")
    ])
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "<b>👋 Welcome to <u>Vargus Account Health Bot</u>!</b>\n\n"
        "<i>Monitor, analyze, and manage your accounts with style.</i>\n\n"
        "<b>Commands:</b>\n"
        "• /report — <i>Get a beautiful health report</i>\n"
        "• /status — <i>Quick healthy/failed count</i>\n\n"
        "<b>Tip:</b> Use the buttons for details!"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = fetch_accounts()
    healthy, failed = analyze_accounts(accounts)
    msg = (
        f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
        f"<b>🔴 Failed:</b> <code>{len(failed)}</code>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# --- Inline button handlers ---
def get_accounts_by_type(accounts, acc_type):
    healthy, failed = analyze_accounts(accounts)
    if acc_type == "healthy":
        return healthy
    elif acc_type == "failed":
        return failed
    return accounts

def format_account_button(account):
    return InlineKeyboardButton(
        f"{account['name']} ({account['phone']})",
        callback_data=f"acc_{account['name']}"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = fetch_accounts()
    if query.data == "show_healthy":
        healthy = get_accounts_by_type(accounts, "healthy")
        keyboard = [[format_account_button(a)] for a in healthy]
        await query.edit_message_text(
            "<b>🟢 Healthy Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif query.data == "show_failed":
        failed = get_accounts_by_type(accounts, "failed")
        keyboard = [[format_account_button(a)] for a in failed]
        await query.edit_message_text(
            "<b>🔴 Failed Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif query.data == "show_all":
        keyboard = [[format_account_button(a)] for a in accounts]
        await query.edit_message_text(
            "<b>📋 All Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif query.data.startswith("acc_"):
        acc_name = query.data[4:]
        acc = next((a for a in accounts if a["name"] == acc_name), None)
        if acc:
            # Here you can fetch more details if needed
            msg = (
                f"<b>👤 Account:</b> <code>{acc['name']}</code>\n"
                f"<b>Phone:</b> <code>{acc['phone']}</code>\n"
                f"<b>Status:</b> <code>{acc['status']}</code>\n"
            )
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_text("Account not found.")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
