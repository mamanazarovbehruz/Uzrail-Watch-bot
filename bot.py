import os
import json
import re
from dotenv import load_dotenv
import calendar
from datetime import date, timedelta,datetime, timezone
from fetcher import fetch_trains, make_summary, search_stations
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
)
from pathlib import Path
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from db import (
    init_db, upsert_user, touch_last_seen,
    set_phone, get_phone,
    save_watch, get_watch, set_watch_enabled,
    list_all_users, list_enabled_watches,
    get_user_lang, set_user_lang, add_feedback,
    get_pool
)
import asyncio
from telegram.error import BadRequest, Forbidden, TimedOut, RetryAfter, NetworkError



load_dotenv()

WATCH_SEM = asyncio.Semaphore(3)  # bir vaqtda 3 ta so'rov (ko'paytirsang ham bo'ladi)
WATCH_CHAT_ID = os.getenv("WATCH_CHAT_ID", "").strip()
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))
MAX_TG = 3900  # 4096 dan biroz past (xavfsiz)
ADMIN_IDS = {6655680807}
LANDING_BASE = "https://uzrail-watch-bot-production.up.railway.app/go"


async def send_long_text(update, text: str, *, chunk_size: int = MAX_TG, reply_markup=None):
    text = text or ""
    msg = update.effective_message

    if not text.strip():
        await msg.reply_text("❌ No results.")
        return

    parts = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

    for idx, part in enumerate(parts):
        # ✅ keyboard faqat 1-xabarga (tagiga) qo'yiladi
        if idx == 0:
            await msg.reply_text(part, reply_markup=reply_markup)
        else:
            await msg.reply_text(part)

def buy_ticket_kb(lang: str, dep_code: str, arv_code: str, date_iso: str):
    lang = (lang or "uz").lower()
    if lang not in ("uz", "ru", "en"):
        lang = "uz"

    # landing page link: bot paramlarni beradi
    url = f"{LANDING_BASE}?lang={lang}&dep={dep_code}&arv={arv_code}&date={date_iso}"

    label = {
        "uz": "🎫 Bilet sotib olish",
        "ru": "🎫 Купить билет",
        "en": "🎫 Buy ticket",
    }.get(lang, "🎫 Buy ticket")

    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)]])



def t(lang: str, key: str, **kwargs) -> str:
    lang = (lang or "uz").lower()
    if lang not in ("uz", "ru", "en"):
        lang = "uz"
    s = TEXT.get(key, {}).get(lang) or TEXT.get(key, {}).get("uz") or key
    try:
        return s.format(**kwargs)
    except Exception:
        return s


LANG_PREFIX = "LANG"  # callback: LANG|uz / LANG|ru / LANG|en

LANG_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data=f"{LANG_PREFIX}|uz")],
    [InlineKeyboardButton("🇷🇺 Русский", callback_data=f"{LANG_PREFIX}|ru")],
    [InlineKeyboardButton("🇺🇸 English", callback_data=f"{LANG_PREFIX}|en")],
])

# =========================
# i18n (UZ/RU/EN)
# =========================

BTN = {
    "route": {"uz": "📍 Yo'nalishni kiritish", "ru": "📍 Ввести маршрут", "en": "📍 Enter route"},
    "contact": {"uz": "📞 Aloqa", "ru": "📞 Контакты", "en": "📞 Contact"},
    "lang": {"uz": "🌐 Tilni tanlash", "ru": "🌐 Выбор языка", "en": "🌐 Language"},
    "feedback": {"uz": "⭐️ Fikr qoldirish", "ru": "⭐️ Оставить отзыв", "en": "⭐️ Leave feedback"},
    "back": {"uz": "🔙 Orqaga", "ru": "🔙 Назад", "en": "🔙 Back"},
    "check_now": {"uz": "Hozir tekshirish", "ru": "Сейчас проверить", "en": "Check now"},
    "stop_track": {"uz": "Kuzatishni o‘chirish", "ru": "Отключить наблюдение", "en": "Turn off tracking"},
    "send_phone": {"uz": "📱 Telefon raqamni yuborish", "ru": "📱 Отправить номер", "en": "📱 Send phone"},
}

