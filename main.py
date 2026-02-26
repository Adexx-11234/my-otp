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
from datetime import datetime, timedelta
from flask import Flask, jsonify
from dotenv import load_dotenv
from urllib.parse import unquote
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import random
HAS_SELENIUM = False
try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    HAS_SELENIUM = True
    logger.info("✅ Selenium/undetected-chromedriver available")
except ImportError:
    logger.warning("⚠️ Selenium not available — will use requests only")

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
IVASMS_EMAIL = os.getenv('IVASMS_EMAIL')
IVASMS_PASSWORD = os.getenv('IVASMS_PASSWORD')
CHANNEL_LINK = os.getenv('CHANNEL_LINK', 'https://t.me/yourchannel')
DEV_LINK = os.getenv('DEV_LINK', 'https://t.me/yourdev')

LOGIN_URL = "https://www.ivasms.com/login"
PORTAL_URL = "https://www.ivasms.com/portal/sms/received"
SMS_LIST_URL = "https://www.ivasms.com/portal/sms/received/getsms"
SMS_NUMBERS_URL = "https://www.ivasms.com/portal/sms/received/getsms/number"
SMS_DETAILS_URL = "https://www.ivasms.com/portal/sms/received/getsms/number/sms"
NUMBERS_PAGE_URL = "https://www.ivasms.com/portal/numbers"

# From script 2 — full realistic browser headers
BASE_HEADERS = {
    "Host": "www.ivasms.com",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Not)A;Brand";v="8", "Chromium";v="138"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-GB,en;q=0.9",
    "Priority": "u=0, i",
    "Connection": "keep-alive"
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
    'consecutive_failures': 0,
}

user_sessions = {}
bot = None
telegram_app = None
ivasms_session = None
last_login_time = 0
csrf_token = None

# ============================================================
# HELPERS
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
# LOGIN — from script 2's approach (full browser simulation)
# ============================================================

def ivasms_login():
    global ivasms_session, last_login_time, csrf_token, bot_stats

    logger.info("🔐 Logging into IVASMS (requests method)...")
    session = requests.Session()

    try:
        # Warm up homepage first
        for attempt in range(3):
            try:
                resp = session.get("https://www.ivasms.com/", headers=BASE_HEADERS.copy(), timeout=15)
                if resp.status_code == 200:
                    logger.info("✅ Homepage warmed up")
                    break
            except Exception as e:
                logger.warning(f"Warmup attempt {attempt+1} failed: {e}")
                time.sleep(random.uniform(2, 4))

        time.sleep(random.uniform(1, 2))

        # Step 1: GET login page for _token
        resp = session.get(LOGIN_URL, headers=BASE_HEADERS.copy(), timeout=20)
        token_match = re.search(r'<input[^>]+name="_token"[^>]+value="([^"]+)"', resp.text)
        if not token_match:
            token_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
        if not token_match:
            logger.warning("Could not find _token — trying Selenium fallback...")
            return _selenium_login()

        _token = token_match.group(1)
        logger.info(f"✅ Got login token")
        time.sleep(random.uniform(1, 2))

        # Step 2: POST credentials
        login_headers = BASE_HEADERS.copy()
        login_headers.update({
            "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Referer": LOGIN_URL,
            "Origin": "https://www.ivasms.com",
        })
        login_data = {
            "_token": _token,
            "email": IVASMS_EMAIL,
            "password": IVASMS_PASSWORD,
            "remember": "on",
            "g-recaptcha-response": "",
            "submit": "register"
        }

        login_resp = session.post(LOGIN_URL, headers=login_headers, data=login_data, timeout=20, allow_redirects=True)
        logger.info(f"Login response: {login_resp.status_code} → {login_resp.url}")

        if login_resp.url.endswith("/login"):
            logger.warning("❌ Requests login failed — trying Selenium fallback...")
            return _selenium_login()

        time.sleep(random.uniform(1, 2))

        # Step 3: GET portal for CSRF token
        portal_headers = BASE_HEADERS.copy()
        portal_headers.update({
            "Sec-Fetch-Site": "same-origin",
            "Referer": "https://www.ivasms.com/portal",
        })
        portal_resp = session.get(PORTAL_URL, headers=portal_headers, timeout=20)
        logger.info(f"Portal: {portal_resp.status_code} → {portal_resp.url}")

        if 'login' in portal_resp.url:
            logger.warning("❌ Portal blocked — trying Selenium fallback...")
            return _selenium_login()

        csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', portal_resp.text)
        if not csrf_match:
            logger.warning("No CSRF in portal — trying Selenium fallback...")
            return _selenium_login()

        csrf_token = csrf_match.group(1)
        ivasms_session = session
        last_login_time = time.time()
        bot_stats['session_valid'] = True
        bot_stats['consecutive_failures'] = 0
        logger.info("✅ Requests login successful!")
        return True

    except Exception as e:
        logger.error(f"Requests login error: {e} — trying Selenium fallback...")
        return _selenium_login()


