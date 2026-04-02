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

def is_authorized(user_id, chat_id, chat_type=None):
    user_ids, group_id = get_allowed_ids()

    # Private chats: only explicitly selected users.
    if chat_type == "private":
        return user_id in user_ids

    # Group chats: only selected users inside the selected group.
    if chat_type in {"group", "supergroup"}:
        return chat_id == group_id and user_id in user_ids

    # Fallback for unknown chat types.
    return False
import os
import logging
import time
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
import json


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_URL = os.getenv("ADMIN_URL", "https://admin.vargus.tech/login")
ACCOUNTS_URL = os.getenv("ACCOUNTS_URL", "https://admin.vargus.tech/accounts")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ACCOUNT_PAGE_SIZE = int(os.getenv("ACCOUNT_PAGE_SIZE", "20"))
STRICT_HEALTH_SCORING = os.getenv("STRICT_HEALTH_SCORING", "false").lower() == "true"
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "45"))
USE_CONTEXT_FILE = os.getenv("USE_CONTEXT_FILE", "false").lower() == "true"

# In-memory cache for account status
# In-memory cache for account status
data_cache = {
    "accounts": None,
    "last_checked": 0.0,
    "overview": None,
    "overview_checked": 0.0,
}


def normalize_text(value):
    return (value or "").strip().lower()


def compute_account_health(account):
    """Classify account health from account status and joining/proxy signals."""
    status = normalize_text(account.get("status"))
    joining = normalize_text(account.get("joining"))
    proxy_status = normalize_text(account.get("proxy_status"))

    if "disabled" in status or "re-auth" in status:
        return "disabled", "Disabled", "🔴"
    if "failing" in proxy_status or "failing" in status:
        return "failing", "Failing", "🟠"
    if "paused" in joining or "paused" in proxy_status:
        return "paused", "Paused", "⏸️"
    if "active" in status and ("enabled" in joining or not joining):
        return "healthy", "Healthy", "🟢"
    return "unknown", "Unknown", "❓"

# --- Proxy Health Fetch ---
PROXY_HEALTH_URL = os.getenv("PROXY_HEALTH_URL", "https://admin.vargus.tech/api/proxies/health")
PROXIES_URL = os.getenv("PROXIES_URL", "https://admin.vargus.tech/proxies")

def fetch_proxy_health(session=None):
    """Fetch proxy health data from the API and return a dict mapping proxy (ip:port) to health info."""
    if session is None:
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


def fetch_proxy_status_by_account(session=None):
    """Parse /proxies page and map account name -> proxy status (Healthy/Failing/Paused)."""
    if session is None:
        session = login_and_get_session()

    try:
        resp = session.get(PROXIES_URL)
    except Exception:
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    status_by_account = {}

    for row in soup.select("tr"):
        link = row.select_one("a[href^='/accounts/']")
        if not link:
            continue

        account_name = link.get_text(strip=True)
        if not account_name:
            href = link.get("href", "")
            account_name = href.split("/accounts/")[-1].strip("/")
        if not account_name:
            continue

        badges = [span.get_text(strip=True) for span in row.select("span.rounded-full") if span.get_text(strip=True)]
        proxy_status = ""
        for badge in badges:
            lowered = badge.lower()
            if "healthy" in lowered:
                proxy_status = "Healthy"
                break
            if "failing" in lowered:
                proxy_status = "Failing"
                break
            if "paused" in lowered:
                proxy_status = "Paused"

        if proxy_status:
            status_by_account[account_name] = proxy_status

    return status_by_account