TEXT = {
    "start_hi": {
        "uz": "Salom, {first}!\nMen O‘zbekiston temir yo‘l poyezd chiptalaridagi o‘zgarishlarni kuzataman va sizga habar beraman.\n\nYo‘nalishni tanlang 👇",
        "ru": "Здравствуйте, {first}!\nЯ отслеживаю изменения билетов на поезда Узбекистон темир йуллари и уведомляю вас.\n\nВыберите действие 👇",
        "en": "Hi, {first}!\nI track changes in Uzbekistan Railways tickets and notify you.\n\nChoose an option 👇",
    },
    "welcome": {
        "uz": "Poyezd Chiptalari Kuzatuvchi botiga xush kelibsiz!",
        "ru": "Добро пожаловать в бот мониторинга билетов!",
        "en": "Welcome to the ticket monitoring bot!",
    },
    "ask_phone": {
        "uz": "📱 Iltimos, pastdagi tugma orqali telefon raqamingizni yuboring.",
        "ru": "📱 Пожалуйста, отправьте свой номер телефона по кнопке ниже.",
        "en": "📱 Please, send your phone number using the button below.",
    },
    "phone_ok": {"uz": "Telefon raqamingiz qabul qilindi ✅", "ru": "Номер принят ✅", "en": "Phone received ✅"},
    "main_home": {"uz": "Bosh sahifa 👇", "ru": "Главное меню 👇", "en": "Home 👇"},
    "feedback_ask": {
        "uz": "⭐️ Fikr qoldiring:\nTaklifingizni bitta xabar qilib yozing.",
        "ru": "⭐️ Оставьте отзыв:\nНапишите предложение одним сообщением.",
        "en": "⭐️ Leave feedback:\nWrite it in one message.",
    },
    "feedback_ok": {"uz": "✅ Rahmat! Fikringiz qabul qilindi.", "ru": "✅ Спасибо! Отзыв принят.", "en": "✅ Thanks! Feedback saved."},
    "lang_choose": {"uz": "Iltimos, Tilni tanlang", "ru": "Пожалуйста, выберите язык", "en": "Please choose language"},
    "lang_saved": {"uz": "✅ Til saqlandi.", "ru": "✅ Язык сохранён.", "en": "✅ Language saved."},
    "searching": {"uz": "🔎 Qidiryapman...", "ru": "🔎 Ищу...", "en": "🔎 searching..."},
    "search_start": {"uz": "🔍 Qidiruv boshlandi...", "ru": "🔎 Поиск начался...", "en": "🔎 Search started..."},
    "tech_break": {
        "uz": "⏳ Texnik tanaffus: {start}–{end}\nBirozdan keyin qayta urinib ko‘ring.",
        "ru": "⏳ Технический перерыв: {start}–{end}\nПопробуйте позже.",
        "en": "⏳ Technical break: {start}–{end}\nPlease try later.",
    },
    "leaving_from": {
        "uz": "Qayerdan ketasiz?  (Bekatni yozing. Misol uchun: Toshkent)",
        "ru": "Откуда вы едете?  (Напишите станцию отправления. Например: Ташкент)",
        "en": "Where are you leaving from?  (Write the station. For example: Tashkent)",
    },
    "go_to": {
        "uz": "Qayerga borasiz? (Bekatni yozing. Misol uchun: Termiz)",
        "ru": "Куда вы идёте? (Напишите остановку. Например: Термез)",
        "en": "Where are you going? (Write the destination. For example: Termez)",
    },
    "choose_station": {
        "uz": "Topilgan bekatlardan birini tanlang:",
        "ru": "Выберите одну из найденных остановок:",
        "en": "Choose one of the available stations:",
    },
    "start_data": {
        "uz": "✅ Boshlanish sana tanlandi:",
        "ru": "✅ Выбрана дата начала:",
        "en": "✅ Start date selected:",
    },
    "end_data": {
        "uz": "✅ Tugash sana tanlandi:",
        "ru": "✅ Выбрана дата окончания:",
        "en": "✅ End date selected:",
    },
    "interval_data": {
        "uz": "✅ Sana oralig‘i tanlandi!",
        "ru": "✅ Выбран диапазон дат!",
        "en": "✅ Date interval selected!",
    },
    "interval_result": {
        "uz": "Shu oraliqda qidiruv natijalarini chiqaramiz.",
        "ru": "Мы показываем результаты поиска в этом диапазоне.",
        "en": "We'll show search results in this range.",
    },
    "available_tickets": {
        "uz": "🎟 Mavjud chiptalar:",
        "ru": "🎟 Доступные билеты:",
        "en": "🎟 Available tickets:",
    },
    "no_available": {
        "uz": "❌ Bu kunda bo‘sh joy topilmadi.",
        "ru": "❌ В этот день нет свободных мест.",
        "en": "❌ No availability on this day.",
    },
    "no_trains": {
        "uz": "❌ Bu kunda poyezd topilmadi.",
        "ru": "❌ На этот день поезда не обнаружено.",
        "en": "❌ No trains on this day.",
    },
    "traver_duration": {
        "uz": "⏱️ Yo‘l davomiyligi:",
        "ru": "⏱️ Продолжительность пути:",
        "en": "⏱️ Travel duration:",
    },
    "available_place": {
        "uz": "📋 Bo‘sh o‘rinlar : ",
        "ru": "📋 Пустые места:",
        "en": "📋 Available places:",
    },
    "monitoring": {
        "uz": "🔔 Kuzatish boshlandi.\n Agar joylar kamayib yoki ko‘payib ketsa, \n yoki yangi vagon chiqsa — darhol habar beraman.",
        "ru": "🔔 Наблюдение началось.\n Если места уменьшаются или увеличиваются, \n или если появится новый вагон - сразу же сообщу.",
        "en": "🔔 Monitoring has begun. \n If the number of seats decreases or increases, \n or if a new wagon becomes available - I'll inform you immediately.",
    },
    "choose_start_data": {
        "uz": "🗓 Boshlanish sanasini tanlang:",
        "ru": "🗓 Выберите дату начала:",
        "en": "🗓 Choose the start date:",
    },
    "choose_end_data": {
        "uz": "🗓 Tugash sanasini tanlang:",
        "ru": "🗓 Выберите дату окончания:",
        "en": "🗓 Select end date:",
    },
    "start_data_earlier": {
        "uz": "❌ Boshlanish sana bugundan oldin bo‘lishi mumkin emas.",
        "ru": "❌ Дата начала не может быть раньше сегодняшнего дня.",
        "en": "❌ The start date cannot be earlier than today.",
    },
    "new_train": {
        "uz": "🆕 Yangi poezd paydo bo‘ldi",
        "ru": "🆕 Появился новый поезд",
        "en": "🆕 A new train has appeared",
    },
    "delete_list": {
        "uz": "🗑 Poezd ro‘yxatdan yo‘qoldi",
        "ru": "🗑 Поезд исчез из списка",
        "en": "🗑 Train disappeared from list",
    },
    "currency": {"uz": "so‘m", "ru": "сум", "en": "sum",},
    "item": {"uz": "ta", "ru": " ", "en": " ",},
    "new_type": {"uz": "yangi vagon turi", "ru": "новый тип вагона", "en": "new type of car",},
    "type_lost": {
        "uz": "vagon turi yo‘qoldi",
        "ru": "тип вагона потерян",
        "en": "the type of car is lost",
    },
    "belite_sold": {
        "uz": "belit sotildi",
        "ru": "белит продан",
        "en": "belite sold",
    },
    "belite_add": {
        "uz": "belit qo‘shildi",
        "ru": "белит добавлен",
        "en": "belit added",
    },
    "place": {"uz": "joy", "ru": "место", "en": "place",},
    "high": {"uz": "Tepa", "ru": "Высокий", "en": "High",},
    "lower": {"uz": "Pastki", "ru": "Нижний", "en": "Lower",},
    "train_lost": {
        "uz": "Poezd yo‘qoldi:",
        "ru": "Поезд пропал:",
        "en": "Train lost:",
    },
    "plase_change": {
        "uz": "Joy o‘zgardi:",
        "ru": "Место изменилось:",
        "en": "Place changed:",
    },
    "cancel": {
        "uz": "❌ Bekor qilish",
        "ru": "❌ Отмена",
        "en": "❌ Cancel",
    },
    "new_found": {
        "uz": "🚆 Yangilik topildi!",
        "ru": "🚆 Найдено новости!",
        "en": "🚆 News found!",
    },
    "comrade": {
        "uz": "Birodar",
        "ru": "Братан",
        "en": "Comrade",
    },
    "first_start": {
        "uz": "✅ Birinchi ishga tushdi. Holat saqlandi, kuzatish boshlandi.",
        "ru": "✅ Первый запущен. Сохранено состояние, начато наблюдение.",
        "en": "✅ Launched first. Status saved, tracking started.",
    },
    "sent_phone_first": {
        "uz": "📱 Avval telefon raqamingizni yuboring.",
        "ru": "📱 Пожалуйста, сначала отправьте свой номер телефона.",
        "en": "📱 Please send your phone number first.",
    },
    "previous": {
        "uz": "⬅️ Oldingi",
        "ru": "⬅️ Передний",
        "en": "⬅️ Previous",
    },
    "next": {
        "uz": "➡️ Keyingi",
        "ru": "➡️ Следующий",
        "en": "➡️ Next",
    },  
    "select_list": {
        "uz": "Iltimos, ro‘yxatdan bekat tanlang.",
        "ru": "Пожалуйста, выберите остановку из списка.",
        "en": "Please select a stop from the list.",
    },
    "select_stop": {
        "uz": "Qayerga borasiz? (bekatni tanlang)",
        "ru": "Куда вы идёте? (Выберите остановку)",
        "en": "Where are you going? (select a stop)",
    },
    "select_first": {
        "uz": "Avval ketish bekatini tanlang.",
        "ru": "Сначала выберите остановку отправления.",
        "en": "Select the departure stop first.",
    },
    "destination_station": {
        "uz": "❌ Borish bekati ketish bekati bilan bir xil bo‘lmasin.",
        "ru": "❌ Станция назначения не должна совпадать со станцией отправления.",
        "en": "❌ The destination station should not be the same as the departure station.",
    },
    "nothing_found": {
        "uz": "Hech narsa topilmadi. Yana yozing (misol: Toshkent, Samarqand).",
        "ru": "Ничего не найдено. Напишите еще (пример: Ташкент, Самарканд).",
        "en": "Nothing found. Write again (example: Tashkent, Samarkand).",
    },
    "select_keyboard": {
        "uz": "Iltimos, keyboarddan tanlang.",
        "ru": "Пожалуйста, выберите с клавиатуры.",
        "en": "Please select from the keyboard.",
    },
    "wrong_choice": {
        "uz": "Noto‘g‘ri tanlov. Qayta tanlang.",
        "ru": "Неправильный выбор. Выберите ещё раз.",
        "en": "Wrong choice. Please select again.",
    },
    "write_again": {
        "uz": "Hech narsa topilmadi. Yana yozing (misol: Termiz, Nukus, Buxoro).",
        "ru": "Ничего не найдено. Напишите еще (пример: Термез, Нукус, Бухара).",
        "en": "Nothing found. Write again (example: Termez, Nukus, Bukhara).",
    },
    "select_data": {
        "uz": "Sanani tugmadan tanlang (YYYY-MM-DD). 👆",
        "ru": "Выберите дату из кнопки (YYYY-MM-DD). 👆",
        "en": "Select the date from the button (YYYY-MM-DD). 👆",
    },
    "end_start_data": {
        "uz": "❌ Tugash sanasi boshlanish sanasidan oldin bo‘lmasin.",
        "ru": "❌ Дата окончания не должна предшествовать дате начала.",
        "en": "❌ End date must not be before start date.",
    },
    "start_search": {
        "uz": "Endi qidiruvni boshlaymiz (keyingi qadam).",
        "ru": "Теперь начнем поиск (следующий шаг).",
        "en": "Now let's start searching (next step).",
    },
    "cancelled": {
        "uz": "❌ Bekor qilindi.",
        "ru": "❌ Отменено.",
        "en": "❌ Cancelled.",
    },
    "please_select": {
        "uz": "🗓 Iltimos, bugundan yoki keyingi sanadan tanlang:",
        "ru": "🗓 Пожалуйста, выберите начиная с сегодняшнего или последующего дня:",
        "en": "🗓 Please select from today or the following date:",
    },
    "select_first_data": {
        "uz": "Avval boshlanish sanani tanlang.",
        "ru": "Сначала выберите дату начала.",
        "en": "Select the start date first.",
    },
    "maximum_3": {
        "uz": "❌ Maksimal 3 kun tanlash mumkin.",
        "ru": "❌ Вы можете выбрать максимум 3 дня.",
        "en": "❌ You can choose a maximum of 3 days.",
    },
    "maximum_3_day": {
        "uz": "🗓 Tugash sanani qayta tanlang (maksimal 3 kun).",
        "ru": "🗓 Выберите дату окончания снова (максимум 3 дня).",
        "en": "🗓 Please re-select the end date (maximum 3 days).",
    },
    "please_re_select": {
        "uz": "🗓 Iltimos, tugash sanasini qayta tanlang:",
        "ru": "🗓 Пожалуйста, выберите дату окончания снова:",
        "en": "🗓 Please re-select the end date:",
    },
    "fallow_stopped": {
        "uz": "⏹️ Kuzatish to‘xtatildi. \n""Qaytadan boshlash uchun: 📍 Yo'nalishni kiriting",
        "ru": "⏹️ Отслеживание остановлено. Чтобы начать заново: 📍 Введите маршрут",
        "en": "⏹️ Following stopped. \n To restart: 📍 Enter the direction",
    },
    "try_writing": {
        "uz": "❌ Bekat topilmadi. Yana yozib ko‘ring.",
        "ru": "❌ Остановка не найдена. Попробуйте написать ещё раз.",
        "en": "❌ Stop not found. Try writing again.",
    },
    "continue_observe": {
        "uz": "🔄 Kuzatishda davom etaman.",
        "ru": "🔄 Продолжаю наблюдение.",
        "en": "🔄 I will continue to observe.",
    },
    "change_detected": {
        "uz": "🚨 O‘zgarish aniqlandi!",
        "ru": "🚨 Обнаружены изменения!",
        "en": "🚨 Change detected!",
    },
    "track_disabled": {
        "uz": "⛔ Kuzatish yoqilmagan.\n""📍 Yo'nalishni kiriting.",
        "ru": "⛔ Наблюдение выключено..\n""📍 Укажите направление.",
        "en": "⛔ Tracking is disabled.\n""📍 Enter the destination.",
    },
    "contact_all": {
        "uz": "📞 Aloqa va qo‘llab-quvvatlash \n\n""🚆 O‘zbekiston Temir Yo‘llari (Uzrailways) \n""🌐 Rasmiy sayt: https://eticket.railway.uz \n""📱 Mobil ilova: Uzrailway tickets \n""☎️ Call-center: 1005 \n\n""👨‍💻 Bot bo‘yicha savollar / takliflar: \n""Admin: @mb_coderpy",
        "ru": "📞 Контакты и поддержка \n\n""🚆 Узбекистон Темир Йуллари (Uzrailways) \n""🌐 Официальный сайт: https://eticket.railway.uz \n""📱 Мобильное приложение: Uzrailway tickets \n""📞 Колл-центр: 1005 \n\n""👨‍💻 Вопросы / предложения по боту: \n""Администратор: @mb_coderpy",
        "en": "📞 Contacts & Support \n\n""🚆 Uzbekistan Railways (Uzrailways) \n""🌐 Official website: https://eticket.railway.uz \n""📱 Mobile app: Uzrailway tickets \n""📞 Call center: 1005 \n\n""👨‍💻 Bot questions / suggestions: \n""Admin: @mb_coderpy",
    },
    "letter3": {
        "uz": "❗ Kamida 3 ta harf yozing. Masalan: Toshkent",
        "ru": "❗ Введите не менее 3 букв. Пример: Ташкент",
        "en": "❗ Write at least 3 letters. For example: Tashkent",
    },
    "pause_try_again": {
        "uz": "⏳ Hozir texnik tanaffus. Birozdan keyin urinib ko‘ring.",
        "ru": "⏳ Сейчас технический перерыв. Попробуйте через некоторое время.",
        "en": "⏳ We are currently experiencing a technical pause. Please try again shortly.",
    },
    "system_undergoing": {
        "uz": "🛠 Hozir tizimda texnik tanaffus bor.\n""Birozdan keyin qayta urinib ko‘ring (odatda 20-30 daqiqa).\n""Agar xohlasangiz, boshqa bekat nomi bilan ham urinib ko‘ring.",
        "ru": "🛠 В данный момент в системе технический перерыв.\n""Попробуйте снова через некоторое время (обычно через 20-30 минут).\n""Если хотите, попробуйте с другим названием остановки.",
        "en": "🛠 The system is currently undergoing maintenance.\n""Try again in a little while (usually after 20-30 minutes).\n""If you'd like, try using a different stop name.",
    },
    "error_station": {
        "uz": "❌ Bekat qidirishda xatolik. Birozdan keyin qayta urinib ko‘ring.",
        "ru": "🚨 Ошибка при поиске остановки. Повторите попытку через некоторое время.",
        "en": "🚨 Error in station search. Please try again in a moment.",
    },
    "no_selected_stop": {
        "uz": "❌ Bekat tanlanmagan. Qaytadan 📍 Yo'nalishni kiritish qiling.",
        "ru": "❌ Остановка не выбрана. Пожалуйста, заново 📍 укажите направление.",
        "en": "❌ No stop has been selected. Please enter the 📍 Direction again.",
    },
    "ticket_sales_stopped": {
        "uz": "Bu poyezdda biletlar sotilishi to‘xtatildi",
        "ru": "Продажа билетов на этот поезд остановлена",
        "en": "Ticket sales for this train have stopped",
    },
    "watch_expired": {
        "uz": "⏳ Kuzatuv sana muddati tugadi. Bosh sahifaga qaytdik 👇",
        "ru": "⏳ Срок наблюдения по датам истёк. Возвращаю в главное меню 👇",
        "en": "⏳ Tracking period has ended. Returning to the main menu 👇",
    },
}

# --- MAIN MENU TEXTS (3 til) ---
ROUTE_TEXTS = {BTN["route"]["uz"], BTN["route"]["ru"], BTN["route"]["en"]}
CONTACT_TEXTS = {BTN["contact"]["uz"], BTN["contact"]["ru"], BTN["contact"]["en"]}
LANG_TEXTS = {BTN["lang"]["uz"], BTN["lang"]["ru"], BTN["lang"]["en"]}
FEEDBACK_TEXTS = {BTN["feedback"]["uz"], BTN["feedback"]["ru"], BTN["feedback"]["en"]}
BACK_TEXTS = {BTN["back"]["uz"], BTN["back"]["ru"], BTN["back"]["en"]}

MENU_PATTERN = r"^(" + "|".join(map(re.escape, sorted(
    ROUTE_TEXTS | CONTACT_TEXTS | LANG_TEXTS | FEEDBACK_TEXTS | BACK_TEXTS
))) + r")$"


