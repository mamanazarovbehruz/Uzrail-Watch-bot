import os
import json
from dotenv import load_dotenv
import calendar
from datetime import date, timedelta,datetime
from fetcher import fetch_trains, make_summary, search_stations
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from pathlib import Path
from telegram.ext import PicklePersistence, Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

load_dotenv()

WATCH_CHAT_ID = os.getenv("WATCH_CHAT_ID", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))

# Hozircha bitta yo'nalish/sana (keyin /add bilan ko'paytiramiz)
DEP = "2900000"
ARV = "2900864"
DATE = "2026-01-30"

STATE_FILE = f"state_{DEP}_{ARV}_{DATE}.json"

STATIONS = ["Toshkent", "Samarqand", "Buxoro", "Andijon", "Termiz", "Qo‘qon"]

PHONE_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📱 Telefon raqamni yuborish", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

MAIN_KB = ReplyKeyboardMarkup(
    [["📍 Yo'nalishni kiritish"]],
    resize_keyboard=True
)

WATCH_KB = ReplyKeyboardMarkup(
    [
        ["/now — hozir tekshirish"],
        ["/stop — kuzatishni o‘chirish"],
    ],
    resize_keyboard=True
)


def kb_route_only():
    return ReplyKeyboardMarkup(
        [["📍 Yo'nalishni kiritish"]],
        resize_keyboard=True
    )

def kb_watch_controls():
    return ReplyKeyboardMarkup(
        [
            ["/stop — Kuzatishni to'xtatish"],
            ["/now — Mavjud chiptalarni ko'rish"],
        ],
        resize_keyboard=True
    )

def has_active_watch(context, chat_id: int) -> bool:
    chats = context.application.bot_data.get("watch_chats", {})
    w = chats.get(chat_id) or {}
    return bool(w.get("enabled"))


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

    for t in trains:
        num = (t.get("number") or "").strip()
        if not num:
            continue

        dep_name = t.get("originRoute", {}).get("depStationName") or t.get("subRoute", {}).get("depStationName") or ""
        arv_name = t.get("originRoute", {}).get("arvStationName") or t.get("subRoute", {}).get("arvStationName") or ""

        cars_map = {}
        for c in (t.get("cars") or []):
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


def _diff_trains(old_api: dict, new_api: dict) -> list[str]:
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
            lines.append(f"🚆 {num}  {meta.get('from','')} → {meta.get('to','')}\n  🆕 Yangi poezd paydo bo‘ldi")
            # carlarni ham sanab o‘tamiz
            for ctype, info in new[num]["cars"].items():
                price = f"{info['tariff']} so‘m" if info.get("tariff") else "-"
                lines.append(f"  {ctype}: {info['free']} ta — {price}")
            continue

        if num not in new:
            meta = old[num]["meta"]
            lines.append(f"🚆 {num}  {meta.get('from','')} → {meta.get('to','')}\n  🗑 Poezd ro‘yxatdan yo‘qoldi")
            continue

        meta = new[num]["meta"] or old[num]["meta"]
        old_cars = old[num]["cars"]
        new_cars = new[num]["cars"]

        all_car_types = set(old_cars.keys()) | set(new_cars.keys())
        per_train_lines = []

        for ctype in sorted(all_car_types):
            if ctype not in old_cars:
                info = new_cars[ctype]
                price = f"{info['tariff']} so‘m" if info.get("tariff") else "-"
                per_train_lines.append(f"  🆕 {ctype}: {info['free']} ta — {price} (yangi vagon turi)")
                continue

            if ctype not in new_cars:
                per_train_lines.append(f"  🗑 {ctype}: vagon turi yo‘qoldi")
                continue

            o = old_cars[ctype]["free"]
            n = new_cars[ctype]["free"]
            if o == n:
                continue

            delta = n - o
            info = new_cars[ctype]
            price = f"{info['tariff']} so‘m" if info.get("tariff") else "-"

            if delta < 0:
                per_train_lines.append(f"  {ctype}: {n} ta — {price} ({abs(delta)} ta belit sotildi)")
            else:
                per_train_lines.append(f"  {ctype}: {n} ta — {price} (+{delta} ta qo‘shildi)")

        if per_train_lines:
            lines.append(f"🚆 {num}  {meta.get('from','')} → {meta.get('to','')}")
            lines.extend(per_train_lines)

    return lines