def fetch_proxy_details_by_account(session=None):
    """Parse /proxies table and map account -> per-account proxy details."""
    if session is None:
        session = login_and_get_session()

    try:
        resp = session.get(PROXIES_URL)
    except Exception:
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    details_by_account = {}

    for row in soup.select("tr"):
        link = row.select_one("a[href^='/accounts/']")
        if not link:
            continue

        account_name = link.get_text(strip=True)
        if not account_name:
            href = link.get("href", "")
            account_name = href.split("/accounts/")[-1].strip("/")
        if not account_name:
            continue

        cols = row.find_all("td")
        if len(cols) < 8:
            continue

        proxy_text = cols[3].get_text(" ", strip=True)
        proxy_type = cols[4].get_text(" ", strip=True)
        row_status = cols[5].get_text(" ", strip=True)
        failures = cols[6].get_text(" ", strip=True)
        last_test = cols[7].get_text(" ", strip=True)

        proxied = bool(proxy_text) and "disabled" not in proxy_text.lower() and "-" != proxy_text
        details_by_account[account_name] = {
            "proxied": proxied,
            "proxy": proxy_text or "-",
            "proxy_type": proxy_type or "-",
            "proxy_status": row_status or "-",
            "proxy_failures": failures or "-",
            "proxy_last_test": last_test or "-",
        }

    return details_by_account


def parse_admin_overview_from_accounts(soup):
    totals = {"listener_total": 0, "scrapper_total": 0, "texter_total": 0}
    cards = soup.select("div.bg-white.rounded-xl.border.border-gray-200.overflow-hidden.mb-6")
    for card in cards:
        title = card.select_one("h3")
        badge = card.select_one("h3 + span")
        if not title or not badge:
            continue

        title_text = title.get_text(strip=True).lower()
        count_text = badge.get_text(strip=True)
        try:
            count = int("".join(ch for ch in count_text if ch.isdigit()) or "0")
        except ValueError:
            count = 0

        if "listener" in title_text:
            totals["listener_total"] = count
        elif "scrapper" in title_text:
            totals["scrapper_total"] = count
        elif "texter" in title_text:
            totals["texter_total"] = count
    return totals


def parse_proxy_stats(soup):
    def read_stat(stat_id):
        node = soup.select_one(f"#{stat_id}")
        if not node:
            return 0
        text = node.get_text(strip=True)
        try:
            return int("".join(ch for ch in text if ch.isdigit()) or "0")
        except ValueError:
            return 0

    return {
        "proxied_accounts": read_stat("stat-proxied"),
        "proxy_healthy": read_stat("stat-healthy"),
        "proxy_failing": read_stat("stat-failing"),
        "proxy_paused": read_stat("stat-paused"),
    }


def fetch_admin_panel_overview(force_refresh=False):
    now = time.time()
    if not force_refresh and data_cache["overview"] and now - data_cache["overview_checked"] < CACHE_TTL_SECONDS:
        return data_cache["overview"]

    session = login_and_get_session()
    overview = {
        "listener_total": 0,
        "scrapper_total": 0,
        "texter_total": 0,
        "proxied_accounts": 0,
        "proxy_healthy": 0,
        "proxy_failing": 0,
        "proxy_paused": 0,
    }

    try:
        accounts_html = session.get(ACCOUNTS_URL).text
        overview.update(parse_admin_overview_from_accounts(BeautifulSoup(accounts_html, "html.parser")))
    except Exception:
        pass

    try:
        proxies_html = session.get(PROXIES_URL).text
        overview.update(parse_proxy_stats(BeautifulSoup(proxies_html, "html.parser")))
    except Exception:
        pass

    data_cache["overview"] = overview
    data_cache["overview_checked"] = now
    return overview


