# --- Access Control ---
ALLOWED_USER_IDS = {1094871625, 5994127216, 6654137551, 7958155942}
ALLOWED_GROUP_ID = -1003894038820
def is_authorized(user_id, chat_id):
    return user_id in ALLOWED_USER_IDS or chat_id == ALLOWED_GROUP_ID
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
    accounts = []
    page = 1
    while True:
        url = f"{ACCOUNTS_URL}?lp={page}&sp=7"
        resp = session.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tbody tr")
        if not rows:
            break
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            name = cols[0].get_text(strip=True)
            phone = cols[1].get_text(strip=True)
            status = cols[2].get_text(strip=True)
            # Try to get account type (Listener/Scrapper) from the table or fallback
            acc_type = "Listener" if "Listener" in status else ("Scrapper" if "Scrapper" in status else "Unknown")
            accounts.append({
                "name": name,
                "phone": phone,
                "status": status,
                "type": acc_type
            })
        # Check for next page
        next_btn = soup.find("a", string=lambda s: s and "Next" in s)
        if next_btn and "href" in next_btn.attrs:
            page += 1
        else:
            break
    return accounts

def fetch_account_details(account_name):
    session = login_and_get_session()
    url = f"{ACCOUNTS_URL}/{account_name}"
    resp = session.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    # Extract details from the admin panel structure
    details = {"name": account_name}
    # Main info
    try:
        details["phone"] = soup.select_one(".font-mono").text.strip()
    except Exception:
        details["phone"] = "-"
    # Status badges
    badges = soup.select("span.rounded-full")
    for badge in badges:
        text = badge.get_text(strip=True)
        if "Active" in text:
            details["status"] = "Active"
        elif "Disabled" in text:
            details["status"] = "Disabled"
        elif "Re-auth Required" in text:
            details["status"] = "Re-auth Required"
        elif "Listener" in text:
            details["type"] = "Listener"
        elif "Scrapper" in text:
            details["type"] = "Scrapper"
        elif "Joining Paused" in text:
            details["joining"] = "Paused"
        elif "Joining Enabled" in text:
            details["joining"] = "Enabled"
        elif "Banned" in text:
            details["banned"] = True
    # Created, Joins Today, Last Join, Last Flood Wait
    grid = soup.select(".grid .text-xs")
    grid_values = soup.select(".grid .font-medium")
    for label, value in zip(grid, grid_values):
        label_text = label.get_text(strip=True)
        value_text = value.get_text(strip=True)
        if "Created" in label_text:
            details["created"] = value_text
        elif "Joins Today" in label_text:
            details["joins_today"] = value_text
        elif "Last Join" in label_text:
            details["last_join"] = value_text
        elif "Last Flood Wait" in label_text:
            details["last_flood_wait"] = value_text
        elif "Telegram ID" in label_text:
            details["telegram_id"] = value_text
        elif "Username" in label_text:
            details["username"] = value_text
        elif "First Name" in label_text:
            details["first_name"] = value_text
        elif "Registered" in label_text:
            details["registered"] = value_text
        elif "Proxy" == label_text:
            details["proxy"] = value_text
        elif "Proxy Server" in label_text:
            details["proxy_server"] = value_text
        elif "Proxy User" in label_text:
            details["proxy_user"] = value_text
    return details

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
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if not is_authorized(user_id, chat_id):
            await update.message.reply_text(
                "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
                parse_mode=ParseMode.HTML
            )
            return
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if not is_authorized(user_id, chat_id):
            await update.message.reply_text(
                "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
                parse_mode=ParseMode.HTML
            )
            return
    msg = (
        "<b>✨ Welcome to <u>Vargus Account Health Bot</u>! ✨</b>\n\n"
        "<i>Monitor, analyze, and manage your accounts with style.</i>\n\n"
        "<b>Main Menu:</b>\n"
        "<b>📈 Account Health</b>\n"
        "<b>🟢 Healthy</b> / <b>🔴 Failed</b> / <b>📋 All</b>\n\n"
        "<b>Commands:</b>\n"
        "• /report — <i>Get a beautiful health report</i>\n"
        "• /status — <i>Quick healthy/failed count</i>\n\n"
        "<b>Tip:</b> Use the menu buttons below!"
    )
    keyboard = [
        [
            InlineKeyboardButton("📈 Account Health Report", callback_data="show_report")
        ],
        [
            InlineKeyboardButton("🟢 Healthy", callback_data="show_healthy"),
            InlineKeyboardButton("🔴 Failed", callback_data="show_failed")
        ],
        [
            InlineKeyboardButton("📋 All Accounts", callback_data="show_all")
        ]
    ]
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if not is_authorized(user_id, chat_id):
            await update.message.reply_text(
                "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
                parse_mode=ParseMode.HTML
            )
            return
    accounts = fetch_accounts()
    healthy, failed = analyze_accounts(accounts)
    msg = (
        f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
        f"<b>🔴 Failed:</b> <code>{len(failed)}</code>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


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
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if not is_authorized(user_id, chat_id):
            await update.callback_query.edit_message_text(
                "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
                parse_mode=ParseMode.HTML
            )
            return
    query = update.callback_query
    await query.answer()
    accounts = fetch_accounts()
    back_keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
    if query.data == "show_report":
        healthy, failed = analyze_accounts(accounts)
        msg = (
            "<b>📈 Account Health Report</b>\n"
            f"<b>Total:</b> <code>{len(accounts)}</code>\n"
            f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
            f"<b>🔴 Failed:</b> <code>{len(failed)}</code>\n\n"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(back_keyboard))
    elif query.data == "show_healthy":
        healthy = get_accounts_by_type(accounts, "healthy")
        keyboard = [[format_account_button(a)] for a in healthy]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>🟢 Healthy Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif query.data == "show_failed":
        failed = get_accounts_by_type(accounts, "failed")
        keyboard = [[format_account_button(a)] for a in failed]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>🔴 Failed Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif query.data == "show_all":
        keyboard = [[format_account_button(a)] for a in accounts]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>📋 All Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif query.data == "main_menu":
        await start(query, context)
    elif query.data.startswith("acc_"):
        acc_name = query.data[4:]
        details = fetch_account_details(acc_name)
        msg = (
            f"<b>👤 Account:</b> <code>{details.get('name','-')}</code>\n"
            f"<b>📱 Phone:</b> <code>{details.get('phone','-')}</code>\n"
            f"<b>📊 Status:</b> <code>{details.get('status','-')}</code>\n"
            f"<b>🧑‍💻 Type:</b> <code>{details.get('type','-')}</code>\n"
            f"<b>🕒 Created:</b> <code>{details.get('created','-')}</code>\n"
            f"<b>🔄 Joins Today:</b> <code>{details.get('joins_today','-')}</code>\n"
            f"<b>⏰ Last Join:</b> <code>{details.get('last_join','-')}</code>\n"
            f"<b>⏳ Last Flood Wait:</b> <code>{details.get('last_flood_wait','-')}</code>\n"
            f"<b>🆔 Telegram ID:</b> <code>{details.get('telegram_id','-')}</code>\n"
            f"<b>👤 Username:</b> <code>{details.get('username','-')}</code>\n"
            f"<b>📝 First Name:</b> <code>{details.get('first_name','-')}</code>\n"
            f"<b>📅 Registered:</b> <code>{details.get('registered','-')}</code>\n"
            f"<b>🔌 Proxy:</b> <code>{details.get('proxy','-')}</code>\n"
            f"<b>🌐 Proxy Server:</b> <code>{details.get('proxy_server','-')}</code>\n"
            f"<b>👤 Proxy User:</b> <code>{details.get('proxy_user','-')}</code>\n"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(back_keyboard))
    

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