def _selenium_login():
    """Selenium fallback using undetected-chromedriver."""
    global ivasms_session, last_login_time, csrf_token, bot_stats

    if not HAS_SELENIUM:
        logger.error("❌ Selenium not available and requests failed. Login impossible.")
        bot_stats['session_valid'] = False
        return False

    logger.info("🤖 Trying Selenium login...")
    driver = None
    try:
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--headless=new")
        options.add_argument(f"--user-agent={BASE_HEADERS['User-Agent']}")

        driver = uc.Chrome(options=options, version_main=None)
        logger.info("✅ Chrome driver started")

        # Warm up
        driver.get("https://www.ivasms.com/")
        time.sleep(random.uniform(2, 4))

        # Go to login
        driver.get(LOGIN_URL)
        time.sleep(random.uniform(3, 5))

        # Wait for form
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.NAME, "email")))

        # Type email
        email_input = driver.find_element(By.NAME, "email")
        for char in IVASMS_EMAIL:
            email_input.send_keys(char)
            time.sleep(random.uniform(0.05, 0.15))

        time.sleep(random.uniform(0.5, 1))

        # Type password
        pass_input = driver.find_element(By.NAME, "password")
        for char in IVASMS_PASSWORD:
            pass_input.send_keys(char)
            time.sleep(random.uniform(0.05, 0.15))

        time.sleep(random.uniform(0.5, 1))

        # Submit
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        time.sleep(random.uniform(4, 6))

        current_url = driver.current_url
        logger.info(f"After Selenium login URL: {current_url}")

        if 'login' in current_url:
            logger.error("❌ Selenium login also failed")
            bot_stats['session_valid'] = False
            return False

        # Extract cookies from Selenium and put into requests session
        selenium_cookies = driver.get_cookies()
        session = requests.Session()
        session.headers.update({'User-Agent': BASE_HEADERS['User-Agent']})
        for cookie in selenium_cookies:
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', 'www.ivasms.com'))

        logger.info(f"✅ Extracted {len(selenium_cookies)} cookies from Selenium")

        # Get CSRF from portal
        portal_resp = session.get(PORTAL_URL, timeout=20)
        csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', portal_resp.text)
        if csrf_match:
            csrf_token = csrf_match.group(1)
            logger.info("✅ Got CSRF token from Selenium session")

        ivasms_session = session
        last_login_time = time.time()
        bot_stats['session_valid'] = True
        bot_stats['consecutive_failures'] = 0
        logger.info("✅ Selenium login successful!")
        return True

    except Exception as e:
        logger.error(f"Selenium login error: {e}")
        bot_stats['session_valid'] = False
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

def refresh_session_if_needed():
    global last_login_time
    # Re-login every 90 minutes
    if time.time() - last_login_time >= 5400:
        logger.info("🔄 Session refresh (90min)...")
        ivasms_login()

# ============================================================
# SMS FETCHING — merged best of both scripts
# ============================================================