async def admin_db_tables(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return

    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname='public'
            ORDER BY tablename
        """)
    tables = [r["tablename"] for r in rows]
    await update.message.reply_text("📦 DB Tables:\n" + "\n".join(tables) if tables else "Jadval yo‘q.")

async def admin_db_feedback(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return

    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT id, chat_id, text, created_at
            FROM feedbacks
            ORDER BY id DESC
            LIMIT 10
        """)

    if not rows:
        await update.message.reply_text("Feedback yo‘q.")
        return

    msg = "\n\n".join(
        f"ID: {r['id']}\nChat: {r['chat_id']}\nText: {r['text']}\nTime: {r['created_at']}"
        for r in rows
    )
    await update.message.reply_text(msg)

async def admin_db_users(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return

    rows = await list_all_users(DB_PATH)
    msg = "\n".join(str(r) for r in rows) or "Userlar yo‘q"
    await update.message.reply_text(msg)


async def menu_router(update, context):
    txt = (update.effective_message.text or "").strip()

    if txt in ROUTE_TEXTS:
        await start_route(update, context)
        return

    if txt in CONTACT_TEXTS:
        await contact_handler(update, context)
        return

    if txt in LANG_TEXTS:
        await lang_handler(update, context)
        return

    if txt in FEEDBACK_TEXTS:
        lang = await get_lang(update, context)
        chat_id = update.effective_chat.id
        # qayerdan bosilganini eslab qolamiz
        context.user_data["fb_from"] = "watch" if await has_active_watch_db(chat_id) else "main"
        await feedback_handler(update, context)
        return

    if txt in BACK_TEXTS:
        await back_to_main(update, context)
        return


def _lang_norm(lang: str | None) -> str:
    lang = (lang or "uz").lower()
    return lang if lang in ("uz", "ru", "en") else "uz"

async def get_lang(update: Update, context) -> str:
    # tezkor cache: context.user_data["lang"]
    if context.user_data.get("lang"):
        return _lang_norm(context.user_data["lang"])
    chat_id = update.effective_chat.id if update and update.effective_chat else None
    if not chat_id:
        return "uz"
    lang = await get_user_lang(DB_PATH, chat_id)
    context.user_data["lang"] = _lang_norm(lang)
    return context.user_data["lang"]

def t(lang: str, key: str, **kw) -> str:
    lang = _lang_norm(lang)
    raw = (TEXT.get(key, {}) or {}).get(lang) or (TEXT.get(key, {}) or {}).get("uz") or key
    try:
        return raw.format(**kw)
    except Exception:
        return raw

def b(lang: str, key: str) -> str:
    lang = _lang_norm(lang)
    return (BTN.get(key, {}) or {}).get(lang) or (BTN.get(key, {}) or {}).get("uz") or key

def kb_phone(lang: str):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(b(lang, "send_phone"), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def kb_main(lang: str):
    return ReplyKeyboardMarkup(
        [
            [b(lang, "route"), b(lang, "contact")],
            [b(lang, "lang"), b(lang, "feedback")],
        ],
        resize_keyboard=True
    )

def kb_back(lang: str):
    return ReplyKeyboardMarkup([[b(lang, "back")]], resize_keyboard=True)

def kb_watch(lang: str):
    # sizda /watch yo'q, faqat /now /stop qoldi
    return ReplyKeyboardMarkup(
        [
            [b(lang, "check_now"), b(lang, "stop_track")],
            [b(lang, "contact"), b(lang, "feedback")],
        ],
        resize_keyboard=True
    )

def kb_watch_controls(lang: str):
    return ReplyKeyboardMarkup(
        [
            [b(lang, "check_now"), b(lang, "stop_track")],
            [b(lang, "contact"), b(lang, "feedback")],
        ],
        resize_keyboard=True
    )

async def has_active_watch_db(chat_id: int) -> bool:
    w = await get_watch(DB_PATH, chat_id)
    return bool(w and w.get("enabled"))


def fmt_date(s: str) -> str:
    """
    '2026-01-28'  ->  '28.01.2026'
    """
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return s

from collections import defaultdict

def _extract_seats_by_train(api: dict) -> dict:
    """
    Natija:
    {
      "082Ф": {
        "meta": {"from": "...", "to": "..."},
        "cars": {
          "Plaskartli": {"free": 76, "tariff": 202920, "up": 22, "down": 28},
          "Kupe": {"free": 40, "tariff": 282480, "up": 20, "down": 20},
          ...
        }
      },
      ...
    }
    """
    out = {}
    try:
        trains = api.get("data", {}).get("directions", {}).get("forward", {}).get("trains", []) or []
    except Exception:
        trains = []

    for trn in trains:
        num = (trn.get("number") or "").strip()
        if not num:
            continue

        dep_name = trn.get("originRoute", {}).get("depStationName") or trn.get("subRoute", {}).get("depStationName") or ""
        arv_name = trn.get("originRoute", {}).get("arvStationName") or trn.get("subRoute", {}).get("arvStationName") or ""

        cars_map = {}
        for c in (trn.get("cars") or []):
            ctype = (c.get("type") or "").strip() or "Unknown"
            free = int(c.get("freeSeats") or 0)

            # tariff: birinchi tariffni olamiz (bo‘lmasa None)
            tariff = None
            tariffs = c.get("tariffs") or []
            if tariffs:
                tariff = tariffs[0].get("tariff")

            # ✅ tepa/past (seatDetail)
            sd = c.get("seatDetail") or {}
            up = int(sd.get("up") or 0)
            down = int(sd.get("down") or 0)

            # Plaskart bo'lsa yonlarini ham qo‘shib yuboramiz (umumiy tepa/past uchun)
            low = (ctype or "").lower()
            if ("plask" in low) or ("плац" in low):
                up += int(sd.get("lateralUp") or 0)
                down += int(sd.get("lateralDn") or 0)

            cars_map[ctype] = {"free": free, "tariff": tariff, "up": up, "down": down}

        out[num] = {"meta": {"from": dep_name, "to": arv_name}, "cars": cars_map}

    return out

async def lang_callback(update, context):
    lang = await get_lang(update, context)
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    # LANG|uz
    parts = data.split("|", 1)
    if len(parts) != 2:
        return

    new_lang = _lang_norm(parts[1])
    chat_id = query.message.chat_id

    await set_user_lang(DB_PATH, chat_id, new_lang)
    context.user_data["lang"] = new_lang
    # 🔥 eng muhim qator: eski jarayonlarni bekor qiladi
    context.user_data["step"] = None

    await query.edit_message_text(t(new_lang, "lang_saved"))
    await query.message.reply_text(t(new_lang, "main_home"), reply_markup=kb_main(new_lang))


def _diff_trains(old_api: dict, new_api: dict, lang: str) -> list[str]:
    """
    Matnlar ro'yxati qaytaradi. Har bir element — bitta o‘zgarish satri.
    """
    old = _extract_seats_by_train(old_api)
    new = _extract_seats_by_train(new_api)

    lines = []

    all_train_nums = set(old.keys()) | set(new.keys())
    for num in sorted(all_train_nums):
        if num not in old:
            # yangi poezd paydo bo'ldi
            meta = new[num]["meta"]
            lines.append(f"🚆 {num}  {meta.get('from','')} → {meta.get('to','')}\n  {t(lang, "new_train")}")
            # carlarni ham sanab o‘tamiz
            for ctype, info in new[num]["cars"].items():
                price = f"{info['tariff']} {t(lang, "currency")}" if info.get("tariff") else "-"
                lines.append(f"  {ctype}: {info['free']} {t(lang, "item")} — {price}")
            continue

        if num not in new:
            meta = old[num]["meta"]
            lines.append(f"🚆 {num}  {meta.get('from','')} → {meta.get('to','')}\n  {t(lang, "delete_list")}")
            continue

        meta = new[num]["meta"] or old[num]["meta"]
        old_cars = old[num]["cars"]
        new_cars = new[num]["cars"]

        all_car_types = set(old_cars.keys()) | set(new_cars.keys())
        per_train_lines = []

        for ctype in sorted(all_car_types):
            if ctype not in old_cars:
                info = new_cars[ctype]
                price = f"{info['tariff']} {t(lang, "currency")}" if info.get("tariff") else "-"
                per_train_lines.append(f"  🆕 {ctype}: {info['free']} {t(lang, "item")} — {price} ({t(lang, "new_type")})")
                continue

            if ctype not in new_cars:
                per_train_lines.append(f"  🗑 {ctype}: {t(lang, "type_lost")}")
                continue

            o = old_cars[ctype]["free"]
            n = new_cars[ctype]["free"]
            if o == n:
                continue

            delta = n - o
            info = new_cars[ctype]
            price = f"{info['tariff']} {t(lang, "currency")}" if info.get("tariff") else "-"

            if delta < 0:
                per_train_lines.append(f"  {ctype}: {n} {t(lang, "item")} — {price} ({abs(delta)} {t(lang, "item")} {t(lang, "belite_sold")})")
            else:
                per_train_lines.append(f"  {ctype}: {n} {t(lang, "item")} — {price} (+{delta} {t(lang, "item")} {t(lang, "belite_add")})")

        if per_train_lines:
            lines.append(f"🚆 {num}  {meta.get('from','')} → {meta.get('to','')}")
            lines.extend(per_train_lines)

    return lines

def _watch_day_report(old_api: dict, new_api: dict, lang: str) -> str:
    """
    Watcher uchun 1 kunlik report:
    - tepa/past ko‘rsatadi (SV bo‘lsa yashiradi)
    - 0 ta joy bo‘lgan poyezd umuman chiqmaydi
    - 0 ta bo‘lgan vagon turlari ham chiqmaydi
    - o'zgarsa (+/-), o'zgarmasa ham ko‘rsatadi
    """
    old_map = _extract_seats_by_train(old_api)
    new_map = _extract_seats_by_train(new_api)

    out = []
    idx = 0

    all_nums = sorted(set(old_map.keys()) | set(new_map.keys()))
    for num in all_nums:
        meta = ((new_map.get(num) or {}).get("meta") or (old_map.get(num) or {}).get("meta") or {})
        full_from = meta.get("from", "")
        full_to = meta.get("to", "")

        old_cars = (old_map.get(num) or {}).get("cars", {})
        new_cars = (new_map.get(num) or {}).get("cars", {})

        # ✅ agar poyezd oldin bor edi, hozir umuman yo‘q — "yo'qoldi" deb chiqaramiz
        if num in old_map and num not in new_map:
            # ✅ Ko‘pincha sotuv yopilganda API bu poyezdni ro‘yxatdan olib tashlaydi.
            # Shuning uchun "0 ta joy" deb emas, "sotuv to‘xtatildi" deb chiqaramiz.
            idx += 1
            out.append(f"🚆 #{idx}:  {num}  {meta.get('from','')} → {meta.get('to','')}")
            out.append(t(lang, "ticket_sales_stopped"))
            out.append("")  # bo‘sh qator
            continue



        # ✅ faqat 0dan katta joylarni hisoblaymiz
        total_now = sum(int(v.get("free") or 0) for v in new_cars.values() if int(v.get("free") or 0) > 0)

        # ✅ 0 bo‘lsa — poyezdni umuman chiqarma
        if total_now <= 0:
            continue

        idx += 1
        out.append(f"🚆 #{idx}:  {num}  {full_from} → {full_to}")
        out.append(f"{t(lang, "available_place")} {total_now} {t(lang, "item")} {t(lang, "place")}")

        all_types = sorted(set(old_cars.keys()) | set(new_cars.keys()))
        for ctype in all_types:
            now = new_cars.get(ctype) or {}
            was = old_cars.get(ctype) or {}
            now_free = int((now or {}).get("free") or 0)
            was_free = int((was or {}).get("free") or 0)
            delta = now_free - was_free

            # ✅ ikkalasi ham 0 bo'lsa kerak emas
            if now_free <= 0 and was_free <= 0:
                continue

            # ✅ agar hozir 0 bo'lib qolgan bo'lsa (sotilgan) — ham chiqaramiz
            sold_out = (now_free <= 0 and was_free > 0)

            # tepa/past
            now_up = int(now.get("up") or 0)
            now_down = int(now.get("down") or 0)

            # SV bo‘lsa tepa/pastni yashiramiz
            low = (ctype or "").lower()
            is_sv = ("sv" == low) or (" sv" in low) or ("св" in low)
            # ✅ Umumiyda tepa/past bo‘lmaydi — yashiramiz
            is_umumiy = ("umumiy" in low) or ("общ" in low) or ("general" in low)

            sign = ""
            if delta != 0:
                sign = f"(➕{delta})" if delta > 0 else f"(➖{abs(delta)})"

            if is_sv or is_umumiy:
                if sold_out:
                    out.append(f"• {ctype} {sign} : 0 {t(lang, 'item')} {t(lang, 'place')}")
                    continue
                # SV: faqat son
                if delta == 0:
                    out.append(f"• {ctype} : {now_free} {t(lang, "item")} {t(lang, "place")}")
                else:
                    out.append(f"• {ctype} {sign} : {now_free} {t(lang, "item")} {t(lang, "place")}")
            else:
                if sold_out:
                    out.append(f"• {ctype} {sign} : 0 {t(lang, 'item')} {t(lang, 'place')}")
                    continue
                # boshqalar: tepa/past FAQAT mavjud bo'lsa ko'rsatamiz
                show_ud = (now_up + now_down) > 0

                ud_text = ""
                if show_ud:
                    ud_text = f" ({t(lang, 'high')} {now_up} {t(lang, 'item')}, {t(lang, 'lower')} {now_down} {t(lang, 'item')})"
                # boshqalar: tepa/past bilan
                if delta == 0:
                    out.append(f"• {ctype} : {now_free} {t(lang, 'item')} {t(lang, 'place')}{ud_text}")
                else:
                    out.append(f"• {ctype} {sign} : {now_free} {t(lang, 'item')} {t(lang, 'place')}{ud_text}")
        out.append("")

    return "\n".join(out).strip()


def fmt_date_obj(d: date) -> str:
    """
    date(2026,1,28) -> '28.01.2026'
    """
    try:
        return d.strftime("%d.%m.%Y")
    except Exception:
        return str(d)

def diff(prev: dict, cur: dict, lang: str) -> list[str]:
    lines = []

    for train_key in cur.keys():
        if train_key not in prev:
            lines.append(f"➕ {t(lang, "new_train")} {train_key}")

    for train_key in prev.keys():
        if train_key not in cur:
            lines.append(f"➖ {t(lang, "train_lost")} {train_key}")

    for train_key, cur_cars in cur.items():
        prev_cars = prev.get(train_key, {})

        for car_type in cur_cars.keys():
            if car_type not in prev_cars:
                lines.append(f"➕ {t(lang, "new_train1")} {train_key} — {car_type} (free={cur_cars[car_type]['freeSeats']})")

        for car_type, cur_info in cur_cars.items():
            if car_type in prev_cars:
                A = int(prev_cars[car_type].get("freeSeats") or 0)
                B = int(cur_info.get("freeSeats") or 0)
                if B != A:
                    arrow = "📈" if B > A else "📉"
                    tariff = cur_info.get("tariff")
                    tariff_txt = f", tariff={tariff}" if tariff is not None else ""
                    lines.append(f"{arrow} {t(lang, 'plase_change')} {train_key} — {car_type} {A} → {B}{tariff_txt}")

    return lines


def load_state() -> dict | None:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def build_date_keyboard(lang: str, days=14):
    today = date.today()
    items = []
    for i in range(days):
        d = today + timedelta(days=i)
        items.append(d.isoformat())  # YYYY-MM-DD

    # 2 tadan qilib chiqamiz
    keyboard = [items[i:i+2] for i in range(0, len(items), 2)]
    keyboard.append(t(lang, "back"))
    return keyboard

CAL_PREFIX = "CAL"  # callback_data prefiks

def _cal_cb(action: str, mode: str, y: int, m: int, d: int = 0) -> str:
    # action: NAV | DAY | IGN
    return f"{CAL_PREFIX}|{action}|{mode}|{y:04d}-{m:02d}|{d:02d}"

def build_calendar(year: int, month: int, mode: str, lang: str) -> InlineKeyboardMarkup:
    """
    mode = 'from' yoki 'to'
    """
    cal = calendar.Calendar(firstweekday=0)  # Monday
    month_name = f"{calendar.month_name[month]} {year}"

    rows = []
    # Header: oy nomi + navigatsiya
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)

    rows.append([
        InlineKeyboardButton("◀️", callback_data=_cal_cb("NAV", mode, prev_y, prev_m)),
        InlineKeyboardButton(month_name, callback_data=_cal_cb("IGN", mode, year, month)),
        InlineKeyboardButton("▶️", callback_data=_cal_cb("NAV", mode, next_y, next_m)),
    ])

    # Weekdays
    rows.append([
        InlineKeyboardButton("Du", callback_data=_cal_cb("IGN", mode, year, month)),
        InlineKeyboardButton("Se", callback_data=_cal_cb("IGN", mode, year, month)),
        InlineKeyboardButton("Ch", callback_data=_cal_cb("IGN", mode, year, month)),
        InlineKeyboardButton("Pa", callback_data=_cal_cb("IGN", mode, year, month)),
        InlineKeyboardButton("Ju", callback_data=_cal_cb("IGN", mode, year, month)),
        InlineKeyboardButton("Sh", callback_data=_cal_cb("IGN", mode, year, month)),
        InlineKeyboardButton("Ya", callback_data=_cal_cb("IGN", mode, year, month)),
    ])

    # Days
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data=_cal_cb("IGN", mode, year, month)))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=_cal_cb("DAY", mode, year, month, day)))
        rows.append(row)

    # Footer
    rows.append([InlineKeyboardButton(t(lang, "cancel"), callback_data=_cal_cb("CANCEL", mode, year, month))])


    return InlineKeyboardMarkup(rows)