def parse_account_table(table, acc_type):
    rows = []
    raw_rows = table.find_all("tr", class_="hover:bg-gray-50")
    for row in raw_rows:
        cols = row.find_all("td")
        if len(cols) < 3:
            continue
        name = cols[0].find("a").get_text(strip=True) if cols[0].find("a") else cols[0].get_text(strip=True)
        phone = cols[1].get_text(strip=True)

        status_col = cols[2]
        status_badges = [span.get_text(strip=True) for span in status_col.find_all("span") if span.get_text(strip=True)]
        status = ""
        joining = ""
        for badge_text in status_badges:
            lowered = badge_text.lower()
            if "active" in lowered or "disabled" in lowered or "re-auth" in lowered:
                status = badge_text
            elif "joining" in lowered:
                joining = badge_text

        joins_today = cols[3].get_text(strip=True) if len(cols) > 3 else "-"
        last_join = cols[4].get_text(strip=True) if len(cols) > 4 else "-"
        proxy_status = ""
        if "paused" in joining.lower():
            proxy_status = "Paused"
        elif "enabled" in joining.lower():
            proxy_status = "Healthy"

        acc = {
            "name": name,
            "phone": phone,
            "status": status or "Unknown",
            "joining": joining or "Unknown",
            "proxy": None,
            "proxy_status": proxy_status,
            "joins_today": joins_today,
            "last_join": last_join,
            "type": acc_type,
        }
        health_key, health_label, health_emoji = compute_account_health(acc)
        acc["health"] = health_key
        acc["health_label"] = health_label
        acc["health_emoji"] = health_emoji
        rows.append(acc)
    return rows


def parse_accounts_page(html):
    soup = BeautifulSoup(html, "html.parser")
    buckets = {"listeners": [], "scrappers": [], "texters": []}
    cards = soup.find_all("div", class_="bg-white rounded-xl border border-gray-200 overflow-hidden mb-6")
    for table in cards:
        header = table.find("h3")
        if not header:
            continue
        header_text = header.get_text(strip=True).lower()
        if "listener accounts" in header_text:
            buckets["listeners"].extend(parse_account_table(table, "Listener"))
        elif "scrapper accounts" in header_text:
            buckets["scrappers"].extend(parse_account_table(table, "Scrapper"))
        elif "texter accounts" in header_text:
            buckets["texters"].extend(parse_account_table(table, "Texter"))
    return buckets


def fetch_all_accounts_pages(session):
    # Crawl each section pagination independently to fetch full inventory.
    collected = {"listeners": [], "scrappers": [], "texters": []}
    section_keys = [
        ("listeners", "lp"),
        ("scrappers", "sp"),
        ("texters", "tp"),
    ]
    for section, query_key in section_keys:
        page = 1
        seen_names = set()
        while page <= 60:
            resp = session.get(ACCOUNTS_URL, params={query_key: page})
            parsed = parse_accounts_page(resp.text)
            rows = parsed.get(section, [])
            if not rows:
                break

            added = 0
            for row in rows:
                name = row.get("name")
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                collected[section].append(row)
                added += 1

            if added == 0:
                break
            page += 1
    return collected