def fetch_sms_ranges():
    global csrf_token
    try:
        today = datetime.now()
        from_date = today.strftime("%m/%d/%Y")
        to_date = (today + timedelta(days=1)).strftime("%m/%d/%Y")

        boundary = "----WebKitFormBoundaryhkp0qMozYkZV6Ham"
        headers = BASE_HEADERS.copy()
        headers.update({
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": PORTAL_URL,
            "Origin": "https://www.ivasms.com",
        })

        body = (
            f"------WebKitFormBoundaryhkp0qMozYkZV6Ham\r\n"
            f"Content-Disposition: form-data; name=\"from\"\r\n\r\n{from_date}\r\n"
            f"------WebKitFormBoundaryhkp0qMozYkZV6Ham\r\n"
            f"Content-Disposition: form-data; name=\"to\"\r\n\r\n{to_date}\r\n"
            f"------WebKitFormBoundaryhkp0qMozYkZV6Ham\r\n"
            f"Content-Disposition: form-data; name=\"_token\"\r\n\r\n{csrf_token}\r\n"
            f"------WebKitFormBoundaryhkp0qMozYkZV6Ham--\r\n"
        )

        resp = ivasms_session.post(SMS_LIST_URL, headers=headers, data=body, timeout=30)
        logger.info(f"SMS ranges response: {resp.status_code}")

        if resp.status_code != 200:
            return []

        # Parse ranges — try both parsing methods
        soup = BeautifulSoup(resp.text, 'html.parser')
        ranges = []

        # Method from script 2 (card-based)
        cards = soup.find_all('div', class_='card card-body mb-1 pointer')
        for card in cards:
            onclick = card.get('onclick', '')
            range_id_match = re.search(r"getDetials\('([^']+)'\)", onclick)
            if range_id_match:
                ranges.append(range_id_match.group(1))

        # Fallback: method from original script (item-based)
        if not ranges:
            items = soup.find_all('div', class_='item')
            for item in items:
                range_div = item.find('div', class_='col-sm-4')
                if range_div:
                    ranges.append(range_div.text.strip())

        logger.info(f"Found ranges: {ranges}")
        return ranges

    except Exception as e:
        logger.error(f"Error fetching ranges: {e}")
        return []

def fetch_numbers_for_range(range_name):
    global csrf_token
    try:
        today = datetime.now()
        to_date = (today + timedelta(days=1)).strftime("%m/%d/%Y")

        headers = BASE_HEADERS.copy()
        headers.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": PORTAL_URL,
            "Origin": "https://www.ivasms.com",
        })

        data = {
            "_token": csrf_token,
            "start": "",
            "end": to_date,
            "range": range_name
        }

        resp = ivasms_session.post(SMS_NUMBERS_URL, headers=headers, data=data, timeout=30)
        soup = BeautifulSoup(resp.text, 'html.parser')

        numbers = []

        # Script 2 parsing (card-based with onclick)
        number_divs = soup.find_all('div', class_='card card-body border-bottom bg-100 p-2 rounded-0')
        for div in number_divs:
            col = div.find('div', class_=re.compile(r'col'))
            if col:
                onclick = col.get('onclick', '')
                match = re.search(r"'([^']+)','([^']+)'", onclick)
                if match:
                    numbers.append(match.group(1))

        # Fallback: original parsing
        if not numbers:
            divs = soup.find_all('div', class_='col-sm-4')
            numbers = [d.text.strip() for d in divs if d.text.strip()]

        return numbers

    except Exception as e:
        logger.error(f"Error fetching numbers for {range_name}: {e}")
        return []

def fetch_sms_for_number(number, range_name):
    global csrf_token
    try:
        today = datetime.now()
        to_date = (today + timedelta(days=1)).strftime("%m/%d/%Y")

        headers = BASE_HEADERS.copy()
        headers.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": PORTAL_URL,
            "Origin": "https://www.ivasms.com",
        })

        data = {
            "_token": csrf_token,
            "start": "",
            "end": to_date,
            "Number": number,
            "Range": range_name
        }

        resp = ivasms_session.post(SMS_DETAILS_URL, headers=headers, data=data, timeout=30)
        soup = BeautifulSoup(resp.text, 'html.parser')

        messages = []

        # Script 2 parsing
        msg_divs = soup.find_all('div', class_='col-9 col-sm-6 text-center text-sm-start')
        for div in msg_divs:
            p = div.find('p')
            if p:
                messages.append(p.text.strip())

        # Fallback: original parsing
        if not messages:
            msg_divs = soup.select('div.col-9.col-sm-6 p.mb-0.pb-0')
            messages = [d.text.strip() for d in msg_divs]

        return messages

    except Exception as e:
        logger.error(f"Error fetching SMS for {number}: {e}")
        return []

