# --- Access Control ---
def get_allowed_ids():
    user_ids = os.getenv("ALLOWED_USER_IDS", "").split(",")
    user_ids = {int(uid.strip()) for uid in user_ids if uid.strip().isdigit()}
    group_id = os.getenv("ALLOWED_GROUP_ID")
    try:
        group_id = int(group_id)
    except Exception:
        group_id = None
    return user_ids, group_id

def is_authorized(user_id, chat_id):
    user_ids, group_id = get_allowed_ids()
    return user_id in user_ids or chat_id == group_id
import os
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
import json

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_URL = os.getenv("ADMIN_URL", "https://admin.vargus.tech/login")
ACCOUNTS_URL = os.getenv("ACCOUNTS_URL", "https://admin.vargus.tech/accounts")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# In-memory cache for account status
# In-memory cache for account status
data_cache = {
    "accounts": [],
    "last_checked": None
}

# --- Proxy Health Fetch ---
PROXY_HEALTH_URL = os.getenv("PROXY_HEALTH_URL", "https://admin.vargus.tech/api/proxies/health")

def fetch_proxy_health():
    """Fetch proxy health data from the API and return a dict mapping proxy (ip:port) to health info."""
    session = login_and_get_session()
    resp = session.get(PROXY_HEALTH_URL)
    try:
        data = resp.json()
    except Exception:
        try:
            data = json.loads(resp.text)
        except Exception:
            return {}
    # Expecting a list of proxies with health info
    # Example: [{"proxy": "1.2.3.4:1080", "status": "Healthy", ...}, ...]
    proxy_map = {}
    for item in data:
        proxy_map[item.get("proxy")] = item
    return proxy_map

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
    # Enhancement: Parse accounts directly from context.txt for speed and clarity
    import os
    context_path = os.path.join(os.path.dirname(__file__), "context.txt")
    with open(context_path, "r", encoding="utf-8") as f:
        html = f.read()
    soup = BeautifulSoup(html, "html.parser")
    proxy_health = fetch_proxy_health()
    listeners = []
    scrappers = []
    rows = soup.find_all("tr", class_="hover:bg-gray-50")
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 6:
            continue
        name = cols[1].get_text(strip=True)
        phone = cols[2].get_text(strip=True)
        proxy = None
        proxy_status = None
        # Proxy info
        proxy_span = cols[3].find("span", class_="inline-flex")
        if proxy_span:
            proxy = proxy_span.get_text(strip=True)
            if proxy in proxy_health:
                proxy_status = proxy_health[proxy].get("status")
        # Type info: The HTML only shows SOCKS5/HTTP, not Listener/Scrapper. For now, mark all as Listener for test.
        acc_type = "Listener"
        # Status info
        status_col = cols[5]
        status_span = status_col.find("span")
        status = status_span.get_text(strip=True) if status_span else status_col.get_text(strip=True)
        acc = {
            "name": name,
            "phone": phone,
            "status": status,
            "proxy": proxy,
            "proxy_status": proxy_status,
            "type": acc_type
        }
        listeners.append(acc)
    return {"listeners": listeners, "scrappers": scrappers}

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
    # Enhanced: consider both account status and proxy health
    healthy = []
    paused = []
    failing = []
    disabled = []
    unknown = []
    listeners = []
    scrappers = []
    for a in accounts:
        acc_status = a.get("status", "")
        proxy_status = a.get("proxy_status", "")
        acc_type = a.get("type", "Unknown")
        if acc_type == "Listener":
            listeners.append(a)
        elif acc_type == "Scrapper":
            scrappers.append(a)
        if proxy_status == "Paused":
            paused.append(a)
        elif proxy_status == "Failing":
            failing.append(a)
        elif "Disabled" in acc_status or "Re-auth Required" in acc_status:
            disabled.append(a)
        elif proxy_status == "Healthy" and "Active" in acc_status:
            healthy.append(a)
        elif not proxy_status and not acc_status:
            unknown.append(a)
    return healthy, paused, failing, disabled, unknown, listeners, scrappers

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts_dict = fetch_accounts()
    listeners = accounts_dict["listeners"]
    scrappers = accounts_dict["scrappers"]
    all_accounts = listeners + scrappers
    healthy, paused, failing, disabled, unknown, _, _ = analyze_accounts(all_accounts)
    msg = (
        "<b>\U0001F4C8 Account Health Report</b>\n"
        f"<b>Total:</b> <code>{len(all_accounts)}</code>\n"
        f"<b>👂 Listener:</b> <code>{len(listeners)}</code>\n"
        f"<b>🧹 Scrapper:</b> <code>{len(scrappers)}</code>\n\n"
        f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
        f"<b>⏸️ Paused:</b> <code>{len(paused)}</code>\n"
        f"<b>🟠 Failing:</b> <code>{len(failing)}</code>\n"
        f"<b>🔴 Disabled:</b> <code>{len(disabled)}</code>\n"
        f"<b>❓ Unknown:</b> <code>{len(unknown)}</code>\n"
    )
    keyboard = [
        [InlineKeyboardButton(f"👂 Listener ({len(listeners)})", callback_data="show_listener")],
        [InlineKeyboardButton(f"🧹 Scrapper ({len(scrappers)})", callback_data="show_scrapper")],
        [InlineKeyboardButton(f"🟢 Healthy ({len(healthy)})", callback_data="show_healthy")],
        [InlineKeyboardButton(f"⏸️ Paused ({len(paused)})", callback_data="show_paused")],
        [InlineKeyboardButton(f"🟠 Failing ({len(failing)})", callback_data="show_failing")],
        [InlineKeyboardButton(f"🔴 Disabled ({len(disabled)})", callback_data="show_disabled")],
        [InlineKeyboardButton(f"❓ Unknown ({len(unknown)})", callback_data="show_unknown")],
        [InlineKeyboardButton("📋 All Accounts", callback_data="show_all")],
        [InlineKeyboardButton("➡️ Next", callback_data="next_page")]
    ]
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Always show exactly three inline buttons as requested
    if hasattr(update, 'callback_query') and update.callback_query:
        user_id = update.callback_query.from_user.id
        chat_id = update.callback_query.message.chat.id
        send_func = update.callback_query.message.reply_text
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        send_func = update.message.reply_text if update.message else None

    if not is_authorized(user_id, chat_id):
        if send_func:
            await send_func(
                "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
                parse_mode=ParseMode.HTML
            )
        return

    msg = (
        "<b>✨ Welcome to <u>Vargus Account Health Bot</u>! ✨</b>\n\n"
        "<i>Monitor, analyze, and manage your accounts with style.</i>\n\n"
        "<b>Choose an option below:</b>"
    )
    keyboard = [
        [InlineKeyboardButton("📈 Account Health Report", callback_data="show_report")],
        [InlineKeyboardButton("🟢 Healthy", callback_data="show_healthy"), InlineKeyboardButton("🔴 Failed", callback_data="show_failed")]
    ]
    if send_func:
        await send_func(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

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
    healthy, paused, failing, disabled, unknown = analyze_accounts(accounts)
    msg = (
        f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
        f"<b>⏸️ Paused:</b> <code>{len(paused)}</code>\n"
        f"<b>🟠 Failing:</b> <code>{len(failing)}</code>\n"
        f"<b>🔴 Disabled:</b> <code>{len(disabled)}</code>\n"
        f"<b>❓ Unknown:</b> <code>{len(unknown)}</code>"
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
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_authorized(user_id, chat_id):
        await query.edit_message_text(
            "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
            parse_mode=ParseMode.HTML
        )
        return
    await query.answer()
    accounts_dict = fetch_accounts()
    all_accounts = accounts_dict["listeners"] + accounts_dict["scrappers"]
    back_keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
    healthy, paused, failing, disabled, unknown, listeners, scrappers = analyze_accounts(all_accounts)

    if query.data == "show_listener":
        keyboard = [[format_account_button(a)] for a in accounts_dict["listeners"]]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>👂 Listener Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    elif query.data == "show_scrapper":
        keyboard = [[format_account_button(a)] for a in accounts_dict["scrappers"]]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>🧹 Scrapper Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    elif query.data == "show_report":
        msg = (
            "<b>📈 Account Health Report</b>\n"
            f"<b>Total:</b> <code>{len(all_accounts)}</code>\n"
            f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
            f"<b>⏸️ Paused:</b> <code>{len(paused)}</code>\n"
            f"<b>🟠 Failing:</b> <code>{len(failing)}</code>\n"
            f"<b>🔴 Disabled:</b> <code>{len(disabled)}</code>\n"
            f"<b>❓ Unknown:</b> <code>{len(unknown)}</code>\n\n"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(back_keyboard))
        return
    elif query.data == "show_healthy":
        keyboard = [[format_account_button(a)] for a in healthy]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>🟢 Healthy Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    elif query.data == "show_paused":
        keyboard = [[format_account_button(a)] for a in paused]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>⏸️ Paused Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    elif query.data == "show_failing":
        keyboard = [[format_account_button(a)] for a in failing]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>🟠 Failing Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    elif query.data == "show_disabled":
        keyboard = [[format_account_button(a)] for a in disabled]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>🔴 Disabled Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    elif query.data == "show_unknown":
        keyboard = [[format_account_button(a)] for a in unknown]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>❓ Unknown Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    elif query.data == "show_all":
        keyboard = [[format_account_button(a)] for a in all_accounts]
        keyboard += back_keyboard
        await query.edit_message_text(
            "<b>📋 All Accounts</b>\nSelect to view details:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    elif query.data == "main_menu":
        await start(update, context)
        return
    elif query.data.startswith("acc_"):
        acc_name = query.data[4:]
        details = fetch_account_details(acc_name)
        msg = (
            f"<b>👤 Account:</b> <code>{details.get('name','-')}</code>\n"
            f"<b>📱 Phone:</b> <code>{details.get('phone','-')}</code>\n"
            f"<b>📊 Status:</b> <code>{details.get('status','-')}</code>\n"
            f"<b>🧑‍💻 Type:</b> <code>{details.get('type','-')}</code>\n"
            f"<b>🔌 Proxy:</b> <code>{details.get('proxy','-')}</code>\n"
            f"<b>🌐 Proxy Health:</b> <code>{details.get('proxy_status','-')}</code>\n"
            f"<b>🕒 Created:</b> <code>{details.get('created','-')}</code>\n"
            f"<b>🔄 Joins Today:</b> <code>{details.get('joins_today','-')}</code>\n"
            f"<b>⏰ Last Join:</b> <code>{details.get('last_join','-')}</code>\n"
            f"<b>⏳ Last Flood Wait:</b> <code>{details.get('last_flood_wait','-')}</code>\n"
            f"<b>🆔 Telegram ID:</b> <code>{details.get('telegram_id','-')}</code>\n"
            f"<b>👤 Username:</b> <code>{details.get('username','-')}</code>\n"
            f"<b>📝 First Name:</b> <code>{details.get('first_name','-')}</code>\n"
            f"<b>📅 Registered:</b> <code>{details.get('registered','-')}</code>\n"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(back_keyboard))
        return
    

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
