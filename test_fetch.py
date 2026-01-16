import asyncio
import json
import httpx

BASE = "https://eticket.railway.uz"
ENDPOINT = f"{BASE}/api/v3/handbook/trains/list"

# Siz tanlagan yo'nalish va sana (curl'dan)
DEP = "2900000"
ARV = "2900864"
DATE = "2026-01-30"

# curl'dan olingan XSRF token
XSRF = "b4860bba-25f4-4de1-8870-8ed4beb483c6"

# curl'dagi Cookie satrini aynan ko'chiramiz (keraksizlari ham bo'lsa mayli)
COOKIE = "_ga=GA1.1.524611250.1760698145; __stripe_mid=c7023c3c-ce6c-4574-aab3-4cdb0c31d6980e275d; G_ENABLED_IDPS=google; XSRF-TOKEN=b4860bba-25f4-4de1-8870-8ed4beb483c6; __stripe_sid=bcf0a26b-532a-42d9-a741-45303cae6906a561d7; _ga_R5LGX7P1YR=GS2.1.s1768501951$o5$g1$t1768502308$j60$l0$h0"

async def main():
    headers = {
        "Accept": "application/json",
        "Accept-Language": "uz",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/uz/home",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "X-XSRF-TOKEN": XSRF,
        "device-type": "BROWSER",
        "Cookie": COOKIE,
    }

    payload = {
        "directions": {
            "forward": {
                "date": DATE,
                "depStationCode": DEP,
                "arvStationCode": ARV
            }
        }
    }

    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        r = await client.post(ENDPOINT, json=payload)
        print("STATUS:", r.status_code)

        try:
            data = r.json()
            print("JSON OK. Top-level keys:", list(data.keys()))
            print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
        except Exception:
            print("NOT JSON. First 3000 chars:")
            print(r.text[:3000])

asyncio.run(main())
