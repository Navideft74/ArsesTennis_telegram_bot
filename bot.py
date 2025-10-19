import logging
import requests
import jdatetime
import os
import time
import sqlite3
from dotenv import load_dotenv
from datetime import date, timedelta
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden

# ---- Version : 3.0.4 : Re-implemented sticky video feature using environment variables.

# --- Load Environment Variables ---
load_dotenv()

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
API_BASE_URL = 'http://arsestennis.ir/reservations/api/'
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
GROUP_VIDEO_FILE_ID = os.getenv('GROUP_VIDEO_FILE_ID') # For the sticky video

MANAGER_NAME = "مدیریت (آقای آرسس)"
MANAGER_PHONE = "09155518828"
STAFF_NAME = "مسئول مجموعه (آقای افتخاری)"
STAFF_PHONE = "09306437158"

BUTTON_COOLDOWN_SECONDS = 5
DB_FILE = 'bot_state.db'
AUTO_UPDATE_INTERVAL_SECONDS = 300 # 5 minutes

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- DATABASE MANAGEMENT ---
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS group_schedules (chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS button_cooldowns (chat_id INTEGER PRIMARY KEY, last_used_timestamp REAL NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER NOT NULL)''')
    cur.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('updates_clicked', 0)")
    cur.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('auto_updates_processed', 0)")
    con.commit()
    con.close()
    logger.info(f"Database '{DB_FILE}' initialized successfully.")

def db_set_schedule_message(chat_id: int, message_id: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO group_schedules (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id))
    con.commit()
    con.close()
def db_get_schedule_message(chat_id: int) -> int | None:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    res = cur.execute("SELECT message_id FROM group_schedules WHERE chat_id = ?", (chat_id,))
    result = res.fetchone()
    con.close()
    return result[0] if result else None
def db_remove_schedule_message(chat_id: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM group_schedules WHERE chat_id = ?", (chat_id,))
    con.commit()
    con.close()
def db_get_cooldown(chat_id: int) -> float:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    res = cur.execute("SELECT last_used_timestamp FROM button_cooldowns WHERE chat_id = ?", (chat_id,))
    result = res.fetchone()
    con.close()
    return result[0] if result else 0.0
def db_set_cooldown(chat_id: int, timestamp: float):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO button_cooldowns (chat_id, last_used_timestamp) VALUES (?, ?)", (chat_id, timestamp))
    con.commit()
    con.close()
def db_increment_stat(key: str):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("UPDATE stats SET value = value + 1 WHERE key = ?", (key,))
    con.commit()
    con.close()
def db_get_stat(key: str) -> int:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    res = cur.execute("SELECT value FROM stats WHERE key = ?", (key,))
    result = res.fetchone()
    con.close()
    return result[0] if result else 0
def db_get_all_active_groups() -> list[int]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    res = cur.execute("SELECT chat_id FROM group_schedules")
    results = res.fetchall()
    con.close()
    return [row[0] for row in results]

# --- ADMIN DECORATOR ---
def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            logger.warning(f"Unauthorized access denied for {user_id}.")
            await update.message.reply_text("⛔️ شما اجازه استفاده از این دستور را ندارید.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- HELPER & CORE FUNCTIONS ---
PERSIAN_WEEKDAYS = {"Saturday": "شنبه", "Sunday": "یکشنبه", "Monday": "دوشنبه", "Tuesday": "سه‌شنبه", "Wednesday": "چهارشنبه", "Thursday": "پنجشنبه", "Friday": "جمعه"}

def create_group_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("🔄 بروزرسانی دستی (فقط ادمین)", callback_data='update_schedule_group')]]
    return InlineKeyboardMarkup(keyboard)
def create_private_chat_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    today = date.today()
    for i in range(2):
        d = today + timedelta(days=i)
        api_date = d.strftime('%Y-%m-%d')
        day_name = PERSIAN_WEEKDAYS.get(d.strftime('%A'))
        jalali_date = jdatetime.date.fromgregorian(date=d)
        label_date = jalali_date.strftime("%d %B")
        full_label = f"{day_name} {label_date}"
        prefix = "امروز - " if i == 0 else "فردا - "
        buttons.append(InlineKeyboardButton(prefix + full_label, callback_data=api_date))
    return InlineKeyboardMarkup([buttons])

def fetch_reservation_data(query_date: str) -> list | None:
    api_url = f"{API_BASE_URL}?date={query_date}"
    logger.info(f"Requesting data from: {api_url}")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    try:
        response = requests.get(api_url, timeout=15, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None

def format_schedule_message(data: list, query_date: str, auto_update=False) -> str:
    try:
        gregorian_dt = date.fromisoformat(query_date)
        jalali_date = jdatetime.date.fromgregorian(date=gregorian_dt)
        persian_day_name = PERSIAN_WEEKDAYS.get(gregorian_dt.strftime('%A'))
        persian_date_str = f"{persian_day_name} {jalali_date.strftime('%d %B %Y')}"
    except (ValueError, ImportError):
        persian_date_str = query_date
    
    last_update_time = jdatetime.datetime.now().strftime('%H:%M')
    update_info = f"*(بروزرسانی خودکار هر {int(AUTO_UPDATE_INTERVAL_SECONDS/60)} دقیقه - آخرین آپدیت: {last_update_time})*\n\n"
    
    message_header = f"📅 **تایم‌های امروز ({persian_date_str})**\n\n{update_info}"

    if not data:
        return message_header + "😕 در حال حاضر هیچ تایم قابل رزروی برای امروز ثبت نشده است."

    message_body = ""
    for court_data in data:
        court_name = court_data.get('court', {}).get('name', 'زمین نامشخص')
        message_body += f"🎾 **زمین: {court_name}**\n"
        time_slots = court_data.get('time_slots', [])
        if not time_slots:
            message_body += "هیچ سانسی برای این زمین وجود ندارد.\n"
        else:
            for slot in time_slots:
                start_time = slot.get('start_time', 'N/A')
                if slot.get('is_available', False):
                    message_body += f"✅ `{start_time}` - قابل رزرو\n"
                else:
                    user_name = slot.get('user', {}).get('full_name', 'شخصی')
                    message_body += f"❌ `{start_time}` - رزرو شده توسط {user_name}\n"
        message_body += "\n"
        
    return message_header + message_body


# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE: return
    reply_markup = create_private_chat_keyboard()
    welcome_message = "به ربات رزرو تنیس آرسس خوش آمدید! 🎾\n\nلطفا برای مشاهده برنامه، یکی از روزهای زیر را انتخاب کنید:"
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='Markdown')

@admin_only
async def setup_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.message.reply_text("این دستور فقط در گروه‌ها قابل استفاده است.")
        return
        
    today_gregorian_str = date.today().strftime('%Y-%m-%d')
    today_jalali = jdatetime.date.fromgregorian(date=date.today())
    today_persian_str = today_jalali.strftime("%A %d %B %Y")
    
    group_welcome_message = (
        f"🎾 **آکادمی تنیس آرسس (بروزرسانی {today_persian_str})**\n\n"
        "به گروه اطلاع‌رسانی تایم‌های آزاد خوش آمدید.\n\n"
        f"👇 **مهم:** لیست تایم‌ها در پیام زیر به صورت خودکار هر **{int(AUTO_UPDATE_INTERVAL_SECONDS/60)} دقیقه** بروزرسانی می‌شود.\n\n"
        "🌐 **وب‌سایت:** [arsestennis.ir](http://arsestennis.ir)\n"
        "📅 **رزرو آنلاین:** [arsestennis.ir/reservations/reserve-for-today](http://arsestennis.ir/reservations/reserve-for-today/)\n\n"
        "📞 **تماس جهت هماهنگی:**\n"
        f"▪️ **{STAFF_NAME}:** [{STAFF_PHONE}](tel:+98{STAFF_PHONE[1:]})\n"
        f"▪️ **{MANAGER_NAME}:** [{MANAGER_PHONE}](tel:+98{MANAGER_PHONE[1:]})"
    )
    reply_markup = create_group_keyboard()
    
    # NEW: Check if a video ID is configured and send it, otherwise send text.
    if GROUP_VIDEO_FILE_ID:
        await context.bot.send_video(
            chat_id=chat.id,
            video=GROUP_VIDEO_FILE_ID,
            caption=group_welcome_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await context.bot.send_message(
            chat_id=chat.id, text=group_welcome_message, reply_markup=reply_markup, parse_mode='Markdown'
        )
    
    reservation_data = fetch_reservation_data(today_gregorian_str)
    schedule_text = format_schedule_message(reservation_data, today_gregorian_str)
    schedule_message = await context.bot.send_message(
        chat_id=chat.id, text=schedule_text, parse_mode='Markdown'
    )
    db_set_schedule_message(chat.id, schedule_message.message_id)
    logger.info(f"Set up schedule message {schedule_message.message_id} for group {chat.id} in DB")
    await update.message.delete()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    chat_type = query.message.chat.type

    if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP] and query.data == 'update_schedule_group':
        user_id = query.from_user.id
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['administrator', 'creator']:
            await query.answer("این دکمه فقط برای ادمین‌ها فعال است.", show_alert=True)
            return

        current_time = time.time()
        if current_time - db_get_cooldown(chat_id) < BUTTON_COOLDOWN_SECONDS:
            await query.answer("لطفا چند لحظه صبر کنید.", show_alert=True)
            return
        db_set_cooldown(chat_id, current_time)
        db_increment_stat('updates_clicked')

        message_id_to_edit = db_get_schedule_message(chat_id)
        if not message_id_to_edit:
            await query.answer("خطا: پیام اصلی پیدا نشد. لطفا از ادمین بخواهید دوباره از دستور /setupgroup استفاده کند.", show_alert=True)
            return

        today_str = date.today().strftime('%Y-%m-%d')
        new_data = fetch_reservation_data(today_str)
        new_text = format_schedule_message(new_data, today_str)

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id_to_edit, text=new_text, parse_mode='Markdown'
            )
        except BadRequest:
            logger.warning(f"Could not find message {message_id_to_edit}. Recreating.")
            new_msg = await context.bot.send_message(chat_id=chat_id, text=new_text, parse_mode='Markdown')
            db_set_schedule_message(chat_id, new_msg.message_id)
        return

    elif chat_type == ChatType.PRIVATE:
        query_date = query.data
        await query.edit_message_text(text=f"لطفا صبر کنید، در حال دریافت برنامه برای تاریخ {query_date}...")
        reservation_data = fetch_reservation_data(query_date)
        message_text = format_schedule_message(reservation_data, query_date)
        await query.edit_message_text(text=message_text, parse_mode='Markdown')
        await context.bot.send_message(
            chat_id=chat_id, text="می‌توانید روز دیگری را انتخاب کنید:", reply_markup=create_private_chat_keyboard()
        )

async def date_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE: return
    user_input_date = update.message.text
    try:
        date.fromisoformat(user_input_date)
        await update.message.reply_text(f"دریافت شد! در حال دریافت برنامه برای تاریخ {user_input_date}...")
        reservation_data = fetch_reservation_data(user_input_date)
        message_text = format_schedule_message(reservation_data, user_input_date)
        await update.message.reply_text(text=message_text, parse_mode='Markdown')
        await context.bot.send_message(
            chat_id=update.message.chat_id, text="می‌توانید روز دیگری را انتخاب کنید:", reply_markup=create_private_chat_keyboard()
        )
    except ValueError:
        await update.message.reply_text("متاسفم، فرمت قابل فهم نیست. لطفا تاریخ را به صورت `YYYY-MM-DD` ارسال کنید.")

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active_groups = db_get_all_active_groups()
    count = len(active_groups)
    await update.message.reply_text(f"📊 ربات در حال حاضر در {count} گروه فعال است.")

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    updates_clicked = db_get_stat('updates_clicked')
    auto_updates = db_get_stat('auto_updates_processed')
    await update.message.reply_text(f"📈 **آمار استفاده ربات:**\n\n- بروزرسانی دستی (ادمین): {updates_clicked} بار\n- بروزرسانی خودکار: {auto_updates} بار")

@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_to_broadcast = " ".join(context.args)
    if not message_to_broadcast:
        await update.message.reply_text("⚠️ لطفا متن پیام خود را بعد از دستور وارد کنید.\nمثال: `/broadcast سلام به همه!`")
        return
    active_groups = db_get_all_active_groups()
    successful_sends, failed_sends = 0, 0
    await update.message.reply_text(f"📣 در حال ارسال پیام شما به {len(active_groups)} گروه...")
    for chat_id in active_groups:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message_to_broadcast)
            successful_sends += 1
        except (Forbidden, BadRequest):
            logger.warning(f"Could not send broadcast to group {chat_id}.")
            db_remove_schedule_message(chat_id)
            failed_sends += 1
    await update.message.reply_text(f"✅ پیام با موفقیت به {successful_sends} گروه ارسال شد.\n❌ در {failed_sends} گروه ارسال ناموفق بود.")

async def auto_update_schedules(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Running scheduled auto-update job...")
    today_str = date.today().strftime('%Y-%m-%d')
    active_groups = db_get_all_active_groups()

    if not active_groups:
        logger.info("Auto-update job finished: No active groups to update.")
        return

    new_data = fetch_reservation_data(today_str)
    
    for chat_id in active_groups:
        message_id = db_get_schedule_message(chat_id)
        if not message_id:
            continue

        new_text = format_schedule_message(new_data, today_str, auto_update=True)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=new_text, parse_mode='Markdown'
            )
            db_increment_stat('auto_updates_processed')
        except Forbidden:
            logger.warning(f"Auto-update failed for group {chat_id}: Bot is no longer an admin or was kicked. Removing from DB.")
            db_remove_schedule_message(chat_id)
        except BadRequest as e:
            if "message to edit not found" in str(e).lower():
                logger.warning(f"Auto-update failed for group {chat_id}: Original message not found. Recreating.")
                new_msg = await context.bot.send_message(chat_id=chat_id, text=new_text, parse_mode='Markdown')
                db_set_schedule_message(chat_id, new_msg.message_id)
            elif "message is not modified" not in str(e).lower():
                 logger.error(f"An unexpected BadRequest occurred during auto-update for group {chat_id}: {e}")

    logger.info(f"Auto-update job finished. Processed {len(active_groups)} groups.")

# --- MAIN APPLICATION SETUP ---
def main() -> None:
    """Starts the Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN not found in environment variables.")
        return
    if not ADMIN_IDS:
        logger.warning("Warning: ADMIN_IDS are not set in the environment variables. Admin commands will not work.")

    init_db()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.job_queue.run_repeating(
        auto_update_schedules,
        interval=AUTO_UPDATE_INTERVAL_SECONDS,
        first=10
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setupgroup", setup_group))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, date_message_handler))

    logger.info("Starting bot v3.0.4...")
    application.run_polling()

if __name__ == '__main__':
    main()

