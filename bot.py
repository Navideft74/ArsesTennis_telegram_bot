import logging
import requests
import jdatetime
import os
import time
from dotenv import load_dotenv
from datetime import date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatType
from telegram.error import BadRequest

# ---- Version : 3.0.0 : Major refactor of group functionality for a dynamic, updatable message system.

# --- Load Environment Variables ---
load_dotenv()

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
API_BASE_URL = 'http://127.0.0.1:8000/reservations/api/'

MANAGER_NAME = "مدیریت (آقای آرسس)"
MANAGER_PHONE = "09155518828"
STAFF_NAME = "مسئول مجموعه (آقای افتخاری)"
STAFF_PHONE = "09306437158"

# --- NEW ---
BUTTON_COOLDOWN_SECONDS = 5

# --- IN-MEMORY STATE STORAGE ---
# NOTE: These dictionaries are in-memory. If the bot restarts, this state is lost.
# A new /setupgroup command will be required to re-establish the link.
group_schedule_messages = {} # Stores {chat_id: message_id_of_schedule}
button_last_used = {}      # Stores {chat_id: timestamp_of_last_click}


# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- HELPER FUNCTIONS ---
PERSIAN_WEEKDAYS = {
    "Saturday": "شنبه", "Sunday": "یکشنبه", "Monday": "دوشنبه",
    "Tuesday": "سه‌شنبه", "Wednesday": "چهارشنبه", "Thursday": "پنجشنبه", "Friday": "جمعه",
}

# --- REFACTORED for V3.0.0 ---
def create_group_keyboard() -> InlineKeyboardMarkup:
    """Creates the single 'Update' button for the main group message."""
    keyboard = [[
        InlineKeyboardButton("🔄 بروزرسانی تایم های رزرو شده", callback_data='update_schedule_group')
    ]]
    return InlineKeyboardMarkup(keyboard)

def create_private_chat_keyboard() -> InlineKeyboardMarkup:
    """Creates the 'Today' and 'Tomorrow' buttons for private chats."""
    buttons = []
    today = date.today()
    for i in range(2):
        current_gregorian_date = today + timedelta(days=i)
        date_str_for_api = current_gregorian_date.strftime('%Y-%m-%d')
        english_day_name = current_gregorian_date.strftime('%A')
        persian_day_name = PERSIAN_WEEKDAYS.get(english_day_name, english_day_name)
        jalali_date = jdatetime.date.fromgregorian(date=current_gregorian_date)
        persian_date_part = jalali_date.strftime("%d %B")
        persian_date_label = f"{persian_day_name} {persian_date_part}"
        label = f"امروز - {persian_date_label}" if i == 0 else f"فردا - {persian_date_label}"
        buttons.append(InlineKeyboardButton(label, callback_data=date_str_for_api))
    return InlineKeyboardMarkup([buttons])


# --- CORE LOGIC ---
def fetch_reservation_data(query_date: str) -> list | None:
    """Fetches reservation data from your API for a specific date."""
    api_url = f"{API_BASE_URL}?date={query_date}"
    logger.info(f"Requesting data from: {api_url}")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        response = requests.get(api_url, timeout=15, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None

def format_schedule_message(data: list, query_date: str) -> str:
    """Formats the JSON data into a user-friendly Persian message."""
    try:
        gregorian_dt = date.fromisoformat(query_date)
        jalali_date = jdatetime.date.fromgregorian(date=gregorian_dt)
        english_day_name = gregorian_dt.strftime('%A')
        persian_day_name = PERSIAN_WEEKDAYS.get(english_day_name, english_day_name)
        persian_date_str = f"{persian_day_name} {jalali_date.strftime('%d %B %Y')}"
    except (ValueError, ImportError):
        persian_date_str = query_date
    
    if not data:
        return f"📅 **تایم‌های امروز ({persian_date_str})**\n\n😕 در حال حاضر هیچ تایم قابل رزروی برای امروز ثبت نشده است."

    message = f"📅 **تایم‌های امروز ({persian_date_str})**\n\n"
    for court_data in data:
        court_name = court_data.get('court', {}).get('name', 'زمین نامشخص')
        message += f"🎾 **زمین: {court_name}**\n"
        time_slots = court_data.get('time_slots', [])
        if not time_slots:
            message += "هیچ سانسی برای این زمین وجود ندارد.\n"
        else:
            for slot in time_slots:
                start_time = slot.get('start_time', 'N/A')
                if slot.get('is_available', False):
                    message += f"✅ `{start_time}` - قابل رزرو\n"
                else:
                    user_info = slot.get('user', {})
                    user_name = user_info.get('full_name', 'شخصی') if user_info else 'شخصی'
                    message += f"❌ `{start_time}` - رزرو شده توسط {user_name}\n"
        message += "\n"
        
    return message


# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /start. Works only in private chats."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return 
    
    reply_markup = create_private_chat_keyboard()
    welcome_message = "به ربات رزرو تنیس آرسس خوش آمدید! 🎾\n\nلطفا برای مشاهده برنامه، یکی از روزهای زیر را انتخاب کنید:"
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='Markdown')