def get_ivasms_numbers():
    try:
        resp = ivasms_session.get(NUMBERS_PAGE_URL, headers=BASE_HEADERS, timeout=15)
        soup = BeautifulSoup(resp.content, 'html.parser')
        numbers = []
        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')[1:]
            for row in rows:
                cells = row.find_all('td')
                if cells:
                    numbers.append([c.get_text(strip=True) for c in cells])
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

        ranges = fetch_sms_ranges()
        if not ranges:
            # Try re-login once if no ranges
            logger.warning("No ranges found, attempting re-login...")
            if ivasms_login():
                ranges = fetch_sms_ranges()
            if not ranges:
                return []

        for range_name in ranges:
            try:
                numbers = fetch_numbers_for_range(range_name)
                country_name = extract_country_from_range(range_name)
                country_emoji = get_country_emoji(country_name)

                for number in numbers:
                    try:
                        sms_list = fetch_sms_for_number(number, range_name)
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

    return f"""✅ {country} | {service} OTP Received

━━━━━━━━━━━━━━━━━━━━
📱 <b>Number:</b> {masked}
🔑 <b>OTP Code:</b> <code>{otp}</code>
🛠 <b>Service:</b> {service}
🌍 <b>Country:</b> {country}
🕐 <b>Time:</b> {timestamp}
━━━━━━━━━━━━━━━━━━━━

💬 <b>Message:</b>
<blockquote>{message}</blockquote>"""

# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 <b>Welcome to NEXUSBOT!</b>\n\nI monitor IVASMS for new OTPs and forward them instantly.\n\nUse the menu below to get started:",
        parse_mode='HTML',
        reply_markup=main_menu_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "menu":
        await query.edit_message_text("🏠 <b>Main Menu</b>\n\nChoose an option:", parse_mode='HTML', reply_markup=main_menu_keyboard())

    elif data in ("get_number", "change_country"):
        await query.edit_message_text("🌍 <b>Select Country:</b>\n\nLoading your IVASMS numbers...", parse_mode='HTML', reply_markup=country_keyboard())

    elif data.startswith("country_"):
        range_name = data.replace("country_", "")
        user_sessions[user_id] = {'country': range_name, 'number': None}
        numbers = get_ivasms_numbers()
        assigned_number = next((row[0] for row in numbers if len(row) >= 2 and row[1] == range_name), "No number available")
        user_sessions[user_id]['number'] = assigned_number
        country = extract_country_from_range(range_name)
        emoji = get_country_emoji(country)
        await query.edit_message_text(
            f"🔄 <b>Number Assigned Successfully</b>\n\n━━━━━━━━━━━━━━━━━━━━\n{emoji} <b>Range:</b> {range_name}\n📱 <b>Number:</b> <code>{assigned_number}</code>\n🟢 <b>Ready to receive OTP</b>\n━━━━━━━━━━━━━━━━━━━━\n\nUse this number to receive OTPs!",
            parse_mode='HTML', reply_markup=number_assigned_keyboard()
        )

    elif data == "change_number":
        session_data = user_sessions.get(user_id, {})
        range_name = session_data.get('country', 'Unknown')
        current_number = session_data.get('number')
        numbers = get_ivasms_numbers()
        assigned_number = next((row[0] for row in numbers if len(row) >= 2 and row[1] == range_name and row[0] != current_number), "No other number available")
        if user_id in user_sessions:
            user_sessions[user_id]['number'] = assigned_number
        country = extract_country_from_range(range_name)
        emoji = get_country_emoji(country)
        await query.edit_message_text(
            f"🔄 <b>New Number Assigned!</b>\n\n━━━━━━━━━━━━━━━━━━━━\n{emoji} <b>Range:</b> {range_name}\n📱 <b>Number:</b> <code>{assigned_number}</code>\n🟢 <b>Ready to receive OTP</b>\n━━━━━━━━━━━━━━━━━━━━",
            parse_mode='HTML', reply_markup=number_assigned_keyboard()
        )

    elif data == "check":
        await query.edit_message_text("🔍 <b>Checking for new OTPs...</b>", parse_mode='HTML')
        messages = get_received_sms()
        if messages:
            await query.edit_message_text(f"✅ <b>Found {len(messages)} new OTP(s)! Forwarding now...</b>", parse_mode='HTML', reply_markup=main_menu_keyboard())
            for msg in messages:
                await send_otp_to_group_async(msg)
        else:
            await query.edit_message_text("📭 <b>No new OTPs found.</b>\n\nI check automatically every 10 seconds.", parse_mode='HTML', reply_markup=main_menu_keyboard())

    elif data == "status":
        uptime = str(datetime.now() - bot_stats['start_time']).split('.')[0]
        session_status = "🟢 Valid" if bot_stats['session_valid'] else "🔴 Expired"
        await query.edit_message_text(
            f"📊 <b>NEXUSBOT Status</b>\n\n⏱ <b>Uptime:</b> {uptime}\n📨 <b>OTPs Sent:</b> {bot_stats['total_otps_sent']}\n🕐 <b>Last Check:</b> {bot_stats['last_check']}\n🔐 <b>Session:</b> {session_status}\n🟢 <b>Monitor:</b> {'Running' if bot_stats['is_running'] else 'Stopped'}\n❌ <b>Last Error:</b> {bot_stats['last_error'] or 'None'}",
            parse_mode='HTML', reply_markup=main_menu_keyboard()
        )

    elif data == "stats":
        uptime = str(datetime.now() - bot_stats['start_time']).split('.')[0]
        await query.edit_message_text(
            f"📈 <b>Detailed Statistics</b>\n\n⏱ <b>Started:</b> {bot_stats['start_time'].strftime('%Y-%m-%d %H:%M:%S')}\n⏱ <b>Uptime:</b> {uptime}\n📨 <b>Total OTPs Sent:</b> {bot_stats['total_otps_sent']}\n🕐 <b>Last Check:</b> {bot_stats['last_check']}\n🔁 <b>Check Interval:</b> Every 10 seconds\n👥 <b>Active Users:</b> {len(user_sessions)}\n🟢 <b>Monitor Running:</b> {'Yes' if bot_stats['is_running'] else 'No'}",
            parse_mode='HTML', reply_markup=main_menu_keyboard()
        )

    elif data == "test":
        test_data = {
            'phone': '+22901440499', 'otp': '840113', 'service': 'WhatsApp',
            'country': '🇧🇯 Benin', 'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'message': 'Your WhatsApp code is 840-113. Do not share it.'
        }
        await context.bot.send_message(chat_id=GROUP_ID, text=format_otp_message(test_data), parse_mode='HTML', reply_markup=otp_buttons())
        await query.edit_message_text("✅ <b>Test OTP sent to the group!</b>", parse_mode='HTML', reply_markup=main_menu_keyboard())

