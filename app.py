"""
Berlin Termin Bot – Web Arayüzü Backend
========================================
Kurulum:
  pip install fastapi uvicorn playwright websockets
  playwright install chromium

Çalıştırma:
  python app.py
  → http://localhost:8000 adresini tarayıcıda aç
"""

import asyncio
import json
import os
import random
import re
import smtplib
import time
from contextlib import asynccontextmanager
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Veri dosyaları ──────────────────────────────────────────
DATA_FILE     = "kisiler.json"
SETTINGS_FILE = "ayarlar.json"
LOG_FILE      = "termin_bot.log"

# ── Global bot durumu ───────────────────────────────────────
bot_task:    Optional[asyncio.Task] = None
bot_running: bool = False
ws_clients:  list[WebSocket] = []


# ── Varsayılan ayarlar ──────────────────────────────────────
DEFAULT_SETTINGS = {
    "termin_url"   : "https://service.berlin.de/terminvereinbarung/termin/time/1323615/",
    "check_interval": 60,
    "email_enabled": False,
    "email_from"   : "",
    "email_pass"   : "",
    "email_to"     : "",
    "manuel_tarih" : "",
    "manuel_saat"  : "",
}


# ── Yardımcı: Dosya okuma/yazma ─────────────────────────────

def load_json(path: str, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_kisiler() -> list:
    return load_json(DATA_FILE, [])


def save_kisiler(kisiler: list):
    save_json(DATA_FILE, kisiler)


def load_settings() -> dict:
    s = load_json(SETTINGS_FILE, {})
    return {**DEFAULT_SETTINGS, **s}


def save_settings(settings: dict):
    save_json(SETTINGS_FILE, settings)


# ── WebSocket yayını ─────────────────────────────────────────

async def broadcast(msg: dict):
    """Tüm bağlı WebSocket istemcilerine mesaj gönder."""
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


async def log_and_broadcast(text: str, level: str = "info"):
    """Hem log dosyasına yaz hem WebSocket'e gönder."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {text}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    await broadcast({"type": "log", "level": level, "text": line})


# ── Stealth tarayıcı ─────────────────────────────────────────

STEALTH_JS = """() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    Object.defineProperty(navigator, 'plugins', { get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
    ]});
    Object.defineProperty(navigator, 'languages', { get: () => ['de-DE','de','en-US','en'] });
}"""

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
]


async def human_delay(a=0.8, b=2.0):
    await asyncio.sleep(random.uniform(a, b))


async def human_type(page, selector, text):
    await page.click(selector)
    await human_delay(0.2, 0.5)
    for ch in text:
        await page.type(selector, ch, delay=random.randint(60, 180))
    await human_delay(0.2, 0.5)


async def human_click(page, selector):
    el = page.locator(selector).first
    if not await el.count():
        return False
    box = await el.bounding_box()
    if not box:
        return False
    tx = box["x"] + box["width"]  * random.uniform(0.3, 0.7)
    ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    for i in range(random.randint(6, 12)):
        p = (i + 1) / 10
        await page.mouse.move(200 + (tx - 200) * p + random.uniform(-6, 6),
                              200 + (ty - 200) * p + random.uniform(-6, 6))
        await asyncio.sleep(random.uniform(0.01, 0.03))
    await page.mouse.click(tx, ty)
    await human_delay(0.3, 0.8)
    return True


# ── Slot bulma ───────────────────────────────────────────────

def extract_date(url: str) -> str:
    m = re.search(r"/(\d{8})/", url)
    if m:
        d = m.group(1)
        return f"{d[6:8]}.{d[4:6]}.{d[0:4]}"
    return "?"


async def find_earliest_slot(page, settings: dict):
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.mouse.wheel(0, random.randint(50, 200))
    await human_delay(0.5, 1.2)

    manuel_tarih = settings.get("manuel_tarih", "").strip()
    manuel_saat  = settings.get("manuel_saat",  "").strip()

    if manuel_tarih:
        for link in await page.locator("td.buchbar a, td.frei a").all():
            text = await link.inner_text()
            if manuel_tarih in text:
                href = await link.get_attribute("href")
                day_url = href if href.startswith("http") else "https://service.berlin.de" + href
                await page.goto(day_url)
                await asyncio.sleep(2)
                return await _earliest_time(page, manuel_saat)

    buchbar = page.locator("td.buchbar a, td.frei a").first
    if not await buchbar.count():
        nxt = page.locator("a.next, .next-month, [title*='nächste']").first
        if await nxt.count():
            await human_click(page, "a.next, .next-month")
            await asyncio.sleep(2)
            buchbar = page.locator("td.buchbar a, td.frei a").first

    if not await buchbar.count():
        return None

    href = await buchbar.get_attribute("href")
    text = (await buchbar.inner_text()).strip()
    day_url = href if href.startswith("http") else "https://service.berlin.de" + href
    await log_and_broadcast(f"📅 En erken gün: {text}")
    await human_delay(0.6, 1.4)
    await page.goto(day_url)
    await asyncio.sleep(2)
    return await _earliest_time(page, manuel_saat)


async def _earliest_time(page, preferred: str = "") -> dict | None:
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    tarih = extract_date(page.url)
    links = await page.locator("td.buchbar a, td.frei a, .timeslot a, a[href*='/time/']").all()
    if not links:
        return None
    if preferred:
        for link in links:
            t = await link.inner_text()
            if preferred in t:
                href = await link.get_attribute("href")
                url  = href if href.startswith("http") else "https://service.berlin.de" + href
                return {"tarih": tarih, "saat": t.strip(), "url": url}
    href = await links[0].get_attribute("href")
    saat = await links[0].inner_text()
    url  = href if href.startswith("http") else "https://service.berlin.de" + href
    return {"tarih": tarih, "saat": saat.strip(), "url": url}


# ── Form doldurma ────────────────────────────────────────────

async def complete_booking(page, kisi: dict, slot: dict, settings: dict) -> bool:
    await page.goto(slot["url"])
    await human_delay(1.5, 3.0)

    soyisim = kisi["isim"].split()[-1]
    isim    = " ".join(kisi["isim"].split()[:-1]) or kisi["isim"]
    email   = settings.get("email_to", "") or "bot@example.com"

    for selectors, value in [
        (["#familyName", "#nachname", "input[name*='name']"],      soyisim),
        (["#firstName",  "#vorname",  "input[name*='vorname']"],   isim),
        (["#birthday", "#geburtsdatum", "input[name*='birth']"],   kisi["dogum_tarihi"]),
        (["#email", "input[type='email']"],                         email),
    ]:
        for sel in selectors:
            if await page.locator(sel).count():
                await human_type(page, sel, value)
                await log_and_broadcast(f"   ✎ {sel.split('[')[0].replace('#','')} → {value}")
                break
        await human_delay(0.3, 0.8)

    # CAPTCHA kontrolü
    captcha = any([
        await page.locator(s).count() > 0
        for s in ["iframe[src*='recaptcha']", ".g-recaptcha", "div[data-sitekey]"]
    ])
    if captcha:
        await log_and_broadcast("⚠️  CAPTCHA tespit edildi — tarayıcıda manuel çöz!", "warn")
        for _ in range(36):
            await asyncio.sleep(5)
            if any(k in page.url for k in ["/confirm", "/success", "/bestaetigung", "/danke"]):
                return True
        return False

    # Submit
    for sel in ["button[type='submit']", "button:has-text('Buchen')",
                "button:has-text('Weiter')", "input[type='submit']"]:
        if await page.locator(sel).count():
            await human_delay(0.5, 1.2)
            await human_click(page, sel)
            await asyncio.sleep(3)
            await log_and_broadcast(f"   → Submit gönderildi")
            return True
    return False


# ── E-posta ──────────────────────────────────────────────────

async def send_email_async(kisi: dict, slot: dict, settings: dict, bekleyen: list):
    if not settings.get("email_enabled"):
        return
    bek_str = "\n".join(f"  {i+1}. {k['isim']} ({k['dogum_tarihi']})"
                         for i, k in enumerate(bekleyen)) or "  —"
    body = f"""