def _watch_day_report(old_api: dict, new_api: dict) -> str:
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

    for num in sorted(new_map.keys()):
        meta = (new_map[num].get("meta") or {})
        full_from = meta.get("from", "")
        full_to = meta.get("to", "")

        old_cars = (old_map.get(num) or {}).get("cars", {})
        new_cars = (new_map.get(num) or {}).get("cars", {})

        # ✅ faqat 0dan katta joylarni hisoblaymiz
        total_now = sum(int(v.get("free") or 0) for v in new_cars.values() if int(v.get("free") or 0) > 0)

        # ✅ 0 bo‘lsa — poyezdni umuman chiqarma
        if total_now <= 0:
            continue

        idx += 1
        out.append(f"🚆 #{idx}:  {num}  {full_from} → {full_to}")
        out.append(f"📋 Bo‘sh o‘rinlar : {total_now} ta joy")

        for ctype in sorted(new_cars.keys()):
            now = new_cars.get(ctype) or {}
            now_free = int(now.get("free") or 0)
            if now_free <= 0:
                continue  # ✅ 0 bo‘lgan vagon turini ham chiqarma

            was = old_cars.get(ctype) or {}
            was_free = int(was.get("free") or 0)
            delta = now_free - was_free

            # tepa/past
            now_up = int(now.get("up") or 0)
            now_down = int(now.get("down") or 0)

            # SV bo‘lsa tepa/pastni yashiramiz
            low = (ctype or "").lower()
            is_sv = ("sv" == low) or (" sv" in low) or ("св" in low) or ("sleeper" in low)
            # ✅ Umumiyda tepa/past bo‘lmaydi — yashiramiz
            is_umumiy = ("umumiy" in low) or ("общ" in low) or ("general" in low)

            sign = ""
            if delta != 0:
                sign = f"(➕{delta})" if delta > 0 else f"(➖{(-1)*delta})"

            if is_sv or is_umumiy:
                # SV: faqat son
                if delta == 0:
                    out.append(f"• {ctype} : {now_free} ta joy")
                else:
                    out.append(f"• {ctype} {sign} : {now_free} ta joy")
            else:
                # boshqalar: tepa/past bilan
                if delta == 0:
                    out.append(f"• {ctype} : {now_free} ta joy (Tepa {now_up} ta, Pastki {now_down} ta)")
                else:
                    out.append(f"• {ctype} {sign} : {now_free} ta joy (Tepa {now_up} ta, Pastki {now_down} ta)")

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

def diff(prev: dict, cur: dict) -> list[str]:
    lines = []

    for train_key in cur.keys():
        if train_key not in prev:
            lines.append(f"➕ Yangi poezd: {train_key}")

    for train_key in prev.keys():
        if train_key not in cur:
            lines.append(f"➖ Poezd yo‘qoldi: {train_key}")

    for train_key, cur_cars in cur.items():
        prev_cars = prev.get(train_key, {})

        for car_type in cur_cars.keys():
            if car_type not in prev_cars:
                lines.append(f"➕ Yangi vagon: {train_key} — {car_type} (free={cur_cars[car_type]['freeSeats']})")

        for car_type, cur_info in cur_cars.items():
            if car_type in prev_cars:
                a = int(prev_cars[car_type].get("freeSeats") or 0)
                b = int(cur_info.get("freeSeats") or 0)
                if b != a:
                    arrow = "📈" if b > a else "📉"
                    t = cur_info.get("tariff")
                    tariff_txt = f", tariff={t}" if t is not None else ""
                    lines.append(f"{arrow} Joy o‘zgardi: {train_key} — {car_type} {a} → {b}{tariff_txt}")

    return lines


def load_state() -> dict | None:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def build_date_keyboard(days=14):
    today = date.today()
    items = []
    for i in range(days):
        d = today + timedelta(days=i)
        items.append(d.isoformat())  # YYYY-MM-DD

    # 2 tadan qilib chiqamiz
    keyboard = [items[i:i+2] for i in range(0, len(items), 2)]
    keyboard.append(["🔙 Orqaga"])
    return keyboard

CAL_PREFIX = "CAL"  # callback_data prefiks

def _cal_cb(action: str, mode: str, y: int, m: int, d: int = 0) -> str:
    # action: NAV | DAY | IGN
    return f"{CAL_PREFIX}|{action}|{mode}|{y:04d}-{m:02d}|{d:02d}"

def build_calendar(year: int, month: int, mode: str) -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton("❌ Bekor qilish", callback_data=_cal_cb("CANCEL", mode, year, month))])


    return InlineKeyboardMarkup(rows)

async def send_calendar(update, context, mode: str):
    """
    mode: 'from' (boshlanish) yoki 'to' (tugash)
    """
    today = date.today()
    context.user_data["cal_mode"] = mode
    context.user_data["cal_ym"] = f"{today.year:04d}-{today.month:02d}"

    caption = "🗓 Boshlanish sanasini tanlang:" if mode == "from" else "🗓 Tugash sanasini tanlang:"
    markup = build_calendar(today.year, today.month, mode)

    # reply keyboard (bekatlar)ni yashirib qo'yamiz
    if update.message:
        await update.effective_message.reply_text(caption, reply_markup=ReplyKeyboardRemove())
        await update.effective_message.reply_text("Kalendar:", reply_markup=markup)
    else:
        # callbackdan chaqirilsa
        await update.callback_query.message.reply_text(caption, reply_markup=markup)


async def check_and_notify(app: Application, chat_id: int):
    api = await fetch_trains(DEP, ARV, DATE)
    cur = make_summary(api)

    prev = load_state()
    if prev is None:
        save_state(cur)
        await app.bot.send_message(chat_id=chat_id, text="✅ Birinchi ishga tushdi. Holat saqlandi, kuzatish boshlandi.")
        return

    changes = diff(prev, cur)
    if changes:
        save_state(cur)
        msg = "🚆 Yangilik topildi!\n\n" + "\n".join(changes)
        await app.bot.send_message(chat_id=chat_id, text=msg[:3500])