# ============================================================
# SEND OTP TO GROUP
# ============================================================

async def send_otp_to_group_async(data):
    try:
        await bot.send_message(chat_id=GROUP_ID, text=format_otp_message(data), parse_mode='HTML', reply_markup=otp_buttons())
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
                bot_stats['consecutive_failures'] = 0
            else:
                logger.info("No new OTPs found")

            time.sleep(10)

        except Exception as e:
            logger.error(f"Monitor error: {e}")
            bot_stats['last_error'] = str(e)
            bot_stats['consecutive_failures'] += 1
            if bot_stats['consecutive_failures'] >= 5:
                logger.warning("5 consecutive failures — re-logging in...")
                ivasms_login()
                bot_stats['consecutive_failures'] = 0
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
        threading.Thread(target=run_bot, daemon=True).start()
        logger.info("✅ Telegram bot polling started")

# ============================================================
# FLASK ROUTES
# ============================================================

@app.route('/')
def home():
    uptime = datetime.now() - bot_stats['start_time']
    return jsonify({
        'status': 'running', 'bot': 'NEXUSBOT',
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
    threading.Thread(target=ivasms_login, daemon=True).start()
    return jsonify({'status': 'Relogin started'})

# ============================================================
# MAIN
# ============================================================

def main():
    global bot, telegram_app

    logger.info("🚀 Starting NEXUSBOT...")

    if not all([BOT_TOKEN, GROUP_ID, IVASMS_EMAIL, IVASMS_PASSWORD]):
        logger.error("❌ Missing required env vars!")
        return

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
                session_line = "✅ Session valid" if bot_stats['session_valid'] else "⚠️ Login failed — check credentials"
                await bot.send_message(
                    chat_id=GROUP_ID,
                    text=f"🚀 <b>NEXUSBOT Started!</b>\n\n{session_line}\n✅ Monitoring every 10 seconds\n✅ Ready to forward OTPs",
                    parse_mode='HTML', reply_markup=otp_buttons()
                )
            loop.run_until_complete(send())
            loop.close()
        except Exception as e:
            logger.error(f"Startup message error: {e}")

    threading.Thread(target=send_startup, daemon=True).start()
    threading.Thread(target=background_monitor, daemon=True).start()

    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == '__main__':
    main()
