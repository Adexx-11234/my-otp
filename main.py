import os
import asyncio
import logging
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import threading
import time
import json

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROUP_ID = os.getenv('TELEGRAM_GROUP_ID')
IVASMS_EMAIL = os.getenv('IVASMS_EMAIL')
IVASMS_PASSWORD = os.getenv('IVASMS_PASSWORD')
CHANNEL_LINK = os.getenv('CHANNEL_LINK', 'https://t.me/yourchannel')
DEV_LINK = os.getenv('DEV_LINK', 'https://t.me/yourdev')

bot_stats = {
    'start_time': datetime.now(),
    'total_otps_sent': 0,
    'last_check': 'Never',
    'last_error': None,
    'is_running': False
}

user_sessions = {}
sent_otps = set()

bot = None
telegram_app = None
ivasms_session = None
ivasms_logged_in = False

# ============================================================
# IVASMS SCRAPER
# ============================================================

def ivasms_login():
    global ivasms_session, ivasms_logged_in
    try:
        ivasms_session = requests.Session()
        ivasms_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })

        login_url = "https://www.ivasms.com/login"
        resp = ivasms_session.get(login_url, timeout=15)
        soup = BeautifulSoup(resp.content, 'html.parser')

        csrf = None
        csrf_input = soup.find('input', {'name': '_token'})
        if csrf_input:
            csrf = csrf_input.get('value')
            logger.info(f"✅ CSRF token found")
        else:
            logger.warning("⚠️ No CSRF token found")

        data = {'email': IVASMS_EMAIL, 'password': IVASMS_PASSWORD}
        if csrf:
            data['_token'] = csrf

        login_resp = ivasms_session.post(login_url, data=data, timeout=15)
        logger.info(f"Login response URL: {login_resp.url}")
        logger.info(f"Login response status: {login_resp.status_code}")

        if 'portal' in login_resp.url or 'dashboard' in login_resp.url:
            ivasms_logged_in = True
            logger.info("✅ IVASMS login successful (URL redirect confirmed)")
            return True

        soup2 = BeautifulSoup(login_resp.content, 'html.parser')
        if soup2.find(string=re.compile(r'logout|dashboard|portal', re.I)):
            ivasms_logged_in = True
            logger.info("✅ IVASMS login successful (page content confirmed)")
            return True

        logger.warning(f"⚠️ Login failed - final URL was: {login_resp.url}")
        logger.warning("Continuing anyway...")
        ivasms_logged_in = True
        return True

    except Exception as e:
        logger.error(f"IVASMS login error: {e}")
        return False

def get_ivasms_numbers():
    global ivasms_session, ivasms_logged_in
    try:
        if not ivasms_logged_in:
            ivasms_login()

        url = "https://www.ivasms.com/portal/numbers"
        resp = ivasms_session.get(url, timeout=15)
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
        logger.error(f"Error fetching numbers: {e}")
        return []


def get_received_sms():
    global ivasms_session, ivasms_logged_in
    try:
        if not ivasms_logged_in:
            ivasms_login()

        url = "https://www.ivasms.com/portal/sms/received"
        resp = ivasms_session.get(url, timeout=15)

        if resp.status_code == 401 or 'login' in resp.url:
            ivasms_login()
            resp = ivasms_session.get(url, timeout=15)

        soup = BeautifulSoup(resp.content, 'html.parser')
        messages = []

        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')[1:]
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 3:
                    texts = [c.get_text(strip=True) for c in cells]
                    phone = ''
                    service = ''
                    message = ''
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                    for t in texts:
                        if re.search(r'\+?\d{8,15}', t):
                            phone = t
                        elif re.search(r'\d{4}:\d{2}|\d{2}/\d{2}', t):
                            timestamp = t
                        elif len(t) > 15:
                            message = t

                    otp_match = re.search(r'\b(\d{4,8})\b', message)
                    if otp_match:
                        otp = otp_match.group(1)
                        for svc in ['WhatsApp', 'Facebook', 'Instagram', 'Twitter', 'Telegram', 'Google', 'TikTok', 'Discord']:
                            if svc.lower() in message.lower():
                                service = svc
                                break
                        if not service:
                            service = 'Unknown'

                        msg_id = f"{phone}_{otp}_{timestamp}"
                        if msg_id not in sent_otps:
                            messages.append({
                                'id': msg_id,
                                'phone': phone or 'Unknown',
                                'otp': otp,
                                'service': service,
                                'message': message,
                                'timestamp': timestamp,
                                'country': detect_country(phone)
                            })

        try:
            live_url = "https://www.ivasms.com/portal/live/my_sms"
            live_resp = ivasms_session.get(live_url, timeout=15)
            live_soup = BeautifulSoup(live_resp.content, 'html.parser')
            live_tables = live_soup.find_all('table')
            for table in live_tables:
                rows = table.find_all('tr')[1:]
                for row in rows:
                    cells = row.find_all('td')
                    if len(cells) >= 3:
                        texts = [c.get_text(strip=True) for c in cells]
                        phone = ''
                        message = ''
                        timestamp = datetime.now().strftime('%H:%M:%S')
                        for t in texts:
                            if re.search(r'\+?\d{8,15}', t):
                                phone = t
                            elif len(t) > 15:
                                message = t
                        otp_match = re.search(r'\b(\d{4,8})\b', message)
                        if otp_match:
                            otp = otp_match.group(1)
                            service = 'Unknown'
                            for svc in ['WhatsApp', 'Facebook', 'Instagram', 'Twitter', 'Telegram', 'Google', 'TikTok']:
                                if svc.lower() in message.lower():
                                    service = svc
                                    break
                            msg_id = f"{phone}_{otp}_{timestamp}"
                            if msg_id not in sent_otps:
                                messages.append({
                                    'id': msg_id,
                                    'phone': phone or 'Unknown',
                                    'otp': otp,
                                    'service': service,
                                    'message': message,
                                    'timestamp': timestamp,
                                    'country': detect_country(phone)
                                })
        except:
            pass

        return messages

    except Exception as e:
        logger.error(f"Error fetching SMS: {e}")
        return []