# --- REFACTORED for V3.0.0 ---
async def setup_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only command to post the initial welcome and schedule messages in a group."""
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.message.reply_text("این دستور فقط در گروه‌ها قابل استفاده است.")
        return

    member = await chat.get_member(user_id)
    if member.status not in ['administrator', 'creator']:
        await update.message.reply_text("فقط ادمین گروه می‌تواند از این دستور استفاده کند.")
        return
        
    today_gregorian_str = date.today().strftime('%Y-%m-%d')
    today_jalali = jdatetime.date.fromgregorian(date=date.today())
    today_persian_str = today_jalali.strftime("%A %d %B %Y")

    # Message A: The Welcome Message (to be pinned)
    group_welcome_message = (
        f"🎾 **آکادمی تنیس آرسس (بروزرسانی {today_persian_str})**\n\n"
        "به گروه اطلاع‌رسانی تایم‌های آزاد خوش آمدید. برای مشاهده آخرین وضعیت سانس‌های امروز، دکمه زیر را فشار دهید.\n\n"
        "🌐 **وب‌سایت:** [arsestennis.ir](http://arsestennis.ir)\n"
        "📅 **رزرو آنلاین:** [arsestennis.ir/reservations/reserve-for-today](http://arsestennis.ir/reservations/reserve-for-today/)\n\n"
        "📞 **تماس جهت هماهنگی:**\n"
        f"▪️ **{STAFF_NAME}:** [{STAFF_PHONE}](tel:+98{STAFF_PHONE[1:]})\n"
        f"▪️ **{MANAGER_NAME}:** [{MANAGER_PHONE}](tel:+98{MANAGER_PHONE[1:]})"
    )
    reply_markup = create_group_keyboard()
    await context.bot.send_message(
        chat_id=chat.id,
        text=group_welcome_message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    # Message B: The Initial Timeslot Message
    reservation_data = fetch_reservation_data(today_gregorian_str)
    schedule_text = format_schedule_message(reservation_data, today_gregorian_str)
    schedule_message = await context.bot.send_message(
        chat_id=chat.id,
        text=schedule_text,
        parse_mode='Markdown'
    )

    # Save the ID of Message B so we can edit it later
    group_schedule_messages[chat.id] = schedule_message.message_id
    logger.info(f"Set up schedule message {schedule_message.message_id} for group {chat.id}")

    await update.message.delete()


# --- REFACTORED for V3.0.0 ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles all button clicks from both private and group chats."""
    query = update.callback_query
    await query.answer() # Always acknowledge the button press first
    
    chat_id = query.message.chat_id
    chat_type = query.message.chat.type

    # --- Group Chat Logic ---
    if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP] and query.data == 'update_schedule_group':
        # Spam Prevention
        current_time = time.time()
        last_used = button_last_used.get(chat_id, 0)
        if current_time - last_used < BUTTON_COOLDOWN_SECONDS:
            await query.answer("لطفا چند لحظه صبر کنید.", show_alert=True)
            return
        button_last_used[chat_id] = current_time

        # Update Logic
        message_id_to_edit = group_schedule_messages.get(chat_id)
        if not message_id_to_edit:
            await query.answer("خطا: پیام زمان‌بندی پیدا نشد. لطفا از ادمین بخواهید دوباره از دستور /setupgroup استفاده کند.", show_alert=True)
            return

        today_str = date.today().strftime('%Y-%m-%d')
        new_data = fetch_reservation_data(today_str)
        new_text = format_schedule_message(new_data, today_str)

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id_to_edit,
                text=new_text,
                parse_mode='Markdown'
            )
            logger.info(f"Updated schedule message {message_id_to_edit} in group {chat_id}")
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                await query.answer("تایم‌ها تغییری نکرده‌اند.")
                logger.warning(f"Update skipped for group {chat_id}: message not modified.")
            else:
                logger.error(f"Failed to edit message in group {chat_id}: {e}")
                group_schedule_messages.pop(chat_id, None) # Remove broken message ID
                await query.answer("خطا: پیام اصلی حذف شده است. لطفا از ادمین بخواهید دوباره ربات را تنظیم کند.", show_alert=True)
        return

    # --- Private Chat Logic ---
    elif chat_type == ChatType.PRIVATE:
        query_date = query.data
        await query.edit_message_text(text=f"لطفا صبر کنید، در حال دریافت برنامه برای تاریخ {query_date}...")
        reservation_data = fetch_reservation_data(query_date)
        message_text = format_schedule_message(reservation_data, query_date)
        await query.edit_message_text(text=message_text, parse_mode='Markdown')
        await context.bot.send_message(
            chat_id=chat_id,
            text="می‌توانید روز دیگری را انتخاب کنید:",
            reply_markup=create_private_chat_keyboard()
        )

async def date_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles manual date entry. Works only in private chats."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    # This function remains unchanged as it's for private chat only.
    # ... (code is identical to previous version) ...
    user_input_date = update.message.text
    try:
        date.fromisoformat(user_input_date)
        await update.message.reply_text(f"دریافت شد! در حال دریافت برنامه برای تاریخ {user_input_date}...")
        reservation_data = fetch_reservation_data(user_input_date)
        message_text = format_schedule_message(reservation_data, user_input_date)
        await update.message.reply_text(text=message_text, parse_mode='Markdown')
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text="می‌توانید روز دیگری را انتخاب کنید:",
            reply_markup=create_private_chat_keyboard()
        )
    except ValueError:
        await update.message.reply_text("متاسفم، فرمت قابل فهم نیست. لطفا تاریخ را به صورت `YYYY-MM-DD` ارسال کنید.")


# --- MAIN APPLICATION SETUP ---
def main() -> None:
    """Starts the Telegram bot and registers all handlers."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN not found in environment variables.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setupgroup", setup_group))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, date_message_handler))

    logger.info("Starting bot v3.0.0...")
    application.run_polling()

if __name__ == '__main__':
    main()

