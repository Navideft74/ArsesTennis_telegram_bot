import logging
import requests
import jdatetime
import os
from dotenv import load_dotenv
from datetime import date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatType
# ---- Version : 2.1.1 : Implemented environment variables for security.

# --- Load Environment Variables ---
load_dotenv()

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
API_BASE_URL = 'http://arsestennis.ir/reservations/api/'

GROUP_MESSAGE_VISIBILITY_DURATION = 30 
GROUP_VIDEO_FILE_ID = 'YOUR_VIDEO_FILE_ID_HERE' 


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

def create_date_keyboard() -> InlineKeyboardMarkup:
    """Creates an inline keyboard with 'Today' and 'Tomorrow' buttons with Persian day names."""
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
    
    try:
        # Removed proxy logic. This is now a direct connection.
        response = requests.get(api_url, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None

def format_schedule_message(data: list, query_date: str, is_group_message: bool = False) -> str:
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
        # This message will also be sent as a temporary message in groups.
        return f"😕 متاسفانه اطلاعاتی برای تاریخ {persian_date_str} پیدا نشد."

    message = f"📅 **برنامه رزرواسیون برای {persian_date_str}**\n\n"
    for court_data in data:
        court_name = court_data.get('court', {}).get('name', 'زمین نامشخص')
        message += f"🎾 **زمین: {court_name}**\n"
        time_slots = court_data.get('time_slots', [])
        if not time_slots:
            message += "هیچ سانس خالی برای این زمین وجود ندارد.\n"
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
    
    if is_group_message:
        message += f"🗑️ *این پیام تا {GROUP_MESSAGE_VISIBILITY_DURATION} ثانیه دیگر حذف می‌شود.*\n"
        
    return message


# --- NEW JOB FOR DELETING MESSAGES ---
async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job to delete a message after a set time."""
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.chat_id, message_id=job.data['message_id'])
        logger.info(f"Deleted message {job.data['message_id']} from chat {job.chat_id}")
    except Exception as e:
        logger.error(f"Failed to delete message: {e}")


# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /start. Works only in private chats."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return 
    
    reply_markup = create_date_keyboard()
    welcome_message = "به ربات رزرو تنیس آرسس خوش آمدید! 🎾\n\nلطفا برای مشاهده برنامه، یکی از روزهای زیر را انتخاب کنید:"
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='Markdown')

async def setup_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only command to post the daily video and pinnable message in a group."""
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.message.reply_text("این دستور فقط در گروه‌ها قابل استفاده است.")
        return

    member = await chat.get_member(user_id)
    if member.status not in ['administrator', 'creator']:
        await update.message.reply_text("فقط ادمین گروه می‌تواند از این دستور استفاده کند.")
        return
        
    today_gregorian = date.today()
    today_jalali = jdatetime.date.fromgregorian(date=today_gregorian)
    today_persian_str = today_jalali.strftime("%A %d %B %Y")

    group_welcome_message = (
        f"🎾 **به گروه آکادمی تنیس آرسس خوش آمدید! (بروزرسانی {today_persian_str})**\n\n"
        "در این گروه می‌توانید از آخرین وضعیت سانس‌های خالی مطلع شوید. برای دسترسی سریع، لینک‌های زیر را دنبال کنید:\n\n"
        "🌐 **وب‌سایت آکادمی:** [arsestennis.ir](http://arsestennis.ir)\n"
        "📅 **بخش رزرواسیون آنلاین:** [arsestennis.ir/reservations](http://arsestennis.ir/reservations)\n"
        "📞 **تماس با مدیریت:** [09123456789](tel:+989123456789)\n\n"
        "👇 برای مشاهده سانس‌های آزاد **امروز** و **فردا**، روی دکمه‌های زیر کلیک کنید. پیشنهاد می‌شود این پیام را در گروه پین کنید."
    )
    
    reply_markup = create_date_keyboard()
    
    if GROUP_VIDEO_FILE_ID and GROUP_VIDEO_FILE_ID != 'YOUR_VIDEO_FILE_ID_HERE':
        await context.bot.send_video(
            chat_id=chat.id,
            video=GROUP_VIDEO_FILE_ID,
            caption=group_welcome_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            text=group_welcome_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    await update.message.delete()


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles clicks on date buttons in both private and group chats."""
    query = update.callback_query
    await query.answer()
    query_date = query.data
    chat_type = query.message.chat.type

    if chat_type == ChatType.PRIVATE:
        await query.edit_message_text(text=f"لطفا صبر کنید، در حال دریافت برنامه برای تاریخ {query_date}...")
        reservation_data = fetch_reservation_data(query_date)
        message_text = format_schedule_message(reservation_data, query_date)
        await query.edit_message_text(text=message_text, parse_mode='Markdown')
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="می‌توانید روز دیگری را انتخاب کنید:",
            reply_markup=create_date_keyboard()
        )

    elif chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        reservation_data = fetch_reservation_data(query_date)
        # This function returns the "no data found" message if needed, and the deletion warning is added.
        message_text = format_schedule_message(reservation_data, query_date, is_group_message=True)
        
        sent_message = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=message_text,
            parse_mode='Markdown'
        )
        
        # This job will run for BOTH "schedule" and "no data found" messages.
        context.job_queue.run_once(
            delete_message_job,
            GROUP_MESSAGE_VISIBILITY_DURATION,
            chat_id=sent_message.chat_id,
            data={'message_id': sent_message.message_id}
        )

async def date_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles manual date entry. Works only in private chats to avoid group spam."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return

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
            reply_markup=create_date_keyboard()
        )
    except ValueError:
        await update.message.reply_text("متاسفم، فرمت قابل فهم نیست. لطفا تاریخ را به صورت `YYYY-MM-DD` ارسال کنید.")

# --- MAIN APPLICATION SETUP ---
def main() -> None:
    """Starts the Telegram bot and registers all handlers."""
    # --- Check for Token ---
    if not TELEGRAM_BOT_TOKEN:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN not found in environment variables.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setupgroup", setup_group))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, date_message_handler))

    logger.info("Starting bot v2.2.0...")
    application.run_polling()

if __name__ == '__main__':
    main()