✅ RANDEVU ALINDI
{'='*45}
Kişi         : {kisi['isim']}
Doğum Tarihi : {kisi['dogum_tarihi']}
Not          : {kisi.get('not') or '—'}
Tarih        : {slot['tarih']}
Saat         : {slot['saat']}
{'='*45}
Kalan kişiler ({len(bekleyen)} kişi):
{bek_str}
"""
    msg = MIMEMultipart()
    msg["Subject"] = f"✅ Randevu Alındı → {kisi['isim']} | {slot['tarih']} {slot['saat']}"
    msg["From"]    = settings["email_from"]
    msg["To"]      = settings["email_to"]
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as s:
            s.ehlo(); s.starttls()
            s.login(settings["email_from"], settings["email_pass"])
            s.sendmail(settings["email_from"], settings["email_to"], msg.as_string())
        await log_and_broadcast(f"📧 E-posta gönderildi → {settings['email_to']}")
    except Exception as e:
        await log_and_broadcast(f"E-posta hatası: {e}", "error")


# ── Ana Bot Görevi ───────────────────────────────────────────

async def bot_main():
    global bot_running
    from playwright.async_api import async_playwright

    settings = load_settings()
    kisiler  = load_kisiler()
    aktif    = [k for k in kisiler if not k.get("tamamlandi")]

    if not aktif:
        await log_and_broadcast("Tüm kişiler zaten tamamlandı.", "warn")
        bot_running = False
        await broadcast({"type": "status", "running": False})
        return

    await log_and_broadcast(f"Bot başlatıldı — {len(aktif)} kişi bekliyor")
    await broadcast({"type": "status", "running": True})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,   # Bulut sunucusunda headless zorunlu
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--window-size=1280,800",
            ],
            ignore_default_args=["--enable-automation"],
        )
        ctx = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()

        for kisi in aktif:
            if not bot_running:
                break

            await log_and_broadcast(f"▶ Sıradaki: {kisi['isim']} ({kisi['dogum_tarihi']})")
            await broadcast({"type": "current_person", "isim": kisi["isim"]})
            attempt = 0

            while bot_running:
                attempt += 1
                await log_and_broadcast(f"[#{attempt}] Kontrol ediliyor...")

                try:
                    await page.goto(settings["termin_url"], timeout=20000)
                    await human_delay(1.0, 2.5)

                    slot = await find_earliest_slot(page, settings)

                    if not slot:
                        await log_and_broadcast("❌ Müsait slot yok.")
                    else:
                        await log_and_broadcast(f"✅ Slot bulundu: {slot['tarih']} {slot['saat']}", "success")
                        await broadcast({"type": "slot_found", "tarih": slot["tarih"], "saat": slot["saat"]})

                        ok = await complete_booking(page, kisi, slot, settings)
                        if ok:
                            # Kişiyi tamamlandı olarak işaretle
                            kisiler = load_kisiler()
                            for k in kisiler:
                                if k["id"] == kisi["id"]:
                                    k["tamamlandi"] = True
                                    k["randevu_tarihi"] = slot["tarih"]
                                    k["randevu_saati"]  = slot["saat"]
                            save_kisiler(kisiler)

                            bekleyen = [k for k in kisiler if not k.get("tamamlandi")]
                            await send_email_async(kisi, slot, settings, bekleyen)
                            await log_and_broadcast(f"🎉 RANDEVU ALINDI! {kisi['isim']} → {slot['tarih']} {slot['saat']}", "success")
                            await broadcast({"type": "kisi_tamamlandi", "id": kisi["id"],
                                            "tarih": slot["tarih"], "saat": slot["saat"]})
                            break

                except Exception as e:
                    await log_and_broadcast(f"Hata: {e}", "error")

                interval = int(settings.get("check_interval", 60))
                await log_and_broadcast(f"   → {interval}sn sonra tekrar...")
                for _ in range(interval):
                    if not bot_running:
                        break
                    await asyncio.sleep(1)

        await browser.close()

    bot_running = False
    await broadcast({"type": "status", "running": False})
    await log_and_broadcast("Bot durduruldu.")


# ── FastAPI ──────────────────────────────────────────────────

app = FastAPI(title="Berlin Termin Bot")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path("index.html")
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html bulunamadı</h1>")

@app.get("/manifest.json")
async def manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker():
    return FileResponse("sw.js", media_type="application/javascript")

@app.get("/icon-192.png")
async def icon192():
    return FileResponse("icon-192.png", media_type="image/png")

@app.get("/icon-512.png")
async def icon512():
    return FileResponse("icon-512.png", media_type="image/png")


# -- Kişiler --

class KisiModel(BaseModel):
    isim: str
    dogum_tarihi: str
    not_: str = ""


@app.get("/api/kisiler")
async def get_kisiler():
    return load_kisiler()


@app.post("/api/kisiler")
async def add_kisi(kisi: KisiModel):
    kisiler = load_kisiler()
    new_id  = max((k["id"] for k in kisiler), default=0) + 1
    kisiler.append({
        "id"           : new_id,
        "isim"         : kisi.isim,
        "dogum_tarihi" : kisi.dogum_tarihi,
        "not"          : kisi.not_,
        "tamamlandi"   : False,
        "randevu_tarihi": "",
        "randevu_saati" : "",
    })
    save_kisiler(kisiler)
    await broadcast({"type": "kisiler_updated"})
    return {"ok": True, "id": new_id}


@app.delete("/api/kisiler/{kid}")
async def delete_kisi(kid: int):
    kisiler = [k for k in load_kisiler() if k["id"] != kid]
    save_kisiler(kisiler)
    await broadcast({"type": "kisiler_updated"})
    return {"ok": True}


@app.put("/api/kisiler/{kid}/sifirla")
async def sifirla_kisi(kid: int):
    kisiler = load_kisiler()
    for k in kisiler:
        if k["id"] == kid:
            k["tamamlandi"]    = False
            k["randevu_tarihi"] = ""
            k["randevu_saati"]  = ""
    save_kisiler(kisiler)
    await broadcast({"type": "kisiler_updated"})
    return {"ok": True}


# -- Bot kontrolü --

@app.post("/api/bot/baslat")
async def baslat_bot():
    global bot_task, bot_running
    if bot_running:
        return {"ok": False, "msg": "Bot zaten çalışıyor"}
    bot_running = True
    bot_task = asyncio.create_task(bot_main())
    return {"ok": True}


@app.post("/api/bot/durdur")
async def durdur_bot():
    global bot_running
    bot_running = False
    await broadcast({"type": "status", "running": False})
    return {"ok": True}


@app.get("/api/bot/durum")
async def bot_durum():
    return {"running": bot_running}


# -- Ayarlar --

@app.get("/api/ayarlar")
async def get_ayarlar():
    return load_settings()


@app.post("/api/ayarlar")
async def save_ayarlar(data: dict):
    save_settings(data)
    return {"ok": True}


# -- Log --

@app.get("/api/log")
async def get_log():
    if os.path.exists(LOG_FILE):
        lines = open(LOG_FILE, encoding="utf-8").readlines()
        return {"lines": lines[-200:]}
    return {"lines": []}


@app.delete("/api/log")
async def clear_log():
    open(LOG_FILE, "w").close()
    return {"ok": True}


# -- WebSocket --

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    await websocket.send_json({"type": "status", "running": bot_running})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ── Başlat ───────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    print(f"\n  Berlin Termin Bot çalışıyor → port {port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


# Railway için port override
import os as _os
_PORT = int(_os.environ.get("PORT", 8000))