from telegram import ReplyKeyboardRemove
from telegram.error import BadRequest

async def send_calendar(update, context, mode: str):
    lang = await get_lang(update, context)
    """
    mode: 'from' (boshlanish) yoki 'to' (tugash)
    """
    today = date.today()
    context.user_data["cal_mode"] = mode
    context.user_data["cal_ym"] = f"{today.year:04d}-{today.month:02d}"

    caption = t(lang, "choose_start_data") if mode == "from" else t(lang, "choose_end_data")
    markup = build_calendar(today.year, today.month, mode, lang)

    # ✅ 1) Reply keyboardni yashiramiz (xabarni keyin o‘chirib tashlaymiz)
    try:
        if update.message:
            tmp = await update.effective_message.reply_text("…", reply_markup=ReplyKeyboardRemove())
        else:
            tmp = await update.callback_query.message.reply_text("…", reply_markup=ReplyKeyboardRemove())
        try:
            await tmp.delete()
        except Exception:
            pass
    except Exception:
        pass

    # ✅ 2) Kalendarni chiqaramiz (caption + inline kalendar bitta xabarda)
    if update.message:
        await update.effective_message.reply_text(caption, reply_markup=markup)
    else:
        await update.callback_query.message.reply_text(caption, reply_markup=markup)


async def check_and_notify(app: Application, chat_id: int, lang: str):
    api = await fetch_trains(DEP, ARV, DATE)
    cur = make_summary(api)

    prev = load_state()
    if prev is None:
        save_state(cur)
        await app.bot.send_message(chat_id=chat_id, text=t(lang, "first_start"))
        return

    changes = diff(prev, cur)
    if changes:
        save_state(cur)
        msg = f"{t(lang, "new_found")}\n\n" + "\n".join(changes)
        await app.bot.send_message(chat_id=chat_id, text=msg[:3500])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    u = update.effective_user

    # ✅ /start bosilganda eski kuzatuvni to'xtatamiz (/stop kabi)
    await save_watch(DB_PATH, chat_id, {"enabled": False})

    # ✅ eski flow holatlarini tozalaymiz
    context.user_data["step"] = None
    for k in ("dep_code", "arv_code", "dep_name", "arv_name", "date_from", "date_to", "station_items", "station_page"):
        context.user_data.pop(k, None)

    # DB user upsert + last_seen
    await upsert_user(DB_PATH, chat_id, u.id if u else None, u.username if u else None,
                      u.first_name if u else None, u.last_name if u else None)

    # ✅ 1) Avval til tekshiramiz
    db_lang = await get_user_lang(DB_PATH, chat_id)  # sendagi funksiya
    if not db_lang:
        context.user_data["step"] = "choose_lang"
        await update.effective_message.reply_text(
            "🌐 Iltimos, tilni tanlang / Please choose language / Пожалуйста, выберите язык",
            reply_markup=LANG_KB
        )
        return

    lang = db_lang
    context.user_data["lang"] = lang


    # ✅ ro'yxatdan o'tmagan bo'lsa /startdayoq telefon so'raymiz
    if await need_phone(context, chat_id):
        context.user_data["step"] = "need_phone"
        await update.effective_message.reply_text(
            t(lang, "ask_phone"),
            reply_markup=kb_phone(lang)
        )
        return

    first = (u.first_name or t(lang, "comrade")).strip() if u else t(lang, "comrade")
    await update.effective_message.reply_text(
        t(lang, "start_hi", first=first),
        reply_markup=kb_main(lang)
    )