async def start(update, context):
    # ✅ avval ro‘yxatdan o‘tgan bo‘lsa, telefon so‘ramaydi
    if context.user_data.get("phone"):
        first = (update.effective_user.first_name or "Birodar").strip()
        await update.effective_message.reply_text(
            f"Salom, {first}!\n"
            "Men O‘zbekiston temir yo‘l poyezd chiptalaridagi o‘zgarishlarni kuzataman va sizga habar beraman.\n\n"
            "Yo‘nalishni tanlang 👇",
            reply_markup=MAIN_KB
        )
        return

    # ❗️ birinchi marta
    await update.effective_message.reply_text("Poyezd Chiptalari Kuzatuvchi botiga xush kelibsiz!")
    await update.effective_message.reply_text(
        "📱 Iltimos, telefon raqamingizni yuboring yoki tugmadan foydalaning.",
        reply_markup=PHONE_KB
    )


def _need_phone(context) -> bool:
    return not context.user_data.get("phone")


async def phone_contact_handler(update, context):
    contact = update.effective_message.contact
    if not contact or not contact.phone_number:
        await update.effective_message.reply_text(
            "📱 Iltimos, pastdagi tugma orqali telefon raqamingizni yuboring.",
            reply_markup=PHONE_KB
        )
        return

    # ✅ shu 2 qator eng muhim
    context.user_data["phone"] = contact.phone_number
    context.user_data["registered"] = True

    await update.effective_message.reply_text(
        "Telefon raqamingiz qabul qilindi ✅",
        reply_markup=ReplyKeyboardRemove()
    )

    first = (update.effective_user.first_name or "Birodar").strip()
    await update.effective_message.reply_text(
        f"Salom, {first}!\n"
        "Men O‘zbekiston temir yo‘l poyezd chiptalaridagi o‘zgarishlarni kuzataman va sizga habar beraman.\n\n"
        "Yo‘nalishni tanlang 👇",
        reply_markup=MAIN_KB
    )


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    watch_chats = context.application.bot_data.get("watch_chats", {})
    if not isinstance(watch_chats, dict):
        watch_chats = {}

    # Agar avval qidiruv qilingan bo‘lsa, shu chatda data bo‘ladi.
    # Bo‘lmasa ham enabled qilib qo'yamiz, keyin qidiruvdan keyin to‘liq to‘ldiramiz.
    w = watch_chats.get(chat_id, {})
    w["enabled"] = True
    watch_chats[chat_id] = w

    context.application.bot_data["watch_chats"] = watch_chats

    await update.effective_message.reply_text(
        f"✅ Kuzatish yoqildi. Har {POLL_SECONDS} soniyada tekshiraman.\n"
        "📍 Avval yo‘nalish va sana oralig‘ini tanlang, keyin kuzatish ishlaydi."
    )


async def start_route(update, context):
    if _need_phone(context):
        await update.effective_message.reply_text(
            "📱 Avval telefon raqamingizni yuboring.",
            reply_markup=PHONE_KB
        )
        return
    context.user_data.clear()
    context.user_data["step"] = "dep_query"

    await update.effective_message.reply_text(
        "Qayerdan ketasiz? (Bekatni yozing. Misol uchun: Toshkent)",
        reply_markup=ReplyKeyboardRemove()
    )

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.user_data.clear()

    if has_active_watch(context, chat_id):
        await update.effective_message.reply_text("Menyu 👇", reply_markup=kb_watch_controls())
    else:
        await update.effective_message.reply_text("Menyu 👇", reply_markup=kb_route_only())