def fetch_accounts(strict=None, force_refresh=False):
    if strict is None:
        strict = STRICT_HEALTH_SCORING

    now = time.time()
    if not force_refresh and data_cache["accounts"] and now - data_cache["last_checked"] < CACHE_TTL_SECONDS:
        return data_cache["accounts"]

    import os
    context_path = os.path.join(os.path.dirname(__file__), "context.txt")
    session = login_and_get_session()
    proxy_health = fetch_proxy_health(session)

    if USE_CONTEXT_FILE and os.path.exists(context_path):
        with open(context_path, "r", encoding="utf-8") as f:
            parsed = parse_accounts_page(f.read())
    else:
        parsed = fetch_all_accounts_pages(session)

    listeners = parsed["listeners"]
    scrappers = parsed["scrappers"]
    texters = parsed["texters"]

    for acc in listeners + scrappers + texters:
        if acc.get("proxy") and acc["proxy"] in proxy_health:
            api_proxy_status = proxy_health[acc["proxy"]].get("status")
            if api_proxy_status:
                acc["proxy_status"] = api_proxy_status

    if strict:
        strict_session = login_and_get_session()
        proxy_health = fetch_proxy_health(strict_session)
        proxy_status_by_account = fetch_proxy_status_by_account(strict_session)
        for acc in listeners + scrappers + texters:
            try:
                details = fetch_account_details(acc["name"], session=strict_session)
            except Exception:
                continue

            if details.get("status"):
                acc["status"] = details["status"]
            if details.get("joining"):
                acc["joining"] = details["joining"]
            if details.get("proxy"):
                acc["proxy"] = details["proxy"]
            if details.get("joins_today"):
                acc["joins_today"] = details["joins_today"]
            if details.get("last_join"):
                acc["last_join"] = details["last_join"]

            page_proxy_status = proxy_status_by_account.get(acc["name"])
            if page_proxy_status:
                acc["proxy_status"] = page_proxy_status

            proxy_key = acc.get("proxy")
            if proxy_key and proxy_key in proxy_health:
                proxy_info = proxy_health[proxy_key]
                acc["proxy_status"] = proxy_info.get("status") or acc.get("proxy_status")

            health_key, health_label, health_emoji = compute_account_health(acc)
            acc["health"] = health_key
            acc["health_label"] = health_label
            acc["health_emoji"] = health_emoji

    result = {"listeners": listeners, "scrappers": scrappers, "texters": texters}
    data_cache["accounts"] = result
    data_cache["last_checked"] = now
    return result