async def need_phone(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    if context.user_data.get("phone"):
        return False
    db_phone = await get_phone(DB_PATH, chat_id)
    if db_phone:
        context.user_data["phone"] = db_phone
        context.user_data["registered"] = True
        return False
    return True


def reset_flow_keep_phone(context):
    phone = context.user_data.get("phone")
    registered = context.user_data.get("registered")

    context.user_data.clear()

    if phone:
        context.user_data["phone"] = phone
    if registered:
        context.user_data["registered"] = registered

async def clear_watch_everything(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    # 1) DB'dagi watchni butunlay tozalash (endi /now eski oraliqni topolmaydi)
    await save_watch(DB_PATH, chat_id, {
        "enabled": False,
        "dep_code": None,
        "arv_code": None,
        "dep_name": None,
        "arv_name": None,
        "date_from": None,
        "date_to": None,
        "snapshot_json": None,
        "snapshot_hash": None,
    })

    # 2) RAM'dagi bot_data ichidan ham o'chirib tashlaymiz (watcher_jobga ham ta'sir qiladi)
    watch_chats = context.application.bot_data.get("watch_chats", {})
    if isinstance(watch_chats, dict):
        watch_chats.pop(chat_id, None)
        context.application.bot_data["watch_chats"] = watch_chats

    # 3) user_data ichidagi flow/watch kalitlarini ham tozalaymiz (phone qoladi)
    for k in [
        "dep_code", "arv_code", "dep", "arv",
        "dep_name", "arv_name",
        "date_from", "date_to",
        "snapshot",
        "watch_enabled", "watch_dep", "watch_arv", "watch_from", "watch_to",
        "watch_chat_id",
        "step",
    ]:
        context.user_data.pop(k, None)


async def phone_contact_handler(update, context):
    lang = await get_lang(update, context)
    contact = update.effective_message.contact
    if not contact or not contact.phone_number:
        await update.effective_message.reply_text(
            t(lang, "ask_phone"),
            reply_markup=kb_phone(lang)
        )
        return

    phone = contact.phone_number

    # ✅ 1) context (tezkor cache)
    context.user_data["phone"] = phone
    context.user_data["registered"] = True

    # ✅ 2) SQLite DB (restartdan keyin ham saqlansin)
    await set_phone(DB_PATH, update.effective_chat.id, phone)

    await update.effective_message.reply_text(
        t(lang, "phone_ok"),
        reply_markup=ReplyKeyboardRemove()
    )

    first = (update.effective_user.first_name or t(lang, "comrade")).strip()
    await update.effective_message.reply_text(
        t(lang, "start_hi", first=first),
        reply_markup=kb_main(lang)
    )


async def start_route(update, context):
    lang = await get_lang(update, context)
    if await need_phone(context, update.effective_chat.id):
        await update.effective_message.reply_text(
            t(lang, "sent_phone_first"),
            reply_markup=kb_phone(lang)
        )
        return
    reset_flow_keep_phone(context)
    context.user_data["step"] = "dep_query"

    await update.effective_message.reply_text(
        t(lang, "leaving_from"),
        reply_markup=kb_back(lang)
    )


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await get_lang(update, context)
    chat_id = update.effective_chat.id
    reset_flow_keep_phone(context)

    if await has_active_watch_db(chat_id):
        await update.effective_message.reply_text(t(lang, "main_home"), reply_markup=kb_watch_controls(lang))
    else:
        await update.effective_message.reply_text(t(lang, "main_home"), reply_markup=kb_main(lang))

def _stations_keyboard(lang: str, items: list, page: int = 0, page_size: int = 8):
    """
    items: [{"name":"Toshkent","code":"2900000"}, ...] yoki sizning format
    """
    from telegram import ReplyKeyboardMarkup

    if not items:
        return ReplyKeyboardMarkup([[b(lang, "back")]], resize_keyboard=True)

    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))

    start = page * page_size
    end = start + page_size
    chunk = items[start:end]

    rows = []
    for s in chunk:
        name = s.get("name") or ""
        code = s.get("code") or ""
        # sizda tanlash formati NAME (CODE) bo‘lsa:
        rows.append([f"{name}"])

    nav = []
    if page > 0:
        nav.append(b(lang, "previous"))
    if page < pages - 1:
        nav.append(b(lang, "next"))
    if nav:
        rows.append(nav)

    rows.append([b(lang, "back")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def choose_dep(update, context):
    lang = await get_lang(update, context)
    dep = update.message.text.strip()

    if dep not in STATIONS:
        await update.effective_message.reply_text(t(lang, "select_list"))
        return

    context.user_data["dep"] = dep
    context.user_data["step"] = "choose_arv"

    # borish ro'yxati: depni olib tashlaymiz
    arv_list = [s for s in STATIONS if s != dep]
    keyboard = [arv_list[i:i+2] for i in range(0, len(arv_list), 2)]
    keyboard.append([t(lang, "back")])

    await update.effective_message.reply_text(
        t(lang, "select_stop"),
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def choose_arv(update, context):
    lang = await get_lang(update, context)
    arv = update.message.text.strip()

    if arv not in STATIONS:
        await update.effective_message.reply_text(t(lang, "select_list"))
        return

    dep = context.user_data.get("dep")
    if not dep:
        await update.effective_message.reply_text(t(lang, "select_first"))
        return

    if arv == dep:
        await update.effective_message.reply_text(t(lang, "destination_station"))
        return

    context.user_data["arv"] = arv
    context.user_data["step"] = "choose_date_from"

    await send_calendar(update, context, "from")


async def dep_query(update, context):
    lang = await get_lang(update, context)
    text = (update.message.text or "").strip()

    if text == t(lang, "back"):
        await back_to_main(update, context)
        return

    # qidiruv
    stations = await search_stations(text)
    if not stations:
        await update.effective_message.reply_text(
            t(lang, "nothing_found"),
            reply_markup=kb_back(lang)
        )
        return

    context.user_data["dep_candidates"] = stations
    context.user_data["dep_page"] = 0
    context.user_data["step"] = "dep_select"

    await update.effective_message.reply_text(
        t(lang, "choose_station"),
        reply_markup=_stations_keyboard(lang, stations, page=0)
    )


async def dep_select(update, context):
    lang = await get_lang(update, context)
    text = (update.message.text or "").strip()

    if text == t(lang, "back"):
        await back_to_main(update, context)
        return

    stations = context.user_data.get("dep_candidates") or []
    page = int(context.user_data.get("dep_page") or 0)

    if text == t(lang, "next"):
        page += 1
        context.user_data["dep_page"] = page
        await update.effective_message.reply_text(
            t(lang, "choose_station"),
            reply_markup=_stations_keyboard(lang, stations, page=page)
        )
        return

    if text == t(lang, "previous"):
        page = max(0, page - 1)
        context.user_data["dep_page"] = page
        await update.effective_message.reply_text(
            t(lang, "choose_station"),
            reply_markup=_stations_keyboard(lang, stations, page=page)
        )
        return

    # format: NAME (CODE)
    if "(" not in text or ")" not in text:
        await update.effective_message.reply_text(
            t(lang, "select_keyboard"),
            reply_markup=_stations_keyboard(lang, stations, page=page)
        )
        return

    code = text.split("(")[-1].split(")")[0].strip()
    name = text.split("(")[0].strip()

    if not code.isdigit():
        await update.effective_message.reply_text(
            t(lang, "wrong_choice"),
            reply_markup=_stations_keyboard(lang, stations, page=page)
        )
        return

    context.user_data["dep"] = name
    context.user_data["dep_code"] = code

    # endi borish bekati
    context.user_data["step"] = "arv_query"
    await update.effective_message.reply_text(
        t(lang, "go_to"),
        reply_markup=kb_back(lang)
    )


async def arv_query(update, context):
    lang = await get_lang(update, context)
    text = (update.message.text or "").strip()

    if text == t(lang, "back"):
        await back_to_main(update, context)
        return

    stations = await search_stations(text)
    if not stations:
        await update.effective_message.reply_text(
            t(lang, "write_again"),
            reply_markup=kb_back(lang)
        )
        return

    # dep bilan bir xil kodni chiqarib tashlaymiz
    dep_code = context.user_data.get("dep_code")
    stations = [s for s in stations if s.get("code") != dep_code]

    context.user_data["arv_candidates"] = stations
    context.user_data["arv_page"] = 0
    context.user_data["step"] = "arv_select"

    await update.effective_message.reply_text(
        t(lang, "choose_station"),
        reply_markup=_stations_keyboard(lang, stations, page=0)
    )


async def arv_select(update, context):
    lang = await get_lang(update, context)
    text = (update.message.text or "").strip()

    if text == t(lang, "back"):
        await back_to_main(update, context)
        return

    stations = context.user_data.get("arv_candidates") or []
    page = int(context.user_data.get("arv_page") or 0)

    if text == t(lang, "next"):
        page += 1
        context.user_data["arv_page"] = page
        await update.effective_message.reply_text(
            t(lang, "choose_station"),
            reply_markup=_stations_keyboard(lang, stations, page=page)
        )
        return

    if text == t(lang, "previous"):
        page = max(0, page - 1)
        context.user_data["arv_page"] = page
        await update.effective_message.reply_text(
            t(lang, "choose_station"),
            reply_markup=_stations_keyboard(lang, stations, page=page)
        )
        return

    if "(" not in text or ")" not in text:
        await update.effective_message.reply_text(
            t(lang, "select_keyboard"),
            reply_markup=_stations_keyboard(lang, stations, page=page)
        )
        return

    code = text.split("(")[-1].split(")")[0].strip()
    name = text.split("(")[0].strip()

    if not code.isdigit():
        await update.effective_message.reply_text(
            t(lang, "wrong_choice"),
            reply_markup=_stations_keyboard(lang, stations, page=page)
        )
        return

    context.user_data["arv"] = name
    context.user_data["arv_code"] = code

    # keyingi bosqich: kalendar
    context.user_data["step"] = "choose_date_to"  # send_calendar ichida mode="from" bo'lgani uchun bu emas
    context.user_data["step"] = "choose_date_from"
    await send_calendar(update, context, "from")



async def choose_date_from(update, context):
    lang = await get_lang(update, context)
    text = update.message.text.strip()

    try:
        date.fromisoformat(text)
    except ValueError:
        await update.effective_message.reply_text(t(lang, "select_data"))
        return

    context.user_data["date_from"] = text
    context.user_data["step"] = "choose_date_to"

    await update.effective_message.reply_text(
        "Tugash sanasini tanlang (YYYY-MM-DD):",
        reply_markup=ReplyKeyboardMarkup(build_date_keyboard(), resize_keyboard=True)
    )

async def choose_date_to(update, context):
    lang = await get_lang(update, context)
    text = update.message.text.strip()

    try:
        to_d = date.fromisoformat(text)
    except ValueError:
        await update.effective_message.reply_text(t(lang, "select_data"))
        return

    from_text = context.user_data.get("date_from")
    from_d = date.fromisoformat(from_text)

    if to_d < from_d:
        await update.effective_message.reply_text(t(lang, "end_start_data"))
        return

    context.user_data["date_to"] = text
    context.user_data["step"] = "done_range"

    dep = context.user_data.get("dep_name") or context.user_data.get("dep")
    arv = context.user_data.get("arv_name") or context.user_data.get("arv")

    await update.effective_message.reply_text(
        f"✅ Tanlandi:\n"
        f"📍 {dep} → {arv}\n"
        f"🗓 {from_text} .. {text}\n\n"
        f"{t(lang, "start_search")}"
    )


async def _safe_edit_text(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        s = str(e)
        if ("Message is not modified" in s) or ("Message to edit not found" in s):
            return
        raise

async def _safe_edit_markup(query, reply_markup):
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    except BadRequest as e:
        s = str(e)
        if ("Message is not modified" in s) or ("Message to edit not found" in s):
            return
        raise


async def calendar_handler(update, context):
    lang = await get_lang(update, context)
    query = update.callback_query
    data = query.data or ""
    if not data.startswith(CAL_PREFIX + "|"):
        return

    await query.answer()

    parts = data.split("|")
    # CAL|ACTION|mode|YYYY-MM|DD
    if len(parts) != 5:
        return

    _, action, mode, ym, dd = parts
    y, m = map(int, ym.split("-"))
    d = int(dd)

    # IGN: hech nima qilmaymiz
    if action == "IGN":
        return

    # NAV: oy almashtirish
    if action == "NAV":
        await _safe_edit_markup(query, build_calendar(y, m, mode, lang))
        return

    if action == "CANCEL":
        context.user_data.pop("date_from", None)
        context.user_data.pop("date_to", None)
        context.user_data["step"] = None

        await _safe_edit_text(query,t(lang, "cancelled"))

        await query.message.reply_text(
            t(lang, "main_home"),
            reply_markup=kb_main(lang)
        )
        return


    # DAY: kun tanlandi
    if action == "DAY":
        selected = date(y, m, d).isoformat()  # YYYY-MM-DD

        if mode == "from":
            selected_date = date(y, m, d)
            today = date.today()

            # ❌ Bugundan oldingi sana bo‘lsa qabul qilinmaydi
            if selected_date < today:
                # eski kalendarni yopamiz
                try:
                    await _safe_edit_text(query, t(lang, "start_data_earlier"))
                except Exception:
                    pass

                # yangi xabar + kalendar
                await query.message.reply_text(
                    t(lang, "please_select"),
                    reply_markup=build_calendar(today.year, today.month, "from", lang)
                )
                return

                        # Agar to‘g‘ri bo‘lsa davom etamiz
            selected = selected_date.isoformat()
            context.user_data["date_from"] = selected
            context.user_data["step"] = "choose_date_to"

            # ✅ 1) Bosilgan "Boshlanish sanasini tanlang" kalendar xabarini butunlay o‘chirib yuboramiz
            try:
                await query.message.delete()
            except BadRequest as e:
                # callback kechiksa/oldin o'chib ketsa - jim o'tkazamiz
                if "Message to delete not found" in str(e):
                    pass
                else:
                    pass  # xohlasangiz print qilib log qiling
            
            # ✅ 2) TASDIQ XABARI (faqat shu qoladi)
            await query.message.chat.send_message(
                f"{t(lang, "start_data")} {fmt_date(selected)}"
            )

            # ✅ 3) Tugash kalendarini chiqaramiz (1 marta)
            await query.message.chat.send_message(
                t(lang, "choose_end_data"),
                reply_markup=build_calendar(y, m, "to", lang)
            )
            return




        if mode == "to":
            from_text = context.user_data.get("date_from")
            if not from_text:
                await _safe_edit_text(query,t(lang, "select_first_data"))
                return

            from_d = date.fromisoformat(from_text)
            to_d = date.fromisoformat(selected)

            MAX_DAYS = 3  # maksimal 3 kunlik oraliq

            if (to_d - from_d).days > (MAX_DAYS - 1):
                # Eski tugash kalendar xabarini yopamiz (xato yozuvi bilan)
                try:
                    await _safe_edit_text(query,t(lang, "maximum_3"))
                except Exception:
                    pass

                # Yangi xabar + yangi kalendar (qayta tanlash uchun)
                await query.message.reply_text(
                    t(lang, "maximum_3_day"),
                    reply_markup=build_calendar(y, m, "to", lang)
                )
                return

            if to_d < from_d:
                # 1) Bosilgan (eski) kalendar xabarini yopamiz
                await _safe_edit_text(query, t(lang, "end_start_data"))

                # 2) Yangi xato xabari + tagidan yangi kalendar
                await query.message.reply_text(
                    t(lang, "please_re_select"),
                    reply_markup=build_calendar(y, m, "to", lang)
                )
                return

            context.user_data["date_to"] = selected
            context.user_data["step"] = "done_range"

            dep_name = context.user_data.get("dep_name")
            arv_name = context.user_data.get("arv_name")
            dep_code = context.user_data.get("dep_code")
            arv_code = context.user_data.get("arv_code")

             # ✅ 1) Bosilgan tugash kalendar xabarini ham o‘chirib yuboramiz
            try:
                await query.message.delete()
            except BadRequest as e:
                # callback kechiksa/oldin o'chib ketsa - jim o'tkazamiz
                if "Message to delete not found" in str(e):
                    pass
                else:
                    pass  # xohlasangiz print qilib log qiling

            # ✅ 1.1) Tugash sana tanlandi xabari (SIZ XOHlagan)
            await query.message.chat.send_message(
                f"{t(lang, "end_data")} {fmt_date(selected)}"
            )

            from_text = context.user_data.get("date_from")

            # route matni
            if dep_name and arv_name:
                route_text = f"📍 {dep_name} → {arv_name}\n"
            elif dep_code and arv_code:
                route_text = f"📍 {dep_code} → {arv_code}\n"
            else:
                route_text = ""

            # ✅ 2) Faqat bitta yakuniy xabar (oraliq “✅ Sana oralig‘i tanlandi.” yo‘q!)
            await query.message.chat.send_message(
                f"{t(lang, "interval_data")}\n"
                f"{route_text}"
                f"🗓 {fmt_date(from_text)} ⟷ {fmt_date(selected)}\n\n"
                f"{t(lang, "interval_result")}"
            )

            await search_in_range_and_show(update, context)
            return

        
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await get_lang(update, context)
    chat_id = update.effective_chat.id

    # 1) RAM'dagi (user_data) hammasini tozalaymiz
    for k in [
        "dep_code", "arv_code", "dep", "arv",
        "dep_name", "arv_name",
        "date_from", "date_to",
        "snapshot",
        "watch_enabled", "watch_dep", "watch_arv", "watch_from", "watch_to",
        "watch_chat_id",
        "step",
    ]:
        context.user_data.pop(k, None)

    # 2) DB'dagi watch holatini BUTUNLAY tozalaymiz (endilikda /now ham ishlamaydi)
    await save_watch(DB_PATH, chat_id, {
        "enabled": False,
        "dep_code": None,
        "arv_code": None,
        "dep_name": None,
        "arv_name": None,
        "date_from": None,
        "date_to": None,
        "snapshot_json": None,
        "snapshot_hash": None,
    })

    await update.effective_message.reply_text(
        t(lang, "fallow_stopped"),
        reply_markup=kb_main(lang)
    )



async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = context.user_data.get("lang") or await get_user_lang(DB_PATH, chat_id) or "uz"
    w = await get_watch(DB_PATH, chat_id)

    if (not w) or (not w.get("enabled")):
        await update.effective_message.reply_text(
            t(lang, "track_disabled")
        )
        return

    if not w.get("dep_code") or not w.get("arv_code") or not w.get("date_from") or not w.get("date_to"):
        await update.effective_message.reply_text(
            "Sizda yo‘nalish va sana tanlanmagan.\n📍 Yo'nalishni kiriting."
        )
        return

    dep_code = w["dep_code"]
    arv_code = w["arv_code"]
    d_from = w["date_from"]
    d_to = w["date_to"]
    dep_name = w.get("dep_name") or str(dep_code)
    arv_name = w.get("arv_name") or str(arv_code)

    await update.effective_message.reply_text(t(lang, "searching"))

    full_text = f"{t(lang, "available_tickets")}\n"
    full_text += f"📍 {dep_name} → {arv_name}\n"
    snapshot = {}

    for d in iter_dates(d_from, d_to):
        lang = context.user_data.get("lang") or await get_user_lang(DB_PATH, update.effective_chat.id)
        api = await fetch_trains(dep_code, arv_code, d, lang=lang)
        full_text += format_trains(d, api, dep_name, arv_name, lang)
        snapshot[d] = api
    
    # 1️⃣ asosiy matn + 🎫 bilet tugmasi
    await update.effective_message.reply_text(
        full_text[:3900],
        reply_markup=buy_ticket_kb(lang, dep_code, arv_code, d)
    )

    # # 2️⃣ alohida xabar bilan /now /stop
    # await update.effective_message.reply_text(
    #     reply_markup=kb_watch(lang)
    # )
    await update.effective_message.reply_text(t(lang, "continue_observe"))

    # snapshot yangilab qo'yamiz (keyingi kuzatuv uchun)
    await save_watch(DB_PATH, chat_id, {
        "enabled": bool(w.get("enabled")),
        "dep_code": dep_code,
        "arv_code": arv_code,
        "dep_name": dep_name,
        "arv_name": arv_name,
        "date_from": d_from,
        "date_to": d_to,
        "snapshot_json": json.dumps(snapshot, ensure_ascii=False, default=str),
        "snapshot_hash": _safe_hash(snapshot),
    })


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    chat_id = app.bot_data.get("watch_chat_id") or (int(WATCH_CHAT_ID) if WATCH_CHAT_ID else None)
    if chat_id:
        await check_and_notify(app, chat_id)

async def route_button_router(update, context, lang: str):
    text = (update.message.text or "").strip()

    if text == b(lang, "route"):
        await start_route(update, context)
        return

    if text == t(lang, "back"):
        return

    # agar hozircha boshqa matn bo'lsa, e'tibor bermaymiz

async def activity_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    u = update.effective_user
    await upsert_user(
        DB_PATH,
        chat_id=chat_id,
        user_id=(u.id if u else None),
        username=(u.username if u else None),
        first_name=(u.first_name if u else None),
        last_name=(u.last_name if u else None),
    )

async def contact_handler(update, context):
    chat_id = update.effective_chat.id
    lang = context.user_data.get("lang") or await get_user_lang(DB_PATH, chat_id)
    await update.effective_message.reply_text(
        t(lang, "contact_all")
    )

LANG_PREFIX = "LANG"

def lang_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇺🇿 O‘zbekcha", callback_data=f"{LANG_PREFIX}|uz")],
            [InlineKeyboardButton("🇷🇺 Русский", callback_data=f"{LANG_PREFIX}|ru")],
            [InlineKeyboardButton("🇺🇸 English", callback_data=f"{LANG_PREFIX}|en")],
        ]
    )
async def lang_handler(update, context):
    lang = await get_lang(update, context)
    await update.effective_message.reply_text(t(lang, "lang_choose"), reply_markup=lang_kb())

async def feedback_handler(update, context):
    lang = await get_lang(update, context)
    context.user_data["step"] = "feedback"

    # qayerdan kelgan bo'lsa o'sha keyboardni saqlab turamiz
    if context.user_data.get("fb_from") == "watch":
        markup = kb_watch_controls(lang)
    else:
        markup = kb_main(lang)

    await update.effective_message.reply_text(
        t(lang, "feedback_ask"),
        reply_markup=kb_back(lang)   # ✅ faqat Orqaga
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN .env ichida yo'q yoki bo'sh")
    
    async def post_init(app: Application):
        await init_db(DB_PATH)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # 🔹 TIL TANLASH CALLBACK HANDLER
    app.add_handler(CallbackQueryHandler(lang_callback, pattern=f"^{LANG_PREFIX}\\|"), group=1)

    import traceback

    async def error_handler(update, context):
        try:
            print(f"[ERROR] {type(context.error).__name__}: {context.error}")
            traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
        except Exception:
            pass
    
    app.add_error_handler(error_handler)

    # 0-group: middleware (hamma update)
    app.add_handler(MessageHandler(~filters.COMMAND, activity_middleware), group=0)

    # 1-group: asosiy handlerlar (start ishlashi uchun)
    app.add_handler(CommandHandler("start", start), group=1)
    app.add_handler(MessageHandler(filters.CONTACT, phone_contact_handler), group=1)
    
    # ✅ 3 tildagi menu tugmalarini ushlaydigan router
    app.add_handler(MessageHandler(filters.Regex(MENU_PATTERN), menu_router), group=1)

    # app.add_handler(CommandHandler("watch", watch), group=1)
    app.add_handler(CommandHandler("stop", stop), group=1)
    app.add_handler(CommandHandler("now", now), group=1)

    app.add_handler(CallbackQueryHandler(inline_button_handler, pattern=r"^empty$"), group=1)
    app.add_handler(CallbackQueryHandler(calendar_handler, pattern=r"^CAL\|"), group=1)

    # ⚠️ TEXT handlerlar bir-birini bosib ketmasin:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, flow_handler), group=2)
    # agar route_button_router kerak bo'lsa, uni ham group=2 qilib flow_handler bilan moslab qo'yish kerak
    # app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_button_router), group=2)

    app.add_handler(CommandHandler("db_tables", admin_db_tables), group=1)
    app.add_handler(CommandHandler("db_feedback", admin_db_feedback), group=1)
    app.add_handler(CommandHandler("db_users", admin_db_users), group=1)


    # JobQueue
    if app.job_queue is None:
        raise RuntimeError('JobQueue yo‘q. requirements.txt: python-telegram-bot[webhooks,job-queue]==21.10')
    app.job_queue.run_repeating(
        watcher_job,
        interval=POLL_SECONDS,
        first=10,
        name="watcher_job",
        job_kwargs={
            "max_instances": 1,         # bir vaqtda faqat 1 dona
            "coalesce": True,           # agar kechiksa — yig‘ib yuboradi (navbat qilmaydi)
            "misfire_grace_time": 60,   # 60s ichida o‘tib ketgan triggerlarni “kechikib” qabul qiladi
        },
    )

    
    print("Bot ishga tushdi...")
    USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
    PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")   # masalan: https://xxx.up.railway.app
    PORT = int(os.getenv("PORT", "8080"))
    WEBHOOK_PATH = (os.getenv("WEBHOOK_PATH") or BOT_TOKEN).lstrip("/")

    if USE_WEBHOOK:
        if not PUBLIC_URL:
            raise RuntimeError("PUBLIC_URL env yo‘q. Railway public domenini PUBLIC_URL ga qo‘ying.")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=f"{PUBLIC_URL}/{WEBHOOK_PATH}",
            drop_pending_updates=True,
        )
    else:
        app.run_polling()

async def inline_button_handler(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "empty":
        await query.edit_message_text("Hozircha bu tugma bo‘sh 🙂")

_STATION_PICK_RE = re.compile(r"^\s*(.*?)\s*\((\d+)\)\s*$")

def _parse_station_pick(text):
    return text.strip(), None

async def flow_handler(update, context):
    lang = await get_lang(update, context)
    # ⭐️ feedback rejimi
    if context.user_data.get("step") == "feedback":
        text = (update.effective_message.text or "").strip()

        chat_id = update.effective_chat.id

        def _fb_markup():
            # watch controlsdan kelgan bo'lsa - o'sha joyda qoladi
            return kb_watch_controls(lang) if context.user_data.get("fb_from") == "watch" else kb_main(lang)
    
         # "Orqaga" bosilsa feedback yozmaymiz
        if text == b(lang, "back"):
            src = context.user_data.get("fb_from", "main")
            context.user_data["step"] = None
            context.user_data.pop("fb_from", None)

            if src == "watch":
                await update.effective_message.reply_text(t(lang, "main_home"), reply_markup=kb_watch_controls(lang))
            else:
                await update.effective_message.reply_text(t(lang, "main_home"), reply_markup=kb_main(lang))
            return

        # Menu tugmalarini feedback deb qabul qilmaymiz
        if text in (
            b(lang, "feedback"),
            b(lang, "lang"),
            b(lang, "contact"),
            b(lang, "route"),
            "📍 Yo'nalishni kiritish",
            "🌐 Tilni tanlash",
            "📞 Aloqa",
            "⭐ Fikr qoldirish",
        ):
            return
        
        # bo‘sh xabar bo‘lsa ham qabul qilmaymiz
        if not text:
            if context.user_data.get("fb_from") == "watch":
                await update.effective_message.reply_text(t(lang, "feedback_ask"), reply_markup=kb_watch_controls(lang))
            else:
                await update.effective_message.reply_text(t(lang, "feedback_ask"), reply_markup=kb_main(lang))
            return
        
        src = context.user_data.get("fb_from", "main")
        context.user_data["step"] = None
        context.user_data.pop("fb_from", None)

        await add_feedback(DB_PATH, chat_id, text)

        if src == "watch":
            await update.effective_message.reply_text(t(lang, "feedback_ok"), reply_markup=kb_watch_controls(lang))
        else:
            await update.effective_message.reply_text(t(lang, "feedback_ok"), reply_markup=kb_main(lang))
        return
    step = context.user_data.get("step")

    chat_id = update.effective_chat.id
    lang = context.user_data.get("lang") or await get_user_lang(DB_PATH, chat_id)
    context.user_data["lang"] = lang

    text = (update.effective_message.text or "").strip()

    # ✅ Kuzatish boshqaruvi tugmalari (slashsiz)
    if text == b(lang, "check_now"):
        await now(update, context)
        return

    if text == b(lang, "stop_track"):
        await stop(update, context)
        return


    # # 📞 Aloqa
    # if text == b(lang, "contact"):
    #     await update.effective_message.reply_text(t(lang, "contact_text"), reply_markup=kb_main(lang))
    #     return

    # # 🌐 Tilni tanlash
    # if text == b(lang, "lang"):
    #     await update.effective_message.reply_text(t(lang, "choose_lang"), reply_markup=LANG_KB)
    #     return

    # # ⭐️ Fikr qoldirish
    # if text == b(lang, "feedback"):
    #     context.user_data["step"] = "feedback"
    #     await update.effective_message.reply_text(t(lang, "feedback_ask"), reply_markup=ReplyKeyboardRemove())
    #     return

    
    # Registratsiya tugamaguncha boshqa narsaga o'tkazmaymiz
    if step == "need_phone":
        await update.effective_message.reply_text(
            t(lang, "ask_phone"),
            reply_markup=kb_phone(lang)
        )
        return

    MENU_TEXTS = {
        b(lang, "route"),
        b(lang, "lang"),
        b(lang, "contact"),
        b(lang, "feedback"),
    }

    # ✅ Menu bosilsa — flow_handler umuman aralashmasin
    if text in MENU_TEXTS:
        return
    
    step = context.user_data.get("step")

    # ✅ step yo‘q bo‘lsa, bu oddiy menu/bo‘sh holat — bekat qidirishga kirmaymiz
    if step is None:
        return

    # Orqaga
    if text == t(lang, "back"):
        await back_to_main(update, context)
        return

    # pagination
    if text in (t(lang, "previous"), t(lang, "next")):
        items = context.user_data.get("station_items") or []
        page = int(context.user_data.get("station_page") or 0)
        if text == t(lang, "previous"):
            page = max(0, page - 1)
        else:
            page = page + 1

        context.user_data["station_page"] = page
        await update.effective_message.reply_text(
            t(lang, "choose_station"),
            reply_markup=_stations_keyboard(lang, items, page=page)
        )
        return

    # 1) Ketish bekati: foydalanuvchi yozadi -> qidiramiz -> keyboard chiqaramiz
    if step == "dep_query":
        q = text
        if len(q) < 3:
            await update.effective_message.reply_text(t(lang, "letter3"))
            return
        await update.effective_message.reply_text(t(lang, "searching"))
        try:
            lang = context.user_data.get("lang") or await get_user_lang(DB_PATH, update.effective_chat.id)
            try:
                items = await search_stations(q, lang=lang)
            except RuntimeError as e:
                if "stations_tech_break" in str(e):
                    await update.effective_message.reply_text(t(lang, "pause_try_again"))
                    return
                raise
        except RuntimeError as e:
            msg = str(e)
            # 424 texnik tanaffus bo'lsa userga tushunarli yozamiz
            if "API status=424" in msg or "stations API status=424" in msg:
                await update.effective_message.reply_text(t(lang, "system_undergoing"))
                return
            await update.effective_message.reply_text(t(lang, "error_station"))
            return
        except Exception:
            await update.effective_message.reply_text(t(lang, "error_station"))
            return

        if not items:
            await update.effective_message.reply_text(t(lang, "try_writing"))
            return

        context.user_data["station_items"] = items
        context.user_data["station_page"] = 0
        context.user_data["step"] = "dep_pick"

        await update.effective_message.reply_text(
            t(lang, "choose_station"),
            reply_markup=_stations_keyboard(lang, items, page=0)
        )
        return

    # 2) Ketish bekati: foydalanuvchi keyboard’dan tanlaydi
    if step == "dep_pick":
        items = context.user_data.get("station_items") or []
        name, code = _parse_station_pick(text)
        # ✅ pagination tugmalari bo‘lsa (agar siz ishlatayotgan bo‘lsangiz)
        if name == b(lang, "previous") or name == b(lang, "next"):
            page = int(context.user_data.get("station_page") or 0)
            if name == b(lang, "previous"):
                page = max(0, page - 1)
            else:
                page = page + 1

            context.user_data["station_page"] = page
            await update.effective_message.reply_text(
                t(lang, "choose_station"),
                reply_markup=_stations_keyboard(lang, items, page=page)
            )
            return
         # ✅ tanlovni kod bo‘yicha (eng ishonchli), bo‘lmasa nom bo‘yicha topamiz
        selected = None

        if code:
            selected = next((s for s in items if str(s.get("code")) == str(code)), None)
        if not selected:
            selected = next((s for s in items if (s.get("name") or "").strip() == name), None)

        if not selected:
            await update.effective_message.reply_text(t(lang, "select_list"))
            return

        context.user_data["dep_name"] = selected["name"]
        context.user_data["dep_code"] = selected["code"]

        # keyingi bosqichingiz qanday bo‘lsa o‘sha qoladi
        context.user_data["step"] = "arv_query"
        # context.user_data.pop("station_items", None)
        # context.user_data.pop("station_page", None)
        await update.effective_message.reply_text(
            t(lang, "go_to"),
            reply_markup=kb_back(lang)
        )
        return
        return

    # 3) Borish bekati: foydalanuvchi yozadi -> qidiramiz -> keyboard chiqaramiz
    if step == "arv_query":
        q = text
        if len(q) < 3:
            await update.effective_message.reply_text(t(lang, "letter3"))
            return
        await update.effective_message.reply_text(t(lang, "searching"))
        try:
            lang = context.user_data.get("lang") or await get_user_lang(DB_PATH, update.effective_chat.id)
            try:
                items = await search_stations(q, lang=lang)
            except RuntimeError as e:
                if "stations_tech_break" in str(e):
                    await update.effective_message.reply_text(t(lang, "pause_try_again"))
                    return
                raise
        except RuntimeError as e:
            msg = str(e)
            # 424 texnik tanaffus bo'lsa userga tushunarli yozamiz
            if "API status=424" in msg or "stations API status=424" in msg:
                await update.effective_message.reply_text(t(lang, "system_undergoing"))
                return
            await update.effective_message.reply_text(t(lang, "error_station"))
            return
        except Exception:
            await update.effective_message.reply_text(t(lang, "error_station"))
            return

        if not items:
            await update.effective_message.reply_text(t(lang, "try_writing"))
            return

        context.user_data["station_items"] = items
        context.user_data["station_page"] = 0
        context.user_data["step"] = "arv_pick"

        await update.effective_message.reply_text(
            t(lang, "choose_station"),
            reply_markup=_stations_keyboard(lang, items, page=0)
        )
        return

    if step == "arv_pick":
        items = context.user_data.get("station_items") or []
        name, code = _parse_station_pick(text)

        if name == b(lang, "previous") or name == b(lang, "next"):
            page = int(context.user_data.get("station_page") or 0)
            if name == b(lang, "previous"):
                page = max(0, page - 1)
            else:
                page = page + 1

            context.user_data["station_page"] = page
            await update.effective_message.reply_text(
                t(lang, "choose_station"),
                reply_markup=_stations_keyboard(lang, items, page=page)
            )
            return

        selected = None
        if code:
            selected = next((s for s in items if str(s.get("code")) == str(code)), None)
        if not selected:
            selected = next((s for s in items if (s.get("name") or "").strip() == name), None)

        if not selected:
            await update.effective_message.reply_text(t(lang, "pick_from_list"))
            return

        context.user_data["arv_name"] = selected["name"]
        context.user_data["arv_code"] = selected["code"]

        # keyingi bosqichingiz (kalendar chiqarish) qanday bo‘lsa o‘sha qoladi
        context.user_data["step"] = "choose_date_from"
        await send_calendar(update, context, "from")
        return


    # Qolgan eski bosqichlar (kalendar va h.k.)
    if step == "choose_dep":
        await choose_dep(update, context); return
    if step == "choose_arv":
        await choose_arv(update, context); return
    if step == "choose_date_from":
        await choose_date_from(update, context); return
    if step == "choose_date_to":
        await choose_date_to(update, context); return


def iter_dates(start_iso, end_iso):
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    cur = start
    while cur <= end:
        yield cur.isoformat()
        cur += timedelta(days=1)

def seats_breakdown(car_type: str, seat_detail: dict, free: int):
    if not seat_detail:
        return None

    ct = (car_type or "").lower()

    up = int(seat_detail.get("up") or 0)
    down = int(seat_detail.get("down") or 0)
    lat_up = int(seat_detail.get("lateralUp") or 0)
    lat_dn = int(seat_detail.get("lateralDn") or 0)

    # Plaskart: yon o‘rinlar ham bor
    if "plask" in ct or "плац" in ct:
        tepa = up + lat_up
        pastki = down + lat_dn
        yon_tepa = lat_up
        yon_pastki = lat_dn
        return tepa, pastki, yon_tepa, yon_pastki

    # Kupe/SV/Umumiy: odatda yon o‘rin yo‘q
    if up or down:
        return up, down, 0, 0

    return None

def format_trains(d, api, dep_name, arv_name, lang: str):
    text = f"\n📅 {fmt_date_obj(d) if hasattr(d,'day') else fmt_date(d)}\n"

    trains = (
        api.get("data", {})
           .get("directions", {})
           .get("forward", {})
           .get("trains", [])
    )

    if not trains:
        return text + f"{t(lang, "no_trains")}\n"

    j = 0

    for i, trn in enumerate(trains, start=1):
        num = trn.get("number")

        total_places = sum(
            int(car.get("freeSeats") or 0)
            for car in trn.get("cars", [])
        )
        # ✅ Joyi yo‘q poyezdni umuman chiqarma
        if total_places <= 0:
            j += 1
            continue

        # 1) Poyezdning asl yo‘nalishi (butun marshrut)
        full_from = trn.get("originRoute", {}).get("depStationName") or ""
        full_to = trn.get("originRoute", {}).get("arvStationName") or ""

        # 2) Siz tanlagan segment (subRoute)
        seg_from = trn.get("subRoute", {}).get("depStationName") or dep_name
        seg_to = trn.get("subRoute", {}).get("arvStationName") or arv_name

        dep_time = trn.get("departureDate")  # masalan: "21.01.2026 21:13"
        arv_time = trn.get("arrivalDate")
        way = trn.get("timeOnWay")

        total_places = sum(
            int(car.get("freeSeats") or 0)
            for car in trn.get("cars", [])
        )

        text += (
            f"🚆 #{i}:  {num}  {full_from} → {full_to}\n"
            f"🕒 {seg_from.upper()} / ({dep_time})\n"
            f"🕒 {seg_to.upper()} / ({arv_time})\n"
            f"{t(lang, "traver_duration")} {way}\n"
            f"{t(lang, "available_place")} {total_places} {t(lang, "item")} {t(lang, "place")}\n"
        )

        for c in trn.get("cars", []):
            ctype = c.get("type")
            free = c.get("freeSeats")

            tariffs = c.get("tariffs") or []
            price = tariffs[0]["tariff"] if tariffs else None
            price_txt = f"{price:,}".replace(",", " ") if price else "-"

            detail = c.get("seatDetail") or {}
            br = seats_breakdown(ctype, detail, free)

            # Agar vagon bo‘yicha joy yo‘q bo‘lsa — umuman SKIP qilamiz
            if free <= 0:
                continue

            ct_low = (ctype or "").lower()

            if br:
                tepa, pastki, yon_tepa, yon_pastki = br

                # SV bo‘lsa tepa/pastki ko‘rsatmaymiz
                if ct_low in ("sv", "св") or "sv" in ct_low or "св" in ct_low:
                    text += f"• {ctype} : {free} {t(lang, "item")} {t(lang, "place")} → {price_txt} {t(lang, "currency")}\n"
                else:
                    text += (
                        f"• {ctype} : {free} {t(lang, "item")} {t(lang, "place")} "
                        f"({t(lang, "high")} {tepa} {t(lang, "item")}, {t(lang, "lower")} {pastki} {t(lang, "item")}) "
                        f"→ {price_txt} {t(lang, "currency")}\n"
                    )
            else:
                text += f"• {ctype} : {free} {t(lang, "item")} {t(lang, "place")} → {price_txt} {t(lang, "currency")}\n"

        text += "\n"

    if j == i:
        text += f"{t(lang, "no_available")}\n"

    return text


async def search_in_range_and_show(update, context):
    if update.message:
        msg = update.message
    else:
        msg = update.callback_query.message

    dep_code = context.user_data.get("dep_code") or context.user_data.get("dep")
    arv_code = context.user_data.get("arv_code") or context.user_data.get("arv")

    if not dep_code or not arv_code:
        await update.effective_message.reply_text(t(lang, "no_selected_stop"))
        return
    
    d_from = context.user_data["date_from"]
    d_to = context.user_data["date_to"]

    lang = context.user_data.get("lang") or await get_user_lang(DB_PATH, update.effective_chat.id)

    await update.effective_message.reply_text(t(lang, "search_start"))
    dep_name = context.user_data.get("dep_name") or context.user_data.get("dep") or "—"
    arv_name = context.user_data.get("arv_name") or context.user_data.get("arv") or "—"
    full_text = f"{t(lang, "available_tickets")}\n"
    full_text += f"📍 {dep_name} → {arv_name}\n"
    snapshot = {}

    for d in iter_dates(d_from, d_to):
        lang = context.user_data.get("lang") or await get_user_lang(DB_PATH, update.effective_chat.id)
        api = await fetch_trains(dep_code, arv_code, d, lang=lang)
        dep_name = context.user_data.get("dep_name") or context.user_data.get("dep") or str(dep_code)
        arv_name = context.user_data.get("arv_name") or context.user_data.get("arv") or str(arv_code)
        full_text += format_trains(d, api, dep_name, arv_name, lang)
        snapshot[d] = api

    # ✅ faqat 1 marta yuboramiz
    await send_long_text(update, full_text, reply_markup=buy_ticket_kb(lang, dep_code, arv_code, d))

    # ✅ DB'ga watch konfiguratsiya + snapshot saqlaymiz
    chat_id = update.effective_chat.id

    snapshot_hash = _safe_hash(snapshot)

    await save_watch(DB_PATH, chat_id, {
        "enabled": True,
        "dep_code": str(dep_code),
        "arv_code": str(arv_code),
        "dep_name": context.user_data.get("dep_name"),
        "arv_name": context.user_data.get("arv_name"),
        "date_from": str(d_from),
        "date_to": str(d_to),
        "snapshot_json": json.dumps(snapshot, ensure_ascii=False, default=str),
        "snapshot_hash": snapshot_hash,
    })

    await update.effective_message.reply_text(
        t(lang, "monitoring"),
        reply_markup=kb_watch(lang)
    )


def _safe_hash(obj) -> str:
    """
    Snapshotlarni solishtirish uchun stabil hash.
    """
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(obj)

def diff_snapshot(old_api: dict, new_api: dict) -> bool:
    """
    O'zgargan/o'zgarmaganini aniqlash.
    Hozircha sodda: butun json tengmi yo'qmi.
    Keyin seat/freeSeats bo'yicha nozikroq qilamiz.
    """
    return _safe_hash(old_api) != _safe_hash(new_api)

async def safe_send(bot, chat_id: int, text: str, retries: int = 3, **kwargs) -> bool:
    """
    Telegram'ga xabar yuborishni xavfsiz bajaruvchi yordamchi.
    Tarmoqdagi vaqtinchalik xatolar (TimedOut, RetryAfter, NetworkError)
    bo'lsa, bir necha marta qayta urinadi.
    """
    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return True
        except Forbidden:
            # foydalanuvchi botni bloklagan
            return False
        except RetryAfter as e:
            # Telegram "keyinroq urin" deganda
            delay = int(getattr(e, "retry_after", 5)) + 1
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError):
            # vaqtinchalik tarmoq muammosi, biroz kutib qayta urinib ko'ramiz
            if attempt == retries:
                return False
            await asyncio.sleep(2 * attempt)
        except Exception:
            # boshqa xatolarni ko'tarib yuboramiz, stack trace loglarda ko'rinishi uchun
            raise


async def watcher_job(context: ContextTypes.DEFAULT_TYPE):
    # timezone-aware bo'lsin, keyin utc datetime bilan ayirishda xato bermaydi
    start_ts = datetime.now(timezone.utc)
    MAX_RUN_SECONDS = int(os.getenv("WATCHER_MAX_RUN_SECONDS", "80"))
    watches = await list_enabled_watches(DB_PATH)
    if not watches:
        return
    
    bot = context.application.bot
    for w in watches:

         # ✅ Time budget: juda cho‘zilib ketmasin
        if (datetime.now(timezone.utc) - start_ts).total_seconds() > MAX_RUN_SECONDS:
            print("[watcher_job] time budget reached, will continue next tick")
            break
        chat_id = int(w["chat_id"])

        lang = (
            context.application.chat_data.get(chat_id, {}).get("lang")
            or w.get("lang")
            or await get_user_lang(DB_PATH, chat_id)
            or "uz"
        )

        dep_code = w.get("dep_code")
        arv_code = w.get("arv_code")
        d_from = w.get("date_from")
        d_to = w.get("date_to")

        if not dep_code or not arv_code or not d_from or not d_to:
            continue  # hali user qidiruv qilmagan

        dep_name = w.get("dep_name") or str(dep_code)
        arv_name = w.get("arv_name") or str(arv_code)

        old_hash = w.get("snapshot_hash") or ""
        old_snapshot = {}
        if w.get("snapshot_json"):
            try:
                old_snapshot = json.loads(w["snapshot_json"])
            except Exception:
                old_snapshot = {}

        new_snapshot = {}
        changed_days = []

        # ✅ Sana muddati tugagan bo‘lsa (oxirgi kundan keyingi kunda) — kuzatuvni o‘chirib, bosh menuga qaytamiz.
        try:
            # Server UTC bo‘lishi mumkin, UZ (+5) ga yaqinlashtiramiz
            now_uz = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5)))
            end_day = date.fromisoformat(d_to)
            if now_uz.date() > end_day:
                await set_watch_enabled(DB_PATH, chat_id, False)
                ok = await safe_send(
                    bot,
                    chat_id,
                    t(lang, "watch_expired"),
                    reply_markup=kb_main(lang),
                )
                if not ok:
                    # user botni bloklagan bo‘lsa ham — endi watch o‘chdi, shunchaki davom etamiz
                    continue
                continue
        except Exception:
            pass


        for d in iter_dates(d_from, d_to):
             # ✅ Time budget: bir user sanalari ham cho‘zsa — shu yerdan chiqib ketamiz
            if (datetime.now(timezone.utc) - start_ts).total_seconds() > MAX_RUN_SECONDS:
                print(f"[watcher_job] time budget reached mid-user chat_id={chat_id}, will continue next tick")
                break
            try:
                async with WATCH_SEM:
                    api = await fetch_trains(dep_code, arv_code, d, lang=lang)
            except Exception as e:
                print(f"[watcher_job] fetch_trains error chat_id={chat_id}: {e}")
                continue

            new_snapshot[d] = api

            old_api = old_snapshot.get(d)
            if old_api is not None and diff_snapshot(old_api, api):
                changed_days.append(d)

        new_hash = _safe_hash(new_snapshot)
        if old_hash and new_hash == old_hash:
            continue  # umuman o'zgarish yo'q

        if changed_days:
            text = f"{t(lang, "change_detected")}\n"
            text += f"📍 {dep_name} → {arv_name}\n"

            has_any = False
            for d in changed_days[:3]:  # spam bo'lmasin (xohlasang olib tashlaymiz)
                old_api = old_snapshot.get(d, {})
                new_api = new_snapshot.get(d, {})

                day_report = _watch_day_report(old_api, new_api, lang)
                if not day_report or not day_report.strip():
                    continue  # ✅ bo‘sh bo‘lsa bu sanani umuman chiqarmaymiz
                
                has_any = True
                text += f"\n\n📅 {fmt_date(d)}\n"
                text += day_report

            if not has_any:
                # snapshot baribir yangilansin (quyida save bo‘ladi)
                pass
            else:
                text += f"\n\n {t(lang, "continue_observe")}"

                for i in range(0, len(text), 3900):
                    ok = await safe_send(
                        bot,
                        chat_id,
                        text[i:i + 3900],
                        reply_markup=buy_ticket_kb(lang, dep_code, arv_code, d) if i == 0 else None,
                    )
                    if not ok:
                        # user botni bloklagan -> watchni o‘chirib qo‘yamiz, job qayta urunib yurmasin
                        await set_watch_enabled(DB_PATH, chat_id, False)
                        break

        # snapshotni DBga yangilab qo'yamiz
        await save_watch(DB_PATH, chat_id, {
            "enabled": True,
            "dep_code": dep_code,
            "arv_code": arv_code,
            "dep_name": dep_name,
            "arv_name": arv_name,
            "date_from": d_from,
            "date_to": d_to,
            "snapshot_json": json.dumps(new_snapshot, ensure_ascii=False, default=str),
            "snapshot_hash": new_hash,
        })


if __name__ == "__main__":
    main()