import os
import httpx
import asyncio
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import json

load_dotenv()

BASE = "https://eticket.railway.uz"
ENDPOINT = f"{BASE}/api/v3/handbook/trains/list"
CSRF_URL = f"{BASE}/api/v1/csrf-token"

_csrf_token = None
_csrf_cookies = None
_csrf_lock = asyncio.Lock()

AUTO_COOKIE = os.getenv("AUTO_COOKIE", "0").strip() == "1"

# Fallback (qo'lda berilgan cookie) - xohlasangiz keyin olib tashlaymiz
XSRF = os.getenv("XSRF_TOKEN", "")
COOKIE = os.getenv("COOKIE", "")

async def _refresh_csrf():
    """
    Yangi XSRF-TOKEN cookie ni olib keladi.
    """
    headers = _manual_headers().copy()
    headers["Referer"] = f"{BASE}/en/home"
    headers["Origin"] = BASE

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
        r = await client.get(CSRF_URL)
        # token odatda cookie ichida bo'ladi
        token = client.cookies.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")

        if not token:
            raise RuntimeError(f"CSRF token topilmadi. status={r.status_code}, body={r.text[:200]}")

        return token, client.cookies

async def _ensure_csrf():
    global _csrf_token, _csrf_cookies
    async with _csrf_lock:
        if _csrf_token and _csrf_cookies:
            return _csrf_token, _csrf_cookies
        _csrf_token, _csrf_cookies = await _refresh_csrf()
        return _csrf_token, _csrf_cookies



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
    Playwright orqali: brauzer cookie/xsrf oladi va API chaqiradi.
    """
    payload = {
        "directions": {
            "forward": {
                "date": date_iso,
                "depStationCode": depStationCode,
                "arvStationCode": arvStationCode
            }
        }
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            extra_http_headers={
                "Accept": "application/json",
                "Accept-Language": "uz",
                "Origin": BASE,
                "Referer": f"{BASE}/uz/home",
                "device-type": "BROWSER",
                "User-Agent": "Mozilla/5.0",
            }
        )

        page = await context.new_page()

        # 1) Home -> cookie/session
        await page.goto(f"{BASE}/uz/home", wait_until="domcontentloaded")

        # 2) CSRF endpoint -> XSRF-TOKEN cookie ni chiqaradi
        await context.request.get(f"{BASE}/api/v1/csrf-token")

        # 3) Cookie ichidan XSRF-TOKEN ni olamiz
        cookies = await context.cookies()
        xsrf = ""
        for c in cookies:
            if c.get("name") == "XSRF-TOKEN":
                xsrf = c.get("value") or ""
                break

        if not xsrf:
            await browser.close()
            raise RuntimeError("XSRF-TOKEN cookie topilmadi (csrf-token chaqirildi, lekin token kelmadi).")

        headers = {
            "Accept": "application/json",
            "Accept-Language": "uz",
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/uz/home",
            "device-type": "BROWSER",
            "User-Agent": "Mozilla/5.0",
            "X-XSRF-TOKEN": xsrf,
        }

        # 4) MUHIM: Playwright requestda `json=` ishlatmaymiz, data=json.dumps(...) qilamiz
        body = json.dumps(payload)
        resp = await context.request.post(ENDPOINT, headers=headers, data=body)

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



async def _fetch_json_auto(url: str, method: str = "GET", payload: dict | None = None):
    """
    Playwright orqali saytga kirib, cookie/XSRF bilan API'ni chaqiradi.
    Return: (status_code, json_or_text, is_json: bool)
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Cookie olish uchun home'ga kiramiz
        await page.goto("https://eticket.railway.uz/en/home", wait_until="domcontentloaded")

        # Cookie ichidan XSRF tokenni olamiz
        cookies = await context.cookies()
        xsrf = ""
        for c in cookies:
            if c.get("name") == "XSRF-TOKEN":
                xsrf = c.get("value") or ""
                break

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://eticket.railway.uz",
            "Referer": "https://eticket.railway.uz/en/home",
            "X-XSRF-TOKEN": xsrf,
            "device-type": "BROWSER",
        }

        req = context.request

        if method.upper() == "GET":
            resp = await req.get(url, headers=headers)
        else:
            body = json.dumps(payload or {})
            resp = await req.post(url, headers=headers, data=body)

        status = resp.status

        # JSON bo'lmasa ham yiqilmasin
        try:
            js = await resp.json()
            is_json = True
            result = js
        except Exception:
            txt = await resp.text()
            is_json = False
            result = txt

        await context.close()
        await browser.close()
        return status, result, is_json

STATIONS_ENDPOINT = f"{BASE}/api/v1/handbook/stations/list"

async def search_stations(name: str) -> list[dict]:
    """
    eticket.railway.uz dan bekatlarni qidiradi.
    return: [{"code":"2900000","name":"TASHKENT"}, ...]
    """
    name = (name or "").strip()
    if not name:
        return []

    headers = _manual_headers().copy()
    # v1 endpoint uchun referer/en ishlatamiz
    headers["Referer"] = f"{BASE}/en/home"
    headers["Origin"] = BASE

    payload = {"name": name.lower()}

    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        r = await client.post(STATIONS_ENDPOINT, json=payload)

        # masalan: r = await client.post(...)

        if r.status_code == 204:
            return []  # hech narsa yo'q, xato emas

        # Agar cookie/xsrf eskirgan bo‘lsa 403 bo‘lishi mumkin
        if r.status_code != 200:
            txt = r.text if hasattr(r, "text") else ""
            raise RuntimeError(f"stations API status={r.status_code}. Body: {txt[:300]}")
        
        js = r.json()
        stations = js.get("data", {}).get("stations", []) or []
        # faqat code/name qoldiramiz
        out = []
        for s in stations:
            if "code" in s and "name" in s:
                out.append({"code": str(s["code"]), "name": str(s["name"])})
        return out
