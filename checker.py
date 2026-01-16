import asyncio
import json
import os
from fetcher import fetch_trains, make_summary

DEP = "2900000"
ARV = "2900864"
DATE = "2026-01-30"

STATE_FILE = f"state_{DEP}_{ARV}_{DATE}.json"

def diff(prev: dict, cur: dict) -> list[str]:
    lines = []

    # yangi poezd paydo bo'lsa
    for train_key in cur.keys():
        if train_key not in prev:
            lines.append(f"➕ Yangi poezd paydo bo‘ldi: {train_key}")

    # eski poezd yo'qolsa
    for train_key in prev.keys():
        if train_key not in cur:
            lines.append(f"➖ Poezd yo‘qoldi: {train_key}")

    # poezd ichida vagon/joy o'zgarishi
    for train_key, cur_cars in cur.items():
        prev_cars = prev.get(train_key, {})

        # yangi vagon turi
        for car_type in cur_cars.keys():
            if car_type not in prev_cars:
                lines.append(f"➕ Yangi vagon: {train_key} — {car_type} (free={cur_cars[car_type]['freeSeats']})")

        # joy ko‘payishi/kamayishi
        for car_type, cur_info in cur_cars.items():
            if car_type in prev_cars:
                a = int(prev_cars[car_type].get("freeSeats") or 0)
                b = int(cur_info.get("freeSeats") or 0)
                if b != a:
                    arrow = "📈" if b > a else "📉"
                    lines.append(f"{arrow} Joy o‘zgardi: {train_key} — {car_type} {a} → {b}")

    return lines

async def main():
    api = await fetch_trains(DEP, ARV, DATE)
    cur = make_summary(api)

    if os.path.exists(STATE_FILE):
        prev = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        changes = diff(prev, cur)

        if changes:
            print("\n".join(changes))
        else:
            print("O‘zgarish yo‘q.")
    else:
        print("Birinchi ishga tushirish: holat saqlandi, hozircha xabar yo‘q.")

    json.dump(cur, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

asyncio.run(main())
