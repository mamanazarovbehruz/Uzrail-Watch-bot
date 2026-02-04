import os
import httpx
import asyncio
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import json
from urllib.parse import unquote

load_dotenv()

BASE = "https://eticket.railway.uz"
ENDPOINT = f"{BASE}/api/v3/handbook/trains/list"
CSRF_URL = f"{BASE}/api/v1/csrf-token"

_cookie_lang = None
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

def _manual_headers(lang: str = "uz") -> dict:
    lang = (lang or "uz").lower()
    if lang not in ("uz", "ru", "en"):
        lang = "uz"

    return {
        "Accept": "application/json",
        "Accept-Language": lang,
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/{lang}/home",
        "User-Agent": "Mozilla/5.0",
        "X-XSRF-TOKEN": XSRF,
        "device-type": "BROWSER",
        "Cookie": COOKIE,
    }



async def _fetch_trains_manual(depStationCode, arvStationCode, date_iso, lang: str = "uz"):
    payload = {
        "directions": {
            "forward": {
                "date": date_iso,
                "depStationCode": depStationCode,
                "arvStationCode": arvStationCode
            }
        }
    }

    headers = _manual_headers(lang)
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        r = await client.post(ENDPOINT, json=payload)
        text = (r.text or "")[:300]

        # ✅ 400/401/403/419 bo'lsa RuntimeError formatida qaytaramiz
        # (fetch_trains() ichidagi refresh/retry shuni taniydi)
        if r.status_code in (400, 401, 403, 419):
            raise RuntimeError(f"API status={r.status_code}. Body: {text}")

        if r.status_code != 200:
            raise RuntimeError(f"API status={r.status_code}. Body: {text}")

        return r.json()


async def _fetch_trains_auto(depStationCode, arvStationCode, date_iso, lang: str = "en"):
    payload = {
        "directions": {
            "forward": {
                "date": date_iso,
                "depStationCode": str(depStationCode),
                "arvStationCode": str(arvStationCode)
            }
        }
    }

    # Retry sozlamalari
    max_tries = int(os.getenv("HTTP_MAX_RETRIES", "3") or "3")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ENGLISH home ko'proq stabil bo'ladi
        context = await browser.new_context(
            extra_http_headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": lang,
                "Origin": BASE,
                "Referer": f"{BASE}/{lang}/home",
                "device-type": "web",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        )

        page = await context.new_page()

        last_text = ""
        for attempt in range(1, max_tries + 1):
            # 1) Home -> session cookie
            await page.goto(f"{BASE}/en/home", wait_until="domcontentloaded")

            # 2) CSRF endpoint -> XSRF-TOKEN cookie
            await context.request.get(CSRF_URL)

            # 3) Cookie ichidan XSRF-TOKEN ni olamiz (URL-encoded bo'lishi mumkin)
            cookies = await context.cookies()
            xsrf = ""
            for c in cookies:
                if c.get("name") == "XSRF-TOKEN":
                    xsrf = c.get("value") or ""
                    break

            xsrf = unquote(xsrf)  # <<< MUHIM

            if not xsrf:
                last_text = "XSRF-TOKEN cookie topilmadi."
                await asyncio.sleep(0.6 * attempt)
                continue

            headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": lang,
                "Content-Type": "application/json",
                "Origin": BASE,
                "Referer": f"{BASE}/{lang}/home",
                "device-type": "web",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "X-XSRF-TOKEN": xsrf,
            }

            body = json.dumps(payload)
            resp = await context.request.post(ENDPOINT, headers=headers, data=body)

            if resp.status == 200:
                data = await resp.json()
                await browser.close()
                return data

            last_text = (await resp.text())[:300]

            # 400/401/403/419 -> odatda sessiya/csrf "sinadi", retry qilamiz
            if resp.status in (400, 401, 403, 419):
                await asyncio.sleep(0.8 * attempt)
                continue

            await browser.close()
            raise RuntimeError(f"API status={resp.status}. Body: {last_text}")

        await browser.close()
        raise RuntimeError(f"API failed after {max_tries} retries. Last body: {last_text}")

async def fetch_trains(depStationCode, arvStationCode, date_iso, lang: str = "uz"):
    global _cookie_lang
    lang = (lang or "uz").lower()
    if lang not in ("uz", "ru", "en"):
        lang = "uz"

    # ✅ til o'zgargan bo'lsa cookie ham shu tilga mos yangilanadi
    if _cookie_lang != lang:
        new_cookie, new_xsrf = await refresh_cookie_via_playwright(lang=lang, max_tries=3)
        os.environ["COOKIE"] = new_cookie
        os.environ["XSRF_TOKEN"] = new_xsrf
        _cookie_lang = lang
        
    # 1) avval MANUAL urinamiz (eng stabil)
    try:
        return await _fetch_trains_manual(depStationCode, arvStationCode, date_iso, lang)
    except RuntimeError as e:
        msg = str(e)
        # 400/401/403/419 bo'lsa cookie eskirgan bo'lishi mumkin
        if any(x in msg for x in ["API status=400", "API status=401", "API status=403", "API status=419"]):
            # 2) cookie yangilab, yana bir marta urinib ko'ramiz
            new_cookie, new_xsrf = await refresh_cookie_via_playwright(lang=lang, max_tries=3)

            # global/env o'rniga runtime-da ishlatamiz:
            os.environ["COOKIE"] = new_cookie
            os.environ["XSRF_TOKEN"] = new_xsrf

            return await _fetch_trains_manual(depStationCode, arvStationCode, date_iso, lang)

        raise


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

async def search_stations(name: str, lang: str = "uz") -> list[dict]:
    """
    eticket.railway.uz dan bekatlarni qidiradi.
    return: [{"code":"2900000","name":"TASHKENT"}, ...]
    """
    name = (name or "").strip()
    if not name:
        return []

    headers = _manual_headers(lang).copy()
    headers["Referer"] = f"{BASE}/{lang}/home"
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


async def refresh_cookie_via_playwright(lang: str = "en", max_tries: int = 3) -> tuple[str, str]:
    """
    returns: (COOKIE_HEADER, XSRF_TOKEN)
    """
    lang = (lang or "en").lower()
    if lang not in ("uz", "ru", "en"):
        lang = "en"

    last_err = None
    for attempt in range(1, max_tries + 1):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    extra_http_headers={"Accept-Language": lang}
                )
                page = await context.new_page()

                await page.goto(f"{BASE}/{lang}/home", wait_until="domcontentloaded")
                await context.request.get(CSRF_URL, timeout=45000)

                cookies = await context.cookies()
                await browser.close()

            xsrf = ""
            cookie_parts = []
            for c in cookies:
                n = c.get("name")
                v = c.get("value") or ""
                if n == "XSRF-TOKEN":
                    xsrf = unquote(v)
                cookie_parts.append(f"{n}={v}")

            cookie_header = "; ".join(cookie_parts)
            if not xsrf or not cookie_header:
                raise RuntimeError("Playwright cookie refresh: XSRF yoki COOKIE bo'sh chiqdi")

            return cookie_header, xsrf

        except Exception as e:
            last_err = e
            await asyncio.sleep(0.8 * attempt)

    raise RuntimeError(f"Playwright cookie refresh failed: {last_err}")
