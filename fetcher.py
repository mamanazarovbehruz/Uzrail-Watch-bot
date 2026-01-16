import os
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://eticket.railway.uz"
ENDPOINT = f"{BASE}/api/v3/handbook/trains/list"

AUTO_COOKIE = os.getenv("AUTO_COOKIE", "0").strip() == "1"

# Fallback (qo'lda berilgan cookie) - xohlasangiz keyin olib tashlaymiz
XSRF = os.getenv("XSRF_TOKEN", "")
COOKIE = os.getenv("COOKIE", "")


def make_summary(api_json: dict) -> dict:
    out = {}
    trains = (
        api_json.get("data", {})
        .get("directions", {})
        .get("forward", {})
        .get("trains", [])
    )

    for t in trains:
        train_key = f"{t.get('number')} | {t.get('departureDate')} -> {t.get('arrivalDate')}"
        cars_map = {}

        for car in (t.get("cars") or []):
            car_type = car.get("type")
            free = int(car.get("freeSeats") or 0)

            tariff_val = None
            tariffs = car.get("tariffs") or []
            for tr in tariffs:
                if isinstance(tr.get("tariff"), (int, float)):
                    tariff_val = tr["tariff"] if (tariff_val is None or tr["tariff"] < tariff_val) else tariff_val

            cars_map[car_type] = {"freeSeats": free, "tariff": tariff_val}

        out[train_key] = cars_map

    return out


def _manual_headers() -> dict:
    return {
        "Accept": "application/json",
        "Accept-Language": "uz",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/uz/home",
        "User-Agent": "Mozilla/5.0",
        "X-XSRF-TOKEN": XSRF,
        "device-type": "BROWSER",
        "Cookie": COOKIE,
    }


async def _fetch_trains_manual(depStationCode: str, arvStationCode: str, date_iso: str) -> dict:
    payload = {
        "directions": {
            "forward": {
                "date": date_iso,
                "depStationCode": depStationCode,
                "arvStationCode": arvStationCode
            }
        }
    }

    headers = _manual_headers()
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        r = await client.post(ENDPOINT, json=payload)
        r.raise_for_status()
        return r.json()


async def _fetch_trains_auto(depStationCode: str, arvStationCode: str, date_iso: str) -> dict:
    """
    Playwright orqali: brauzer o'zi cookie/xsrf oladi va API chaqiradi.
    """
    from playwright.async_api import async_playwright

    payload = {
        "directions": {
            "forward": {
                "date": date_iso,
                "depStationCode": depStationCode,
                "arvStationCode": arvStationCode
            }
        }
    }

    extra_headers = {
        "Accept": "application/json",
        "Accept-Language": "uz",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/uz/home",
        "device-type": "BROWSER",
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(extra_http_headers=extra_headers)

        # Muhim: home sahifaga kirib, cookie/session hosil qilamiz
        page = await context.new_page()
        await page.goto(f"{BASE}/uz/home", wait_until="domcontentloaded")

        # Endi shu context ichidan API'ni chaqiramiz (cookie avtomatik ketadi)
        resp = await context.request.post(ENDPOINT, data=None, json=payload)
        if resp.status != 200:
            text = await resp.text()
            await browser.close()
            raise RuntimeError(f"API status={resp.status}. Body: {text[:300]}")

        data = await resp.json()
        await browser.close()
        return data


async def fetch_trains(depStationCode: str, arvStationCode: str, date_iso: str) -> dict:
    if AUTO_COOKIE:
        return await _fetch_trains_auto(depStationCode, arvStationCode, date_iso)
    return await _fetch_trains_manual(depStationCode, arvStationCode, date_iso)