def fetch_account_details(account_name, session=None):
    if session is None:
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

    proxy_status_by_account = fetch_proxy_status_by_account(session)
    if account_name in proxy_status_by_account:
        details["proxy_status"] = proxy_status_by_account[account_name]

    proxy_details_by_account = fetch_proxy_details_by_account(session)
    if account_name in proxy_details_by_account:
        details.update(proxy_details_by_account[account_name])
    else:
        details.setdefault("proxied", bool(details.get("proxy") and details.get("proxy") != "-"))
        details.setdefault("proxy_type", "-")
        details.setdefault("proxy_failures", "-")
        details.setdefault("proxy_last_test", "-")

    health_key, health_label, health_emoji = compute_account_health(details)
    details["health"] = health_key
    details["health_label"] = health_label
    details["health_emoji"] = health_emoji
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
    texters = []
    for a in accounts:
        acc_type = a.get("type", "Unknown")
        if acc_type == "Listener":
            listeners.append(a)
        elif acc_type == "Scrapper":
            scrappers.append(a)
        elif acc_type == "Texter":
            texters.append(a)

        health_key, health_label, health_emoji = compute_account_health(a)
        a["health"] = health_key
        a["health_label"] = health_label
        a["health_emoji"] = health_emoji

        if health_key == "paused":
            paused.append(a)
        elif health_key == "failing":
            failing.append(a)
        elif health_key == "disabled":
            disabled.append(a)
        elif health_key == "healthy":
            healthy.append(a)
        else:
            unknown.append(a)
    return healthy, paused, failing, disabled, unknown, listeners, scrappers, texters

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type if update.effective_chat else None
    if not is_authorized(user_id, chat_id, chat_type):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
            parse_mode=ParseMode.HTML
        )
        return

    accounts_dict = fetch_accounts(strict=False)
    listeners = accounts_dict["listeners"]
    scrappers = accounts_dict["scrappers"]
    texters = accounts_dict.get("texters", [])
    all_accounts = listeners + scrappers + texters
    healthy, paused, failing, disabled, unknown, _, _, _ = analyze_accounts(all_accounts)
    overview = fetch_admin_panel_overview()
    msg = (
        "<b>\U0001F4C8 Account Health Report</b>\n"
        f"<b>Total:</b> <code>{len(all_accounts)}</code>\n"
        f"<b>👂 Listener:</b> <code>{overview.get('listener_total', len(listeners))}</code>\n"
        f"<b>🧹 Scrapper:</b> <code>{overview.get('scrapper_total', len(scrappers))}</code>\n"
        f"<b>✍️ Texter:</b> <code>{overview.get('texter_total', 0)}</code>\n\n"
        f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
        f"<b>⏸️ Paused:</b> <code>{len(paused)}</code>\n"
        f"<b>🟠 Failing:</b> <code>{len(failing)}</code>\n"
        f"<b>🔴 Disabled:</b> <code>{len(disabled)}</code>\n\n"
        "<b>🌐 Proxy Panel</b>\n"
        f"<b>Proxied Accounts:</b> <code>{overview.get('proxied_accounts', 0)}</code>\n"
        f"<b>Proxy Healthy:</b> <code>{overview.get('proxy_healthy', 0)}</code>\n"
        f"<b>Proxy Failing:</b> <code>{overview.get('proxy_failing', 0)}</code>\n"
        f"<b>Proxy Paused:</b> <code>{overview.get('proxy_paused', 0)}</code>\n"
    )
    keyboard = [
        [
            InlineKeyboardButton(f"👂 Listener {len(listeners)}", callback_data="show_listener"),
            InlineKeyboardButton(f"🧹 Scrapper {len(scrappers)}", callback_data="show_scrapper"),
        ],
        [
            InlineKeyboardButton(f"✍️ Texter {len(texters)}", callback_data="show_texter"),
            InlineKeyboardButton(f"📋 All {len(all_accounts)}", callback_data="show_all"),
        ],
        [
            InlineKeyboardButton(f"🟢 {len(healthy)}", callback_data="show_healthy"),
            InlineKeyboardButton(f"⏸️ {len(paused)}", callback_data="show_paused"),
            InlineKeyboardButton(f"🟠 {len(failing)}", callback_data="show_failing"),
            InlineKeyboardButton(f"🔴 {len(disabled)}", callback_data="show_disabled"),
        ],
        [InlineKeyboardButton("♻️ Refresh", callback_data="show_report_refresh")],
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
        chat_type = update.callback_query.message.chat.type
        send_func = update.callback_query.message.reply_text
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type if update.effective_chat else None
        send_func = update.message.reply_text if update.message else None

    if not is_authorized(user_id, chat_id, chat_type):
        if send_func:
            await send_func(
                "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
                parse_mode=ParseMode.HTML
            )
        return

    # In groups, never show welcome text; return concise status directly.
    if chat_type in {"group", "supergroup"}:
        await quick(update, context)
        return

    msg = (
        "<b>✨ Welcome to <u>Vargus Account Health Bot</u>! ✨</b>\n\n"
        "<i>Monitor, analyze, and manage your accounts with style.</i>\n\n"
        "<b>Choose an option below:</b>"
    )
    keyboard = [
        [InlineKeyboardButton("📈 Account Health Report", callback_data="show_report")],
    ]
    if send_func:
        await send_func(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type if update.effective_chat else None
    if not is_authorized(user_id, chat_id, chat_type):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
            parse_mode=ParseMode.HTML
        )
        return
    accounts_dict = fetch_accounts(strict=False)
    all_accounts = accounts_dict["listeners"] + accounts_dict["scrappers"] + accounts_dict.get("texters", [])
    healthy, paused, failing, disabled, unknown, _, _, _ = analyze_accounts(all_accounts)
    msg = (
        f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
        f"<b>⏸️ Paused:</b> <code>{len(paused)}</code>\n"
        f"<b>🟠 Failing:</b> <code>{len(failing)}</code>\n"
        f"<b>🔴 Disabled:</b> <code>{len(disabled)}</code>"
    )
    if chat_type in {"group", "supergroup"}:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    else:
        keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


# --- Inline button handlers ---
def get_accounts_by_type(accounts, acc_type):
    healthy, _, failing, _, _, _, _, _ = analyze_accounts(accounts)
    if acc_type == "healthy":
        return healthy
    elif acc_type == "failed":
        return failing
    return accounts

def format_account_button(account):
    health = account.get("health_emoji", "❓")
    return InlineKeyboardButton(
        f"{health} {account['name']} ({account['phone']})",
        callback_data=f"acc_{account['name']}"
    )


def get_list_label(kind):
    labels = {
        "listener": "👂 Listener Accounts",
        "scrapper": "🧹 Scrapper Accounts",
        "texter": "✍️ Texter Accounts",
        "healthy": "🟢 Healthy Accounts",
        "paused": "⏸️ Paused Accounts",
        "failing": "🟠 Failing Accounts",
        "disabled": "🔴 Disabled Accounts",
        "unknown": "❓ Unknown Accounts",
        "all": "📋 All Accounts",
    }
    return labels.get(kind, "📋 Accounts")


async def show_accounts_page(query, accounts, kind, page):
    if page < 0:
        page = 0

    total = len(accounts)
    page_size = min(max(1, ACCOUNT_PAGE_SIZE), 10)
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page >= total_pages:
        page = total_pages - 1

    start = page * page_size
    end = min(start + page_size, total)
    current_accounts = accounts[start:end]

    keyboard = [[format_account_button(a)] for a in current_accounts]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list:{kind}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"list:{kind}:{page + 1}"))
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])

    msg = (
        f"<b>{get_list_label(kind)}</b>\n"
        f"Showing <code>{start + 1}</code>-<code>{end}</code> of <code>{total}</code>\n"
        "Select to view details:"
    )
    await query.edit_message_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type if update.effective_chat else None
    if not is_authorized(user_id, chat_id, chat_type):
        await query.edit_message_text(
            "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        await query.answer()
    except BadRequest as exc:
        # Happens when the callback button was clicked too late (> Telegram timeout).
        if "query is too old" in str(exc).lower() or "query id is invalid" in str(exc).lower():
            logger.info("Ignoring stale callback query: %s", exc)
            return
        raise
    if query.data == "noop":
        return

    if query.data == "main_menu":
        await start(update, context)
        return

    if query.data == "show_report_refresh":
        data_cache["accounts"] = None
        data_cache["overview"] = None
        data_cache["last_checked"] = 0.0
        data_cache["overview_checked"] = 0.0

    accounts_dict = fetch_accounts(strict=False)
    texters = accounts_dict.get("texters", [])
    all_accounts = accounts_dict["listeners"] + accounts_dict["scrappers"] + texters
    back_keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
    healthy, paused, failing, disabled, unknown, listeners, scrappers, texters = analyze_accounts(all_accounts)

    if query.data.startswith("list:"):
        _, kind, page_str = query.data.split(":", 2)
        try:
            page = int(page_str)
        except ValueError:
            page = 0

        grouped = {
            "listener": listeners,
            "scrapper": scrappers,
            "texter": texters,
            "healthy": healthy,
            "paused": paused,
            "failing": failing,
            "disabled": disabled,
            "unknown": unknown,
            "all": all_accounts,
        }
        await show_accounts_page(query, grouped.get(kind, all_accounts), kind, page)
        return
    if query.data == "show_listener":
        await show_accounts_page(query, listeners, "listener", 0)
        return
    elif query.data == "show_scrapper":
        await show_accounts_page(query, scrappers, "scrapper", 0)
        return
    elif query.data == "show_texter":
        await show_accounts_page(query, texters, "texter", 0)
        return
    elif query.data == "show_report":
        overview = fetch_admin_panel_overview()
        msg = (
            "<b>📈 Account Health Report</b>\n"
            f"<b>Total:</b> <code>{len(all_accounts)}</code>\n"
            f"<b>👂 Listener:</b> <code>{overview.get('listener_total', len(listeners))}</code>\n"
            f"<b>🧹 Scrapper:</b> <code>{overview.get('scrapper_total', len(scrappers))}</code>\n"
            f"<b>✍️ Texter:</b> <code>{overview.get('texter_total', len(texters))}</code>\n\n"
            f"<b>🟢 Healthy:</b> <code>{len(healthy)}</code>\n"
            f"<b>⏸️ Paused:</b> <code>{len(paused)}</code>\n"
            f"<b>🟠 Failing:</b> <code>{len(failing)}</code>\n"
            f"<b>🔴 Disabled:</b> <code>{len(disabled)}</code>\n\n"
            "<b>🌐 Proxy Panel</b>\n"
            f"<b>Proxied Accounts:</b> <code>{overview.get('proxied_accounts', 0)}</code>\n"
            f"<b>Proxy Healthy:</b> <code>{overview.get('proxy_healthy', 0)}</code>\n"
            f"<b>Proxy Failing:</b> <code>{overview.get('proxy_failing', 0)}</code>\n"
            f"<b>Proxy Paused:</b> <code>{overview.get('proxy_paused', 0)}</code>\n\n"
            "<i>Tap category buttons to drill down.</i>"
        )
        report_keyboard = [
            [
                InlineKeyboardButton(f"👂 {len(listeners)}", callback_data="show_listener"),
                InlineKeyboardButton(f"🧹 {len(scrappers)}", callback_data="show_scrapper"),
                InlineKeyboardButton(f"✍️ {len(texters)}", callback_data="show_texter"),
            ],
            [
                InlineKeyboardButton(f"⏸️ {len(paused)}", callback_data="show_paused"),
                InlineKeyboardButton(f"🟠 {len(failing)}", callback_data="show_failing"),
                InlineKeyboardButton(f"🔴 {len(disabled)}", callback_data="show_disabled"),
            ],
            [
                InlineKeyboardButton("📋 View All", callback_data="show_all"),
                InlineKeyboardButton("♻️ Refresh", callback_data="show_report_refresh"),
            ],
            back_keyboard[0],
        ]
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(report_keyboard))
        return
    elif query.data == "show_healthy":
        await show_accounts_page(query, healthy, "healthy", 0)
        return
    elif query.data == "show_paused":
        await show_accounts_page(query, paused, "paused", 0)
        return
    elif query.data == "show_failing" or query.data == "show_failed":
        await show_accounts_page(query, failing, "failing", 0)
        return
    elif query.data == "show_disabled":
        await show_accounts_page(query, disabled, "disabled", 0)
        return
    elif query.data == "show_unknown":
        await show_accounts_page(query, unknown, "unknown", 0)
        return
    elif query.data == "show_all":
        await show_accounts_page(query, all_accounts, "all", 0)
        return
    elif query.data.startswith("acc_"):
        acc_name = query.data[4:]
        details = fetch_account_details(acc_name)
        proxied_text = "Yes" if details.get("proxied") else "No"
        msg = (
            f"<b>👤 Account:</b> <code>{details.get('name','-')}</code>\n"
            f"<b>📱 Phone:</b> <code>{details.get('phone','-')}</code>\n"
            f"<b>📊 Status:</b> <code>{details.get('status','-')}</code>\n"
            f"<b>{details.get('health_emoji','❓')} Health:</b> <code>{details.get('health_label','Unknown')}</code>\n"
            f"<b>⏯️ Joining:</b> <code>{details.get('joining','-')}</code>\n"
            f"<b>🧑‍💻 Type:</b> <code>{details.get('type','-')}</code>\n"
            f"<b>🔌 Proxied:</b> <code>{proxied_text}</code>\n"
            f"<b>🌐 Proxy:</b> <code>{details.get('proxy','-')}</code>\n"
            f"<b>🧩 Proxy Type:</b> <code>{details.get('proxy_type','-')}</code>\n"
            f"<b>📶 Proxy Health:</b> <code>{details.get('proxy_status','-')}</code>\n"
            f"<b>⚠️ Proxy Failures:</b> <code>{details.get('proxy_failures','-')}</code>\n"
            f"<b>🕒 Proxy Last Test:</b> <code>{details.get('proxy_last_test','-')}</code>\n"
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


async def quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type if update.effective_chat else None
    if not is_authorized(user_id, chat_id, chat_type):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
            parse_mode=ParseMode.HTML
        )
        return

    accounts_dict = fetch_accounts(strict=False)
    all_accounts = accounts_dict["listeners"] + accounts_dict["scrappers"] + accounts_dict.get("texters", [])
    healthy, paused, failing, disabled, _, listeners, scrappers, texters = analyze_accounts(all_accounts)
    overview = fetch_admin_panel_overview()

    msg = (
        "<b>⚡ Quick Stats</b>\n"
        f"<b>Total:</b> <code>{len(all_accounts)}</code> | "
        f"<b>👂</b> <code>{len(listeners)}</code> | "
        f"<b>🧹</b> <code>{len(scrappers)}</code> | "
        f"<b>✍️</b> <code>{len(texters)}</code>\n"
        f"<b>🟢</b> <code>{len(healthy)}</code> | "
        f"<b>⏸️</b> <code>{len(paused)}</code> | "
        f"<b>🟠</b> <code>{len(failing)}</code> | "
        f"<b>🔴</b> <code>{len(disabled)}</code>\n"
        f"<b>🌐 Proxied:</b> <code>{overview.get('proxied_accounts', 0)}</code>"
    )
    keyboard = [[InlineKeyboardButton("📈 Open Full Report", callback_data="show_report")]]
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def healthy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type if update.effective_chat else None
    if not is_authorized(user_id, chat_id, chat_type):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
            parse_mode=ParseMode.HTML
        )
        return

    accounts_dict = fetch_accounts(strict=False)
    all_accounts = accounts_dict["listeners"] + accounts_dict["scrappers"] + accounts_dict.get("texters", [])
    healthy, _, _, _, _, _, _, _ = analyze_accounts(all_accounts)

    if not healthy:
        await update.message.reply_text("No healthy accounts right now.")
        return

    keyboard = [[format_account_button(a)] for a in healthy[:10]]
    if len(healthy) > 10:
        keyboard.append([InlineKeyboardButton("Next ➡️", callback_data="list:healthy:1")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])

    msg = (
        "<b>🟢 Healthy Accounts</b>\n"
        f"Showing <code>1</code>-<code>{min(10, len(healthy))}</code> of <code>{len(healthy)}</code>\n"
        "Select to view details:"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type if update.effective_chat else None
    if not is_authorized(user_id, chat_id, chat_type):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\nYou are not authorized to use this bot.",
            parse_mode=ParseMode.HTML
        )
        return

    msg = (
        "<b>🤖 Vargus Bot Commands</b>\n"
        "<code>/start</code> - Open menu\n"
        "<code>/report</code> - Full account health report\n"
        "<code>/quick</code> - Fast one-message stats\n"
        "<code>/status</code> - Health counters only\n"
        "<code>/healthy</code> - Open healthy accounts list\n"
        "<code>/help</code> - Show this help"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    

def main():
    async def post_init(app):
        private_commands = [
            BotCommand("start", "restart / open main menu"),
            BotCommand("report", "full account health report"),
            BotCommand("quick", "fast stats in one message"),
            BotCommand("status", "health counters only"),
            BotCommand("healthy", "list healthy accounts"),
            BotCommand("help", "help and commands"),
        ]
        group_commands = [
            BotCommand("report", "full account health report"),
            BotCommand("quick", "fast stats"),
            BotCommand("status", "health counters"),
        ]
        await app.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        if isinstance(err, BadRequest):
            msg = str(err).lower()
            if "query is too old" in msg or "query id is invalid" in msg:
                logger.info("Suppressed stale callback query error: %s", err)
                return
        logger.exception("Unhandled bot error", exc_info=err)

    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("quick", quick))
    app.add_handler(CommandHandler("healthy", healthy_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
