import os
import json
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from fetcher import fetch_trains, make_summary

load_dotenv()

WATCH_CHAT_ID = os.getenv("WATCH_CHAT_ID", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))

# Hozircha bitta yo'nalish/sana (keyin /add bilan ko'paytiramiz)
DEP = "2900000"
ARV = "2900864"
DATE = "2026-01-30"

STATE_FILE = f"state_{DEP}_{ARV}_{DATE}.json"


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom, Birodar!\n"
        "Men poezd joy/vagon o‘zgarishini kuzataman.\n\n"
        "Komandalar:\n"
        "/watch — kuzatishni shu chatga yoqish\n"
        "/now — hozir tekshirish\n"
        "/stop — kuzatishni o‘chirish"
    )


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["watch_chat_id"] = update.effective_chat.id
    await update.message.reply_text(f"✅ Kuzatish yoqildi. Har {POLL_SECONDS} soniyada tekshiraman.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data.pop("watch_chat_id", None)
    await update.message.reply_text("⛔ Kuzatish o‘chirildi.")


async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔎 Tekshiryapman...")
    await check_and_notify(context.application, chat_id)
    await update.message.reply_text("✅ Tayyor.")


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    chat_id = app.bot_data.get("watch_chat_id") or (int(WATCH_CHAT_ID) if WATCH_CHAT_ID else None)
    if chat_id:
        await check_and_notify(app, chat_id)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN .env ichida yo'q yoki bo'sh")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("now", now))

    # JobQueue PTB ichida ishlaydi (event loop muammosiz)
    app.job_queue.run_repeating(scheduled_job, interval=POLL_SECONDS, first=5)

    print("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()