import os
import asyncio
import logging
import re
import requests
import json
import time
import threading
import pycountry
from bs4 import BeautifulSoup
from datetime import datetime
from flask import Flask, jsonify
from dotenv import load_dotenv
from urllib.parse import unquote
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROUP_ID = os.getenv('TELEGRAM_GROUP_ID')
CHANNEL_LINK = os.getenv('CHANNEL_LINK', 'https://t.me/yourchannel')
DEV_LINK = os.getenv('DEV_LINK', 'https://t.me/yourdev')

LOGIN_URL = "https://www.ivasms.com/login"
SMS_LIST_URL = "https://www.ivasms.com/portal/sms/received/getsms"
SMS_NUMBERS_URL = "https://www.ivasms.com/portal/sms/received/getsms/number"
SMS_DETAILS_URL = "https://www.ivasms.com/portal/sms/received/getsms/number/sms"
NUMBERS_PAGE_URL = "https://www.ivasms.com/portal/numbers"

SMS_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.ivasms.com/portal/sms/received",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "text/html, */*; q=0.01",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate"
}

SERVICE_PATTERNS = {
    "WhatsApp": r"(whatsapp|wa\.me|verify|wassap|whtsapp)",
    "Facebook": r"(facebook|fb\.me|fb\-|meta)",
    "Telegram": r"(telegram|t\.me|tg|telegrambot)",
    "Google": r"(google|gmail|goog|g\.co|accounts\.google)",
    "Twitter": r"(twitter|x\.com|twtr)",
    "Instagram": r"(instagram|insta|ig)",
    "Apple": r"(apple|icloud|appleid)",
    "Amazon": r"(amazon|amzn)",
    "Microsoft": r"(microsoft|msft|outlook|hotmail)",
    "PayPal": r"(paypal)",
    "Netflix": r"(netflix)",
    "Uber": r"(uber)",
    "TikTok": r"(tiktok)",
    "LinkedIn": r"(linkedin)",
    "Spotify": r"(spotify)",
    "Lalamove": r"(lalamove)",
}

COUNTRY_ALIASES = {
    "Ivory": "Cote d'Ivoire",
    "USA": "United States",
    "UK": "United Kingdom",
    "UAE": "United Arab Emirates",
    "Benin": "Benin",
    "Russia": "Russian Federation",
    "China": "China",
    "India": "India",
    "Brazil": "Brazil",
    "Nigeria": "Nigeria",
    "Algeria": "Algeria",
    "Madagascar": "Madagascar",
}

OTP_HISTORY_FILE = "otp_history.json"

bot_stats = {
    'start_time': datetime.now(),
    'total_otps_sent': 0,
    'last_check': 'Never',
    'last_error': None,
    'is_running': False,
    'session_valid': False,
}

user_sessions = {}
bot = None
telegram_app = None
ivasms_session = None
last_login_time = 0


# ============================================================
# COUNTRY / SERVICE HELPERS
# ============================================================

def get_flag_emoji(country_code):
    if not country_code or len(country_code) != 2:
        return "🌍"
    code_points = [ord(c.upper()) - ord('A') + 0x1F1E6 for c in country_code]
    return chr(code_points[0]) + chr(code_points[1])


def get_country_emoji(country_name):
    try:
        name = COUNTRY_ALIASES.get(country_name, country_name)
        countries = pycountry.countries.search_fuzzy(name)
        if countries:
            return get_flag_emoji(countries[0].alpha_2)
    except Exception:
        pass
    return "🌍"


def extract_country_from_range(range_name):
    if not range_name:
        return "Unknown"
    parts = range_name.strip().split()
    if parts:
        return parts[0].capitalize()
    return "Unknown"


def extract_service(message):
    for service, pattern in SERVICE_PATTERNS.items():
        if re.search(pattern, message, re.IGNORECASE):
            return service
    return "Unknown"


def extract_otp(text):
    match = re.search(r'\b(\d{4,8})\b', text)
    return match.group(1) if match else None


# ============================================================
# OTP HISTORY
# ============================================================

def load_otp_history():
    try:
        if os.path.exists(OTP_HISTORY_FILE):
            with open(OTP_HISTORY_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_otp_history(history):
    try:
        with open(OTP_HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving OTP history: {e}")


def is_otp_already_sent(msg_id, full_message):
    history = load_otp_history()
    if msg_id not in history:
        return False
    for entry in history[msg_id]:
        if entry.get("full_message") == full_message:
            return True
    return False


def mark_otp_sent(msg_id, otp, full_message):
    history = load_otp_history()
    if msg_id not in history:
        history[msg_id] = []
    history[msg_id].append({
        "otp": otp,
        "full_message": full_message,
        "timestamp": datetime.now().isoformat()
    })
    save_otp_history(history)


# ============================================================
# COOKIE-BASED AUTH (no browser needed)
# ============================================================

def ivasms_login():
    """
    Load cookies from the IVASMS_COOKIES env variable.
    
    Set on Render as a single env var:
    IVASMS_COOKIES=cf_clearance=XXX; ivas_sms_session=YYY; XSRF-TOKEN=ZZZ
    """
    global ivasms_session, last_login_time, bot_stats

    cookie_string = os.getenv('IVASMS_COOKIES', '')
    if not cookie_string:
        logger.error("❌ IVASMS_COOKIES env var not set! Bot cannot fetch SMS.")
        bot_stats['session_valid'] = False
        return False

    session = requests.Session()
    session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
    })

    # Parse "key=value; key2=value2" string into session cookies
    cookie_count = 0
    for part in cookie_string.split(';'):
        part = part.strip()
        if '=' in part:
            name, _, value = part.partition('=')
            name = name.strip()
            value = value.strip()
            session.cookies.set(name, value, domain='www.ivasms.com')
            logger.info(f"  ✅ Loaded cookie: {name}")
            cookie_count += 1

    if cookie_count == 0:
        logger.error("❌ No cookies parsed from IVASMS_COOKIES!")
        bot_stats['session_valid'] = False
        return False

    # Test the session
    try:
        test = session.get('https://www.ivasms.com/portal/sms/received', timeout=15)
        logger.info(f"Session test: {test.status_code} → {test.url}")

        if test.status_code == 200 and 'login' not in test.url:
            ivasms_session = session
            last_login_time = time.time()
            bot_stats['session_valid'] = True
            logger.info("✅ IVASMS session working!")
            return True
        else:
            logger.error("❌ Cookies rejected — redirected to login. Update IVASMS_COOKIES!")
            bot_stats['session_valid'] = False
            ivasms_session = session  # keep it, will retry
            send_cookie_expiry_alert()
            return False

    except Exception as e:
        logger.error(f"Session test error: {e}")
        ivasms_session = session
        bot_stats['session_valid'] = False
        return False


def refresh_session_if_needed():
    """Re-test the session every hour."""
    global last_login_time
    if time.time() - last_login_time >= 3600:
        logger.info("🔄 Re-testing IVASMS session (hourly check)...")
        ivasms_login()


def send_cookie_expiry_alert():
    """Send a Telegram alert when cookies expire."""
    def _alert():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def _send():
                b = Bot(token=BOT_TOKEN)
                await b.send_message(
                    chat_id=GROUP_ID,
                    text=(
                        "⚠️ <b>NEXUSBOT: Session Expired!</b>\n\n"
                        "Your IVASMS cookies have expired.\n\n"
                        "<b>To fix:</b>\n"
                        "1. Login to ivasms.com in Chrome\n"
                        "2. F12 → Application → Cookies → www.ivasms.com\n"
                        "3. Copy values of:\n"
                        "   • <code>cf_clearance</code>\n"
                        "   • <code>ivas_sms_session</code>\n"
                        "   • <code>XSRF-TOKEN</code>\n"
                        "4. Update <code>IVASMS_COOKIES</code> on Render\n"
                        "5. Redeploy the service"
                    ),
                    parse_mode='HTML'
                )
            loop.run_until_complete(_send())
            loop.close()
        except Exception as e:
            logger.error(f"Could not send expiry alert: {e}")

    threading.Thread(target=_alert, daemon=True).start()


# ============================================================
# IVASMS DATA FETCHING
# ============================================================

def get_csrf_token():
    try:
        resp = ivasms_session.get('https://www.ivasms.com/portal/sms/received', timeout=15)

        if 'login' in resp.url:
            logger.warning("⚠️ Redirected to login — cookies expired!")
            bot_stats['session_valid'] = False
            send_cookie_expiry_alert()
            return None

        soup = BeautifulSoup(resp.content, 'html.parser')

        csrf = soup.find('meta', {'name': 'csrf-token'})
        if csrf and csrf.get('content'):
            return csrf.get('content')

        csrf_input = soup.find('input', {'name': '_token'})
        if csrf_input and csrf_input.get('value'):
            return csrf_input.get('value')

        xsrf = ivasms_session.cookies.get('XSRF-TOKEN', '')
        if xsrf:
            return unquote(xsrf)

    except Exception as e:
        logger.error(f"Error getting CSRF: {e}")
    return None


def fetch_sms_ranges(csrf):
    try:
        headers = SMS_HEADERS.copy()
        headers['X-CSRF-TOKEN'] = csrf
        payload = f"_token={csrf}&from=&to="
        resp = ivasms_session.post(SMS_LIST_URL, headers=headers, data=payload, timeout=30)
        logger.info(f"SMS ranges response: {resp.status_code}")

        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        items = soup.find_all('div', class_='item')
        ranges = []
        for item in items:
            range_div = item.find('div', class_='col-sm-4')
            if range_div:
                ranges.append(range_div.text.strip())

        logger.info(f"Found ranges: {ranges}")
        return ranges

    except Exception as e:
        logger.error(f"Error fetching ranges: {e}")
        return []


def fetch_numbers_for_range(range_name, csrf):
    try:
        headers = SMS_HEADERS.copy()
        headers['X-CSRF-TOKEN'] = csrf
        payload = f"_token={csrf}&start=&end=&range={range_name}"
        resp = ivasms_session.post(SMS_NUMBERS_URL, headers=headers, data=payload, timeout=30)
        soup = BeautifulSoup(resp.text, 'html.parser')
        number_divs = soup.find_all('div', class_='col-sm-4')
        return [div.text.strip() for div in number_divs if div.text.strip()]
    except Exception as e:
        logger.error(f"Error fetching numbers for {range_name}: {e}")
        return []


def fetch_sms_for_number(number, range_name, csrf):
    try:
        headers = SMS_HEADERS.copy()
        headers['X-CSRF-TOKEN'] = csrf
        payload = f"_token={csrf}&start=&end=&Number={number}&Range={range_name}"
        resp = ivasms_session.post(SMS_DETAILS_URL, headers=headers, data=payload, timeout=30)
        soup = BeautifulSoup(resp.text, 'html.parser')
        message_divs = soup.select('div.col-9.col-sm-6 p.mb-0.pb-0')
        return [div.text.strip() for div in message_divs] if message_divs else []
    except Exception as e:
        logger.error(f"Error fetching SMS for {number}: {e}")
        return []


def get_ivasms_numbers():
    try:
        resp = ivasms_session.get(NUMBERS_PAGE_URL, timeout=15)
        soup = BeautifulSoup(resp.content, 'html.parser')
        numbers = []
        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')[1:]
            for row in rows:
                cells = row.find_all('td')
                if cells:
                    row_data = [c.get_text(strip=True) for c in cells]
                    numbers.append(row_data)
        return numbers
    except Exception as e:
        logger.error(f"Error fetching numbers page: {e}")
        return []


def get_received_sms():
    messages = []
    try:
        if ivasms_session is None:
            logger.error("No session available")
            return []

        refresh_session_if_needed()
        csrf = get_csrf_token()
        if not csrf:
            logger.warning("Could not get CSRF token — session may have expired")
            ivasms_login()
            csrf = get_csrf_token()
            if not csrf:
                return []

        ranges = fetch_sms_ranges(csrf)
        if not ranges:
            logger.info("No SMS ranges found")
            return []

        for range_name in ranges:
            try:
                numbers = fetch_numbers_for_range(range_name, csrf)
                country_name = extract_country_from_range(range_name)
                country_emoji = get_country_emoji(country_name)

                for number in numbers:
                    try:
                        sms_list = fetch_sms_for_number(number, range_name, csrf)
                        for sms_text in sms_list:
                            otp = extract_otp(sms_text)
                            if not otp:
                                continue

                            service = extract_service(sms_text)
                            msg_id = f"{number}_{otp}_{sms_text[:30]}"

                            if is_otp_already_sent(msg_id, sms_text):
                                continue

                            messages.append({
                                'id': msg_id,
                                'phone': number,
                                'otp': otp,
                                'service': service,
                                'message': sms_text,
                                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                'country': f"{country_emoji} {country_name}",
                                'range': range_name,
                            })

                        time.sleep(0.3)

                    except Exception as e:
                        logger.error(f"Error processing number {number}: {e}")
                        continue

            except Exception as e:
                logger.error(f"Error processing range {range_name}: {e}")
                continue

    except Exception as e:
        logger.error(f"Error in get_received_sms: {e}")

    return messages


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📱 Get Number", callback_data="get_number")],
        [InlineKeyboardButton("📊 Status", callback_data="status"),
         InlineKeyboardButton("📈 Stats", callback_data="stats")],
        [InlineKeyboardButton("🔍 Check OTPs Now", callback_data="check")],
        [InlineKeyboardButton("🧪 Send Test OTP", callback_data="test")]
    ]
    return InlineKeyboardMarkup(keyboard)


def country_keyboard():
    numbers = get_ivasms_numbers()
    ranges = {}
    for row in numbers:
        if len(row) >= 2:
            number = row[0]
            range_name = row[1]
            if range_name not in ranges:
                ranges[range_name] = number

    keyboard = []
    row = []
    for range_name, number in list(ranges.items())[:20]:
        country = extract_country_from_range(range_name)
        emoji = get_country_emoji(country)
        row.append(InlineKeyboardButton(
            f"{emoji} {range_name}",
            callback_data=f"country_{range_name}"
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    if not keyboard:
        keyboard = [[InlineKeyboardButton("⚠️ No numbers found", callback_data="menu")]]

    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu")])
    return InlineKeyboardMarkup(keyboard)


def number_assigned_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔄 Change Number", callback_data="change_number")],
        [InlineKeyboardButton("🌍 Change Country", callback_data="change_country")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def otp_buttons():
    keyboard = [
        [
            InlineKeyboardButton("📢 NUMBER CHANNEL", url=CHANNEL_LINK),
            InlineKeyboardButton("🤖 BOT DEVELOPER", url=DEV_LINK)
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ============================================================
# MESSAGE FORMATTERS
# ============================================================

def format_otp_message(data):
    service = data.get('service', 'Unknown')
    country = data.get('country', '🌍 Unknown')
    phone = data.get('phone', 'Unknown')
    otp = data.get('otp', '------')
    timestamp = data.get('timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    message = data.get('message', '')

    if len(phone) > 6:
        masked = phone[:4] + '***' + phone[-4:]
    else:
        masked = phone

    text = f"""✅ {country} | {service} OTP Received

━━━━━━━━━━━━━━━━━━━━
📱 <b>Number:</b> {masked}
🔑 <b>OTP Code:</b> <code>{otp}</code>
🛠 <b>Service:</b> {service}
🌍 <b>Country:</b> {country}
🕐 <b>Time:</b> {timestamp}
━━━━━━━━━━━━━━━━━━━━

💬 <b>Message:</b>
<blockquote>{message}</blockquote>"""

    return text


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = """🏠 <b>Welcome to NEXUSBOT!</b>

I monitor IVASMS for new OTPs and forward them instantly.

Use the menu below to get started:"""
    await update.message.reply_text(welcome, parse_mode='HTML', reply_markup=main_menu_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "menu":
        await query.edit_message_text(
            "🏠 <b>Main Menu</b>\n\nChoose an option:",
            parse_mode='HTML',
            reply_markup=main_menu_keyboard()
        )

    elif data == "get_number" or data == "change_country":
        await query.edit_message_text(
            "🌍 <b>Select Country:</b>\n\nLoading your IVASMS numbers...",
            parse_mode='HTML',
            reply_markup=country_keyboard()
        )

    elif data.startswith("country_"):
        range_name = data.replace("country_", "")
        user_sessions[user_id] = {'country': range_name, 'number': None}

        numbers = get_ivasms_numbers()
        assigned_number = None
        for row in numbers:
            if len(row) >= 2 and row[1] == range_name:
                assigned_number = row[0]
                break

        if not assigned_number:
            assigned_number = "No number available"

        user_sessions[user_id]['number'] = assigned_number
        country = extract_country_from_range(range_name)
        emoji = get_country_emoji(country)

        await query.edit_message_text(
            f"""🔄 <b>Number Assigned Successfully</b>

━━━━━━━━━━━━━━━━━━━━
{emoji} <b>Range:</b> {range_name}
📱 <b>Number:</b> <code>{assigned_number}</code>
🟢 <b>Ready to receive OTP</b>
━━━━━━━━━━━━━━━━━━━━

Use this number to receive OTPs!""",
            parse_mode='HTML',
            reply_markup=number_assigned_keyboard()
        )

    elif data == "change_number":
        user_id_session = user_sessions.get(user_id, {})
        range_name = user_id_session.get('country', 'Unknown')
        current_number = user_id_session.get('number')

        numbers = get_ivasms_numbers()
        assigned_number = None
        for row in numbers:
            if len(row) >= 2 and row[1] == range_name and row[0] != current_number:
                assigned_number = row[0]
                break

        if not assigned_number:
            assigned_number = "No other number available"

        if user_id in user_sessions:
            user_sessions[user_id]['number'] = assigned_number

        country = extract_country_from_range(range_name)
        emoji = get_country_emoji(country)

        await query.edit_message_text(
            f"""🔄 <b>New Number Assigned!</b>

━━━━━━━━━━━━━━━━━━━━
{emoji} <b>Range:</b> {range_name}
📱 <b>Number:</b> <code>{assigned_number}</code>
🟢 <b>Ready to receive OTP</b>
━━━━━━━━━━━━━━━━━━━━""",
            parse_mode='HTML',
            reply_markup=number_assigned_keyboard()
        )

    elif data == "check":
        await query.edit_message_text("🔍 <b>Checking for new OTPs...</b>", parse_mode='HTML')
        messages = get_received_sms()
        if messages:
            await query.edit_message_text(
                f"✅ <b>Found {len(messages)} new OTP(s)! Forwarding now...</b>",
                parse_mode='HTML',
                reply_markup=main_menu_keyboard()
            )
            for msg in messages:
                await send_otp_to_group_async(msg)
        else:
            await query.edit_message_text(
                "📭 <b>No new OTPs found.</b>\n\nI check automatically every 10 seconds.",
                parse_mode='HTML',
                reply_markup=main_menu_keyboard()
            )

    elif data == "status":
        uptime = datetime.now() - bot_stats['start_time']
        uptime_str = str(uptime).split('.')[0]
        session_status = "🟢 Valid" if bot_stats['session_valid'] else "🔴 Expired — update cookies!"
        status_text = f"""📊 <b>NEXUSBOT Status</b>

⏱ <b>Uptime:</b> {uptime_str}
📨 <b>OTPs Sent:</b> {bot_stats['total_otps_sent']}
🕐 <b>Last Check:</b> {bot_stats['last_check']}
🔐 <b>Session:</b> {session_status}
🟢 <b>Monitor:</b> {'Running' if bot_stats['is_running'] else 'Stopped'}
❌ <b>Last Error:</b> {bot_stats['last_error'] or 'None'}"""
        await query.edit_message_text(status_text, parse_mode='HTML', reply_markup=main_menu_keyboard())

    elif data == "stats":
        uptime = datetime.now() - bot_stats['start_time']
        uptime_str = str(uptime).split('.')[0]
        stats_text = f"""📈 <b>Detailed Statistics</b>

⏱ <b>Started:</b> {bot_stats['start_time'].strftime('%Y-%m-%d %H:%M:%S')}
⏱ <b>Uptime:</b> {uptime_str}
📨 <b>Total OTPs Sent:</b> {bot_stats['total_otps_sent']}
🕐 <b>Last Check:</b> {bot_stats['last_check']}
🔁 <b>Check Interval:</b> Every 10 seconds
👥 <b>Active Users:</b> {len(user_sessions)}
🟢 <b>Monitor Running:</b> {'Yes' if bot_stats['is_running'] else 'No'}"""
        await query.edit_message_text(stats_text, parse_mode='HTML', reply_markup=main_menu_keyboard())

    elif data == "test":
        test_data = {
            'phone': '+22901440499',
            'otp': '840113',
            'service': 'WhatsApp',
            'country': '🇧🇯 Benin',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'message': 'Your WhatsApp code is 840-113. Do not share it.'
        }
        msg_text = format_otp_message(test_data)
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=msg_text,
            parse_mode='HTML',
            reply_markup=otp_buttons()
        )
        await query.edit_message_text(
            "✅ <b>Test OTP sent to the group!</b>",
            parse_mode='HTML',
            reply_markup=main_menu_keyboard()
        )


# ============================================================
# SEND OTP TO GROUP
# ============================================================

async def send_otp_to_group_async(data):
    try:
        msg_text = format_otp_message(data)
        await bot.send_message(
            chat_id=GROUP_ID,
            text=msg_text,
            parse_mode='HTML',
            reply_markup=otp_buttons()
        )
        mark_otp_sent(data['id'], data['otp'], data['message'])
        bot_stats['total_otps_sent'] += 1
        logger.info(f"✅ OTP sent: {data['otp']} | {data['service']} | {data['country']}")
    except Exception as e:
        logger.error(f"Failed to send OTP: {e}")


def send_otp_to_group(data):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_otp_to_group_async(data))
        loop.close()
    except Exception as e:
        logger.error(f"Error sending OTP: {e}")


# ============================================================
# BACKGROUND MONITOR
# ============================================================

def background_monitor():
    global bot_stats
    bot_stats['is_running'] = True
    logger.info("🔍 Background OTP monitor started")

    while bot_stats['is_running']:
        try:
            logger.info("Checking for new OTPs...")
            messages = get_received_sms()
            bot_stats['last_check'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if messages:
                logger.info(f"Found {len(messages)} new OTPs")
                for msg in messages:
                    send_otp_to_group(msg)
            else:
                logger.info("No new OTPs found")

            time.sleep(10)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
            bot_stats['last_error'] = str(e)
            time.sleep(30)


def start_telegram_bot():
    if telegram_app:
        def run_bot():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def start():
                await telegram_app.initialize()
                await telegram_app.start()
                await telegram_app.updater.start_polling(drop_pending_updates=True)
            loop.run_until_complete(start())
            loop.run_forever()
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        logger.info("✅ Telegram bot polling started")


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route('/')
def home():
    uptime = datetime.now() - bot_stats['start_time']
    return jsonify({
        'status': 'running',
        'bot': 'NEXUSBOT',
        'uptime': str(uptime).split('.')[0],
        'total_otps_sent': bot_stats['total_otps_sent'],
        'last_check': bot_stats['last_check'],
        'monitor_running': bot_stats['is_running'],
        'session_valid': bot_stats['session_valid'],
    })


@app.route('/check')
def manual_check():
    messages = get_received_sms()
    for msg in messages:
        send_otp_to_group(msg)
    return jsonify({'status': 'success', 'found': len(messages)})


@app.route('/status')
def status():
    uptime = datetime.now() - bot_stats['start_time']
    return jsonify({
        'uptime': str(uptime).split('.')[0],
        'total_otps_sent': bot_stats['total_otps_sent'],
        'last_check': bot_stats['last_check'],
        'is_running': bot_stats['is_running'],
        'session_valid': bot_stats['session_valid'],
        'last_error': bot_stats['last_error']
    })


@app.route('/relogin')
def relogin():
    """Reload cookies from env var (after you've updated IVASMS_COOKIES on Render)."""
    threading.Thread(target=ivasms_login, daemon=True).start()
    return jsonify({'status': 'Session reload started'})


# ============================================================
# MAIN
# ============================================================

def main():
    global bot, telegram_app

    logger.info("🚀 Starting NEXUSBOT...")

    if not all([BOT_TOKEN, GROUP_ID]):
        logger.error("❌ Missing BOT_TOKEN or GROUP_ID!")
        return

    # Load cookies from env var
    ivasms_login()

    bot = Bot(token=BOT_TOKEN)
    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("✅ Bot initialized")
    start_telegram_bot()

    def send_startup():
        time.sleep(3)
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def send():
                session_line = "✅ Session valid" if bot_stats['session_valid'] else "⚠️ Cookies expired — update IVASMS_COOKIES!"
                await bot.send_message(
                    chat_id=GROUP_ID,
                    text=(
                        f"🚀 <b>NEXUSBOT Started!</b>\n\n"
                        f"{session_line}\n"
                        f"✅ Monitoring every 10 seconds\n"
                        f"✅ Ready to forward OTPs"
                    ),
                    parse_mode='HTML',
                    reply_markup=otp_buttons()
                )
            loop.run_until_complete(send())
            loop.close()
        except Exception as e:
            logger.error(f"Startup message error: {e}")

    threading.Thread(target=send_startup, daemon=True).start()

    monitor_thread = threading.Thread(target=background_monitor, daemon=True)
    monitor_thread.start()

    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)


if __name__ == '__main__':
    main()