def detect_country(phone):
    """Get country from IVASMS numbers list by matching phone number"""
    try:
        numbers = get_ivasms_numbers()
        for row in numbers:
            if len(row) >= 2:
                number = row[0]
                range_name = row[1]  # e.g "BENIN 761"
                if phone and number and phone.replace('+', '').replace(' ', '') in number.replace('+', '').replace(' ', ''):
                    # Extract just the country name from range name (remove the number at end)
                    country_name = ' '.join(range_name.split()[:-1])  # "BENIN 761" → "BENIN"
                    return country_name.title()  # "Benin"
    except:
        pass
    return '🌍 Unknown'


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
        row.append(InlineKeyboardButton(
            f"📱 {range_name}",
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
    welcome = """🏠 <b>Welcome to OTP Bot!</b>

I monitor IVASMS for new OTPs and forward them to your group instantly.

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
            "🌍 <b>Select Country:</b>\n\nChoose a country to get a virtual number:",
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

        await query.edit_message_text(
            f"""🔄 <b>Number Assigned Successfully</b>

━━━━━━━━━━━━━━━━━━━━
🌍 <b>Range:</b> {range_name}
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

        await query.edit_message_text(
            f"""🔄 <b>New Number Assigned!</b>

━━━━━━━━━━━━━━━━━━━━
🌍 <b>Range:</b> {range_name}
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
        status_text = f"""📊 <b>Bot Status</b>

⏱ <b>Uptime:</b> {uptime_str}
📨 <b>OTPs Sent:</b> {bot_stats['total_otps_sent']}
🕐 <b>Last Check:</b> {bot_stats['last_check']}
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
            'phone': '+8493***2484',
            'otp': '840113',
            'service': 'WhatsApp',
            'country': '🇻🇳 Vietnam',
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
        sent_otps.add(data['id'])
        bot_stats['total_otps_sent'] += 1
        logger.info(f"✅ OTP sent: {data['otp']} for {data['service']}")
    except Exception as e:
        logger.error(f"Failed to send OTP to group: {e}")


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
            time.sleep(120)


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
        'uptime': str(uptime).split('.')[0],
        'total_otps_sent': bot_stats['total_otps_sent'],
        'last_check': bot_stats['last_check'],
        'monitor_running': bot_stats['is_running']
    })

@app.route('/check')
def manual_check():
    messages = get_received_sms()
    for msg in messages:
        send_otp_to_group(msg)
    return jsonify({'status': 'success', 'found': len(messages)})

@app.route('/status')
def status():
    return jsonify(bot_stats)


# ============================================================
# MAIN
# ============================================================

def main():
    global bot, telegram_app

    logger.info("🚀 Starting OTP Bot...")

    if not all([BOT_TOKEN, GROUP_ID, IVASMS_EMAIL, IVASMS_PASSWORD]):
        logger.error("❌ Missing environment variables!")
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
                await bot.send_message(
                    chat_id=GROUP_ID,
                    text="🚀 <b>OTP Bot Started!</b>\n\n✅ IVASMS connected\n✅ Monitoring every 10 seconds\n✅ Ready to forward OTPs",
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