def _stations_keyboard(items: list[dict], page: int = 0, per_page: int = 10):
    """
    items: [{"code","name"}]
    tugma matni: NAME | CODE
    """
    start = page * per_page
    chunk = items[start:start + per_page]

    keyboard = []
    for s in chunk:
        keyboard.append([f"{s['name']}"])

    nav = []
    if page > 0:
        nav.append("⬅️ Oldingi")
    if start + per_page < len(items):
        nav.append("➡️ Keyingi")
    if nav:
        keyboard.append(nav)

    keyboard.append(["🔙 Orqaga"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# def _parse_station_choice(text: str):
#     """
#     'TASHKENT | 2900000' -> ('TASHKENT','2900000')
#     """
#     if "|" not in text:
#         return None, None
#     name, code = [x.strip() for x in text.split("|", 1)]
#     if not code.isdigit():
#         return None, None
#     return name, code


async def choose_dep(update, context):
    dep = update.message.text.strip()

    if dep not in STATIONS:
        await update.effective_message.reply_text("Iltimos, ro‘yxatdan bekat tanlang.")
        return

    context.user_data["dep"] = dep
    context.user_data["step"] = "choose_arv"

    # borish ro'yxati: depni olib tashlaymiz
    arv_list = [s for s in STATIONS if s != dep]
    keyboard = [arv_list[i:i+2] for i in range(0, len(arv_list), 2)]
    keyboard.append(["🔙 Orqaga"])

    await update.effective_message.reply_text(
        "Qayerga borasiz? (bekatni tanlang)",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def choose_arv(update, context):
    arv = update.message.text.strip()

    if arv not in STATIONS:
        await update.effective_message.reply_text("Iltimos, ro‘yxatdan bekat tanlang.")
        return

    dep = context.user_data.get("dep")
    if not dep:
        await update.effective_message.reply_text("Avval ketish bekatini tanlang.")
        return

    if arv == dep:
        await update.effective_message.reply_text("Borish bekati ketish bekati bilan bir xil bo‘lmasin.")
        return

    context.user_data["arv"] = arv
    context.user_data["step"] = "choose_date_from"

    await send_calendar(update, context, "from")


async def dep_query(update, context):
    text = (update.message.text or "").strip()

    if text == "🔙 Orqaga":
        await back_to_main(update, context)
        return

    # qidiruv
    stations = await search_stations(text)
    if not stations:
        await update.effective_message.reply_text(
            "Hech narsa topilmadi. Yana yozing (misol: Toshkent, Samarqand)."
        )
        return

    context.user_data["dep_candidates"] = stations
    context.user_data["dep_page"] = 0
    context.user_data["step"] = "dep_select"

    await update.effective_message.reply_text(
        "Topilgan bekatlardan birini tanlang:",
        reply_markup=_stations_keyboard(stations, page=0)
    )


async def dep_select(update, context):
    text = (update.message.text or "").strip()

    if text == "🔙 Orqaga":
        await back_to_main(update, context)
        return

    stations = context.user_data.get("dep_candidates") or []
    page = int(context.user_data.get("dep_page") or 0)

    if text == "➡️ Keyingi":
        page += 1
        context.user_data["dep_page"] = page
        await update.effective_message.reply_text(
            "Topilgan bekatlardan birini tanlang:",
            reply_markup=_stations_keyboard(stations, page=page)
        )
        return

    if text == "⬅️ Oldingi":
        page = max(0, page - 1)
        context.user_data["dep_page"] = page
        await update.effective_message.reply_text(
            "Topilgan bekatlardan birini tanlang:",
            reply_markup=_stations_keyboard(stations, page=page)
        )
        return

    # format: NAME (CODE)
    if "(" not in text or ")" not in text:
        await update.effective_message.reply_text("Iltimos, keyboarddan tanlang.")
        return

    code = text.split("(")[-1].split(")")[0].strip()
    name = text.split("(")[0].strip()

    if not code.isdigit():
        await update.effective_message.reply_text("Noto‘g‘ri tanlov. Qayta tanlang.")
        return

    context.user_data["dep"] = name
    context.user_data["dep_code"] = code

    # endi borish bekati
    context.user_data["step"] = "arv_query"
    await update.effective_message.reply_text(
        "Qayerga borasiz? (Bekatni yozing. Misol uchun: Termiz)",
        reply_markup=ReplyKeyboardRemove()
    )


async def arv_query(update, context):
    text = (update.message.text or "").strip()

    if text == "🔙 Orqaga":
        await back_to_main(update, context)
        return

    stations = await search_stations(text)
    if not stations:
        await update.effective_message.reply_text(
            "Hech narsa topilmadi. Yana yozing (misol: Termiz, Nukus, Buxoro)."
        )
        return

    # dep bilan bir xil kodni chiqarib tashlaymiz
    dep_code = context.user_data.get("dep_code")
    stations = [s for s in stations if s.get("code") != dep_code]

    context.user_data["arv_candidates"] = stations
    context.user_data["arv_page"] = 0
    context.user_data["step"] = "arv_select"

    await update.effective_message.reply_text(
        "Topilgan bekatlardan birini tanlang:",
        reply_markup=_stations_keyboard(stations, page=0)
    )


async def arv_select(update, context):
    text = (update.message.text or "").strip()

    if text == "🔙 Orqaga":
        await back_to_main(update, context)
        return

    stations = context.user_data.get("arv_candidates") or []
    page = int(context.user_data.get("arv_page") or 0)

    if text == "➡️ Keyingi":
        page += 1
        context.user_data["arv_page"] = page
        await update.effective_message.reply_text(
            "Topilgan bekatlardan birini tanlang:",
            reply_markup=_stations_keyboard(stations, page=page)
        )
        return

    if text == "⬅️ Oldingi":
        page = max(0, page - 1)
        context.user_data["arv_page"] = page
        await update.effective_message.reply_text(
            "Topilgan bekatlardan birini tanlang:",
            reply_markup=_stations_keyboard(stations, page=page)
        )
        return

    if "(" not in text or ")" not in text:
        await update.effective_message.reply_text("Iltimos, keyboarddan tanlang.")
        return

    code = text.split("(")[-1].split(")")[0].strip()
    name = text.split("(")[0].strip()

    if not code.isdigit():
        await update.effective_message.reply_text("Noto‘g‘ri tanlov. Qayta tanlang.")
        return

    context.user_data["arv"] = name
    context.user_data["arv_code"] = code

    # keyingi bosqich: kalendar
    context.user_data["step"] = "choose_date_to"  # send_calendar ichida mode="from" bo'lgani uchun bu emas
    context.user_data["step"] = "choose_date_from"
    await send_calendar(update, context, "from")



async def choose_date_from(update, context):
    text = update.message.text.strip()

    try:
        date.fromisoformat(text)
    except ValueError:
        await update.effective_message.reply_text("Sanani tugmadan tanlang (YYYY-MM-DD).")
        return

    context.user_data["date_from"] = text
    context.user_data["step"] = "choose_date_to"

    await update.effective_message.reply_text(
        "Tugash sanasini tanlang (YYYY-MM-DD):",
        reply_markup=ReplyKeyboardMarkup(build_date_keyboard(), resize_keyboard=True)
    )

async def choose_date_to(update, context):
    text = update.message.text.strip()

    try:
        to_d = date.fromisoformat(text)
    except ValueError:
        await update.effective_message.reply_text("Sanani tugmadan tanlang (YYYY-MM-DD).")
        return

    from_text = context.user_data.get("date_from")
    from_d = date.fromisoformat(from_text)

    if to_d < from_d:
        await update.effective_message.reply_text("Tugash sanasi boshlanish sanasidan oldin bo‘lmasin.")
        return

    context.user_data["date_to"] = text
    context.user_data["step"] = "done_range"

    dep = context.user_data.get("dep_name") or context.user_data.get("dep")
    arv = context.user_data.get("arv_name") or context.user_data.get("arv")

    await update.effective_message.reply_text(
        f"✅ Tanlandi:\n"
        f"📍 {dep} → {arv}\n"
        f"🗓 {from_text} .. {text}\n\n"
        f"Endi qidiruvni boshlaymiz (keyingi qadam)."
    )

async def calendar_handler(update, context):
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
        await query.edit_message_reply_markup(reply_markup=build_calendar(y, m, mode))
        return

    if action == "CANCEL":
        context.user_data.pop("date_from", None)
        context.user_data.pop("date_to", None)
        context.user_data["step"] = None

        await query.edit_message_text("❌ Bekor qilindi.")
        # xohlasangiz asosiy menyu tugmalarini qaytarib qo'yamiz:
        reply_keyboard = [
            ["📍 Yo'nalishni kiritish"],
            ["/watch — kuzatishni yoqish"],
            ["/now — hozir tekshirish"],
            ["/stop — kuzatishni o‘chirish"]
        ]
        await query.message.reply_text(
            "Asosiy menyu 👇",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
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
                    await query.edit_message_text("❌ Boshlanish sana bugundan oldin bo‘lishi mumkin emas.")
                except Exception:
                    pass

                # yangi xabar + kalendar
                await query.message.reply_text(
                    "🗓 Iltimos, bugundan yoki keyingi sanadan tanlang:",
                    reply_markup=build_calendar(today.year, today.month, "from")
                )
                return

            # Agar to‘g‘ri bo‘lsa davom etamiz
            selected = selected_date.isoformat()
            context.user_data["date_from"] = selected
            context.user_data["step"] = "choose_date_to"

            # Bosilgan kalendarni yopamiz
            await query.edit_message_text(f"✅ Boshlanish sana tanlandi: {fmt_date(selected)}")

            # Tugash kalendari chiqadi
            await query.message.reply_text(
                "🗓 Tugash sanasini tanlang:",
                reply_markup=build_calendar(y, m, "to")
            )
            return



        if mode == "to":
            from_text = context.user_data.get("date_from")
            if not from_text:
                await query.edit_message_text("Avval boshlanish sanani tanlang.")
                return

            from_d = date.fromisoformat(from_text)
            to_d = date.fromisoformat(selected)

            MAX_DAYS = 3  # maksimal 3 kunlik oraliq

            if (to_d - from_d).days > (MAX_DAYS - 1):
                # Eski tugash kalendar xabarini yopamiz (xato yozuvi bilan)
                try:
                    await query.edit_message_text("❌ Maksimal 3 kun tanlash mumkin.")
                except Exception:
                    pass

                # Yangi xabar + yangi kalendar (qayta tanlash uchun)
                await query.message.reply_text(
                    "🗓 Tugash sanani qayta tanlang (maksimal 3 kun).",
                    reply_markup=build_calendar(y, m, "to")
                )
                return

            if to_d < from_d:
                # 1) Bosilgan (eski) kalendar xabarini yopamiz
                try:
                    await query.edit_message_text("❌ Tugash sanasi boshlanish sanasidan oldin bo‘lmasin.\n")
                except Exception:
                    pass

                # 2) Yangi xato xabari + tagidan yangi kalendar
                await query.message.reply_text(
                    "🗓 Iltimos, tugash sanasini qayta tanlang:",
                    reply_markup=build_calendar(y, m, "to")
                )
                return



            context.user_data["date_to"] = selected
            context.user_data["step"] = "done_range"

            dep_name = context.user_data.get("dep_name")
            arv_name = context.user_data.get("arv_name")
            dep_code = context.user_data.get("dep_code")
            arv_code = context.user_data.get("arv_code")

            # 1) BOSILGAN KALENDAR XABARINI YOPAMIZ (shu o'zi!)
            await query.edit_message_text(f"🗓 Tugash sanasini tanlang:")

            # 2) (ixtiyoriy) tugash sana alohida SMS kerak bo‘lsa — mana bu qolsin:
            await query.message.reply_text(f"✅ Tugash sana tanlandi: {fmt_date(selected)}")

            # route matni: avval nom, bo‘lmasa kod
            route_text = ""
            if dep_name and arv_name:
                route_text = f"📍 {dep_name} → {arv_name}\n"
            elif dep_code and arv_code:
                route_text = f"📍 {dep_code} → {arv_code}\n"

            await query.message.reply_text(
                "✅ Sana oralig‘i tanlandi !\n"
                f"{route_text}"
                f"🗓 {fmt_date(from_text)} ⟷ {fmt_date(selected)}\n\n"
                "Shu oraliqda qidiruv natijalarini chiqaramiz."
            )
            await search_in_range_and_show(update, context)
            return

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if _need_phone(context):
    #     await update.effective_message.reply_text(
    #         "📱 Avval telefon raqamingizni yuboring.",
    #         reply_markup=PHONE_KB
    #     )
    #     return
    chat_id = update.effective_chat.id

    watch_chats = context.application.bot_data.get("watch_chats", {})
    if isinstance(watch_chats, dict):
        watch_chats.pop(chat_id, None)
        context.application.bot_data["watch_chats"] = watch_chats

    # istasangiz user_data ham tozalanadi:
    context.user_data.clear()

    await update.effective_message.reply_text(
        "⛔ Kuzatish to‘xtatildi.\n"
        "Yangi yo‘nalish kiritish uchun pastdagi tugmani bosing 👇",
        reply_markup=MAIN_KB
    )

async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watch_chats = context.application.bot_data.get("watch_chats", {})
    cfg = watch_chats.get(chat_id)

    if not cfg or not cfg.get("enabled"):
        await update.effective_message.reply_text(
            "❌ Kuzatuv ma’lumoti to‘liq emas. Qaytadan yo‘nalish kiriting.",
            reply_markup=MAIN_KB
        )
        return

    dep_code = cfg["dep"]
    arv_code = cfg["arv"]
    d_from = cfg["from"]
    d_to = cfg["to"]
    dep_name = cfg.get("dep_name") or str(dep_code)
    arv_name = cfg.get("arv_name") or str(arv_code)

    await update.effective_message.reply_text("🔎 Tekshiryapman...")

    full_text = "🎟 Mavjud chiptalar:\n"
    full_text += f"📍 {dep_name} → {arv_name}\n"
    snapshot = {}

    for d in iter_dates(d_from, d_to):
        api = await fetch_trains(dep_code, arv_code, d)
        full_text += format_trains(d, api, dep_name, arv_name)
        snapshot[d] = api

    await update.effective_message.reply_text(full_text, reply_markup=WATCH_KB)

    # snapshot yangilanib qolsin (keyingi kuzatuv uchun)
    cfg["snapshot"] = snapshot
    watch_chats[chat_id] = cfg
    context.application.bot_data["watch_chats"] = watch_chats


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    chat_id = app.bot_data.get("watch_chat_id") or (int(WATCH_CHAT_ID) if WATCH_CHAT_ID else None)
    if chat_id:
        await check_and_notify(app, chat_id)

async def route_button_router(update, context):
    text = (update.message.text or "").strip()

    if text == "📍 Yo'nalishni kiritish":
        await start_route(update, context)
        return

    if text == "🔙 Orqaga":
        await back_to_main(update, context)
        return

    # agar hozircha boshqa matn bo'lsa, e'tibor bermaymiz


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN .env ichida yo'q yoki bo'sh")
    
    base_dir = Path(__file__).resolve().parent
    pkl_path = base_dir / "bot_data.pkl"
    
    persistence = PicklePersistence(filepath="bot_data.pkl")  # ✅ shu faylga saqlanadi

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)   # ✅ MUHIM
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.CONTACT, phone_contact_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^📍 Yo'nalishni kiritish$"), start_route))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, flow_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^🔙 Orqaga$"), back_to_main))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("now", now))
    app.add_handler(CallbackQueryHandler(inline_button_handler, pattern=r"^empty$"))

    # JobQueue PTB ichida ishlaydi (event loop muammosiz)
    app.job_queue.run_repeating(watcher_job, interval=POLL_SECONDS, first=10)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_button_router))
    app.add_handler(CallbackQueryHandler(calendar_handler, pattern=r"^CAL\|"))

    print("Bot ishga tushdi...")
    app.run_polling()

async def inline_button_handler(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "empty":
        await query.edit_message_text("Hozircha bu tugma bo‘sh 🙂")

async def flow_handler(update, context):
    # # Registratsiya tugamaguncha hamma narsa blok
    # if _need_phone(context):
    #     await update.effective_message.reply_text(
    #         "📱 Iltimos, telefon raqamingizni yuboring (pastdagi tugma orqali) 👇",
    #         reply_markup=PHONE_KB
    #     )
    #     return
    step = context.user_data.get("step")
    # Registratsiya tugamaguncha boshqa narsaga o'tkazmaymiz
    if step == "need_phone":
        await update.effective_message.reply_text(
            "📱 Iltimos, telefon raqamingizni tugma orqali yuboring 👇",
            reply_markup=PHONE_KB
        )
        return
    text = (update.message.text or "").strip()

    # Orqaga
    if text == "🔙 Orqaga":
        await back_to_main(update, context)
        return

    # pagination
    if text in ("⬅️ Oldingi", "➡️ Keyingi"):
        items = context.user_data.get("station_items") or []
        page = int(context.user_data.get("station_page") or 0)
        if text == "⬅️ Oldingi":
            page = max(0, page - 1)
        else:
            page = page + 1

        context.user_data["station_page"] = page
        await update.effective_message.reply_text(
            "Topilgan bekatlardan birini tanlang:",
            reply_markup=_stations_keyboard(items, page=page)
        )
        return

    # 1) Ketish bekati: foydalanuvchi yozadi -> qidiramiz -> keyboard chiqaramiz
    if step == "dep_query":
        q = text
        if len(q) < 3:
            await update.effective_message.reply_text("❗ Kamida 3 ta harf yozing. Masalan: Toshkent")
            return
        await update.effective_message.reply_text("🔎 Qidiryapman...")
        items = await search_stations(q)

        if not items:
            await update.effective_message.reply_text("❌ Bekat topilmadi. Yana yozib ko‘ring.")
            return

        context.user_data["station_items"] = items
        context.user_data["station_page"] = 0
        context.user_data["step"] = "dep_pick"

        await update.effective_message.reply_text(
            "Topilgan bekatlardan birini tanlang:",
            reply_markup=_stations_keyboard(items, page=0)
        )
        return

    # 2) Ketish bekati: foydalanuvchi keyboard’dan tanlaydi
    if step == "dep_pick":
        items = context.user_data.get("station_items") or []
        selected = next((s for s in items if s["name"] == text), None)

        if not selected:
            await update.effective_message.reply_text("Iltimos, ro‘yxatdan tanlang (tugmani bosing).")
            return

        context.user_data["dep_name"] = selected["name"]
        context.user_data["dep_code"] = selected["code"]

        context.user_data["step"] = "arv_query"
        context.user_data.pop("station_items", None)
        context.user_data.pop("station_page", None)

        await update.effective_message.reply_text(
            "Qayerga borasiz? (Bekatni yozing. Misol uchun: Termiz)",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    # 3) Borish bekati: foydalanuvchi yozadi -> qidiramiz -> keyboard chiqaramiz
    if step == "arv_query":
        q = text
        if len(q) < 3:
            await update.effective_message.reply_text("❗ Kamida 3 ta harf yozing. Masalan: Toshkent")
            return
        await update.effective_message.reply_text("🔎 Qidiryapman...")
        items = await search_stations(q)

        if not items:
            await update.effective_message.reply_text("❌ Bekat topilmadi. Yana yozib ko‘ring.")
            return

        context.user_data["station_items"] = items
        context.user_data["station_page"] = 0
        context.user_data["step"] = "arv_pick"

        await update.effective_message.reply_text(
            "Topilgan bekatlardan birini tanlang:",
            reply_markup=_stations_keyboard(items, page=0)
        )
        return

    # 4) Borish bekati: tanlash
    if step == "arv_pick":
        items = context.user_data.get("station_items") or []
        selected = next((s for s in items if s["name"] == text), None)

        if not selected:
            await update.effective_message.reply_text("Iltimos, ro‘yxatdan tanlang (tugmani bosing).")
            return

        dep_code = context.user_data.get("dep_code")
        if selected["code"] == dep_code:
            await update.effective_message.reply_text("❌ Borish bekati ketish bekati bilan bir xil bo‘lmasin.")
            return

        context.user_data["arv_name"] = selected["name"]
        context.user_data["arv_code"] = selected["code"]

        context.user_data["step"] = "choose_date_from"
        context.user_data.pop("station_items", None)
        context.user_data.pop("station_page", None)

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

def format_trains(d, api, dep_name, arv_name):
    text = f"\n📅 {fmt_date_obj(d) if hasattr(d,'day') else fmt_date(d)}\n"

    trains = (
        api.get("data", {})
           .get("directions", {})
           .get("forward", {})
           .get("trains", [])
    )

    if not trains:
        return text + "❌ Bu kunda poyezd topilmadi.\n"

    j = 0

    for i, t in enumerate(trains, start=1):
        num = t.get("number")

        total_places = sum(
            int(car.get("freeSeats") or 0)
            for car in t.get("cars", [])
        )
        # ✅ Joyi yo‘q poyezdni umuman chiqarma
        if total_places <= 0:
            j += 1
            continue

        # 1) Poyezdning asl yo‘nalishi (butun marshrut)
        full_from = t.get("originRoute", {}).get("depStationName") or ""
        full_to = t.get("originRoute", {}).get("arvStationName") or ""

        # 2) Siz tanlagan segment (subRoute)
        seg_from = t.get("subRoute", {}).get("depStationName") or dep_name
        seg_to = t.get("subRoute", {}).get("arvStationName") or arv_name

        dep_time = t.get("departureDate")  # masalan: "21.01.2026 21:13"
        arv_time = t.get("arrivalDate")
        way = t.get("timeOnWay")

        total_places = sum(
            int(car.get("freeSeats") or 0)
            for car in t.get("cars", [])
        )

        text += (
            f"🚆 #{i}:  {num}  {full_from} → {full_to}\n"
            f"🕒 {seg_from.upper()} / ({dep_time})\n"
            f"🕒 {seg_to.upper()} / ({arv_time})\n"
            f"⏱️ Yo‘l davomiyligi: {way}\n"
            f"📋 Bo‘sh o‘rinlar : {total_places} ta joy\n"
        )

        for c in t.get("cars", []):
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
                    text += f"• {ctype} : {free} ta joy → {price_txt} so‘m\n"
                else:
                    text += (
                        f"• {ctype} : {free} ta joy "
                        f"(Tepa {tepa} ta, Pastki {pastki} ta) "
                        f"→ {price_txt} so‘m\n"
                    )
            else:
                text += f"• {ctype} : {free} ta joy → {price_txt} so‘m\n"

        text += "\n"

    if j == i:
        text += "❌ Bu kunda bo‘sh joy topilmadi.\n"

    return text


async def search_in_range_and_show(update, context):
    if update.message:
        msg = update.message
    else:
        msg = update.callback_query.message

    dep_code = context.user_data.get("dep_code") or context.user_data.get("dep")
    arv_code = context.user_data.get("arv_code") or context.user_data.get("arv")

    if not dep_code or not arv_code:
        await update.effective_message.reply_text("❌ Bekat tanlanmagan. Qaytadan 📍 Yo'nalishni kiritish qiling.")
        return
    
    d_from = context.user_data["date_from"]
    d_to = context.user_data["date_to"]

    await update.effective_message.reply_text("🔍 Qidiruv boshlandi...")
    dep_name = context.user_data.get("dep_name") or context.user_data.get("dep") or "—"
    arv_name = context.user_data.get("arv_name") or context.user_data.get("arv") or "—"
    full_text = "🎟 Mavjud chiptalar:\n"
    full_text += f"📍 {dep_name} → {arv_name}\n"
    snapshot = {}

    for d in iter_dates(d_from, d_to):
        api = await fetch_trains(dep_code, arv_code, d)
        dep_name = context.user_data.get("dep_name") or context.user_data.get("dep") or str(dep_code)
        arv_name = context.user_data.get("arv_name") or context.user_data.get("arv") or str(arv_code)
        full_text += format_trains(d, api, dep_name, arv_name)
        snapshot[d] = api

    # ✅ faqat 1 marta yuboramiz
    await update.effective_message.reply_text(full_text)

    # ✅ kuzatishni yoqamiz va hamma kerakli narsani saqlaymiz
    context.user_data["watch_enabled"] = True
    context.user_data["snapshot"] = snapshot
    context.user_data["watch_dep"] = dep_code
    context.user_data["watch_arv"] = arv_code
    context.user_data["watch_from"] = d_from
    context.user_data["watch_to"] = d_to
    context.user_data["watch_chat_id"] = msg.chat_id

    chat_id = update.effective_chat.id

    watch_chats = context.application.bot_data.get("watch_chats", {})
    if not isinstance(watch_chats, dict):
        watch_chats = {}

    watch_chats[chat_id] = {
        "enabled": True,
        "dep_code": dep_code,
        "arv_code": arv_code,
        "dep_name": context.user_data.get("dep_name"),
        "arv_name": context.user_data.get("arv_name"),
        "date_from": d_from,
        "date_to": d_to,
        "snapshot": snapshot,
    }
    context.application.bot_data["watch_chats"] = watch_chats

    await update.effective_message.reply_text(
        "🔔 Kuzatish boshlandi.\n"
        "Agar joylar kamayib yoki ko‘payib ketsa,\n"
        "yoki yangi vagon chiqsa — darhol habar beraman.",
        reply_markup=WATCH_KB
    )



    # ✅ JobQueue ko‘ra olishi uchun application.chat_data ga ham yozamiz
    watch_chats = context.application.bot_data.get("watch_chats", {})

    watch_chats[msg.chat_id] = {
        "enabled": True,
        "dep": dep_code,
        "arv": arv_code,
        "from": d_from,
        "to": d_to,
        "snapshot": snapshot,
        "dep_name": context.user_data.get("dep_name"),
        "arv_name": context.user_data.get("arv_name"),
    }

    context.application.bot_data["watch_chats"] = watch_chats



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

async def watcher_job(context):
    """
    Har POLL_SECONDS da tekshiradi.
    O'zgarish bo'lsa chatga yuboradi va snapshotni yangilaydi.
    """
    # user_data yo'q: JobQueue faqat application context beradi,
    # shuning uchun biz chat_id larni application.chat_data da yuritamiz.
    # Lekin siz bitta user bilan ishlayapsiz — shuning uchun sodda yo'l:
    chats = context.application.bot_data.get("watch_chats", {})

    if not chats:
        return

    for chat_id, w in list(chats.items()):
        if not w.get("enabled"):
            continue

        dep = w["dep"]
        arv = w["arv"]
        d_from = w["from"]
        d_to = w["to"]
        old_snapshot = w.get("snapshot", {})

        new_snapshot = {}
        changed_days = []

        for d in iter_dates(d_from, d_to):
            api = await fetch_trains(dep, arv, d)
            new_snapshot[d] = api

            old_api = old_snapshot.get(d)
            if old_api is not None and diff_snapshot(old_api, api):
                changed_days.append(d)

        if changed_days:
            dep_name = w.get("dep_name") or dep
            arv_name = w.get("arv_name") or arv

            text = (
                "🚨 O‘zgarish aniqlandi!\n"
                f"📍 {dep_name} → {arv_name}\n"
            )

            for d in changed_days:
                text += f"\n\n📅 {fmt_date(d)}\n"
                old_api = old_snapshot.get(d, {})
                new_api = new_snapshot.get(d, {})
                text += _watch_day_report(old_api, new_api)

            text += "\n\n🔄 Kuzatishda davom etaman."

            await context.bot.send_message(chat_id=chat_id, text=text)

            # yangisini saqlab qo'yamiz
            w["snapshot"] = new_snapshot
            chats[chat_id] = w


    context.application.bot_data["watch_chats"] = chats


if __name__ == "__main__":
    main()