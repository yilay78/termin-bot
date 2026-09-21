"""
Berlin Termin Bot – Web Arayüzü Backend
========================================
service.berlin.de üzerinde randevu (Termin) izleyen ve boş slot bulunca
otomatik dolduran/rezerve eden FastAPI tabanlı web uygulaması.

Kurulum:
  pip install -r requirements.txt
  playwright install chromium

Çalıştırma:
  python app.py
  → http://localhost:8000 adresini tarayıcıda aç

Bu sürüm, masaüstü bot.py'deki kanıtlanmış portal mantığını içerir:
  - Bot koruması / "Unerwünschter Zugriff" challenge sayfasını aşma
  - "Wunschtage- und Zeiträume auswählen" (restriction) sayfasını geçme
  - Portalın kendi cooldown süresini okuyup tam o kadar bekleme
  - "Ich bin kein Bot" onay kutusunu çok yöntemli işaretleme
  - E-posta + Telegram bildirimleri
"""

import asyncio
import json
import os
import random
import re
import smtplib
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, Response
from pydantic import BaseModel

# ── Veri dosyaları ──────────────────────────────────────────
BASE_DIR      = Path(__file__).resolve().parent
DATA_FILE     = os.environ.get("DATA_FILE",     str(BASE_DIR / "kisiler.json"))
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", str(BASE_DIR / "ayarlar.json"))
LOG_FILE      = os.environ.get("LOG_FILE",      str(BASE_DIR / "termin_bot.log"))

# ── Global bot durumu ───────────────────────────────────────
bot_task:    Optional[asyncio.Task] = None
bot_running: bool = False
ws_clients:  list[WebSocket] = []

# En düşük tarama aralığı (saniye). Sunucuyu yormamak ve IP engelini
# önlemek için bu değerin altına inilmez.
MIN_INTERVAL = 30

BASE_URL = "https://service.berlin.de"


# ── Varsayılan ayarlar ──────────────────────────────────────
DEFAULT_SETTINGS = {
    # Takvim (tag.php) URL'si kullanılmalı; oturuma bağlı .../termin/time/<id>/
    # linkleri kısa sürede geçersiz olup portalı /termin/stop/ hatasına atar.
    # Varsayılan: Führungszeugnis (Dienstleistung 120926), Berlin geneli tüm
    # Bürgeramt'lar. Başka hizmet için anliegen[] ve dienstleisterlist'i değiştir.
    "termin_url"         : ("https://service.berlin.de/terminvereinbarung/termin/tag.php?"
                            "termin=1&anliegen[]=120926&dienstleisterlist="
                            "122217,122219,122227,122231,122238,122243,122254,122252,"
                            "122260,122262,122271,122273,122277,351612,351065,122280,"
                            "122282,122284,122291,122285,122296,150230,355948,122301,"
                            "122297,122294,122312,122314,122304,122311,122309,122281,"
                            "351358,122279,324414,122283,122276,122274"
                            "&herkunft=http%3A%2F%2Fservice.berlin.de%2Fdienstleistung%2F120926%2F"),
    "check_interval"     : 60,
    "manuel_tarih"       : "",
    "manuel_saat"        : "",
    "auto_submit"        : False,
    "fast_booking"       : True,   # slot bulununca formu hızlıca (anında) doldur
    # E-posta
    "email_enabled"      : False,
    "email_from"         : "",
    "email_pass"         : "",
    "email_to"           : "",
    # Telegram
    "telegram_enabled"   : False,
    "telegram_bot_token" : "",
    "telegram_chat_id"   : "",
}


# ── Yardımcı: Dosya okuma/yazma ─────────────────────────────

def load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_json(path: str, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_kisiler() -> list:
    return load_json(DATA_FILE, [])


def save_kisiler(kisiler: list):
    save_json(DATA_FILE, kisiler)


def load_settings() -> dict:
    s = load_json(SETTINGS_FILE, {})
    merged = {**DEFAULT_SETTINGS, **(s if isinstance(s, dict) else {})}
    # Aralığı güvenli tabana sabitle
    try:
        merged["check_interval"] = max(MIN_INTERVAL, int(merged.get("check_interval", 60)))
    except (TypeError, ValueError):
        merged["check_interval"] = 60
    return merged


def save_settings(settings: dict):
    # Bilinmeyen anahtarları at, eksikleri varsayılanla tamamla
    clean = {k: settings.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    try:
        clean["check_interval"] = max(MIN_INTERVAL, int(clean.get("check_interval", 60)))
    except (TypeError, ValueError):
        clean["check_interval"] = 60
    clean["auto_submit"]      = bool(clean.get("auto_submit"))
    clean["fast_booking"]     = bool(clean.get("fast_booking"))
    clean["email_enabled"]    = bool(clean.get("email_enabled"))
    clean["telegram_enabled"] = bool(clean.get("telegram_enabled"))
    save_json(SETTINGS_FILE, clean)


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
        if ws in ws_clients:
            ws_clients.remove(ws)


async def log_and_broadcast(text: str, level: str = "info"):
    """Hem log dosyasına yaz hem WebSocket'e gönder."""
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {text}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
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


def _abs_url(page, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urllib.parse.urljoin(page.url if page else BASE_URL, href)


# ── Bot koruması / challenge ─────────────────────────────────

async def is_blocked(page) -> bool:
    """Portalın 'Unerwünschter Zugriff' / Forbidden koruma sayfasında mıyız?"""
    try:
        title = await page.title()
    except Exception:
        return True
    if "Forbidden" in title or "Unerwünschter" in title:
        return True
    try:
        body = (await page.inner_text("body"))[:400]
        return "Unerwünschter Zugriff" in body
    except Exception:
        return True


async def wait_out_challenge(page, url: str, max_rounds: int = 10) -> bool:
    """Koruma sayfasındaysak birkaç kez bekleyip sayfayı yeniden yükler."""
    for _ in range(max_rounds):
        if not await is_blocked(page):
            return True
        await log_and_broadcast("🛡️ Bot koruması algılandı, bekleniyor…", "warn")
        await asyncio.sleep(5)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
    return not await is_blocked(page)


# ── 'Wunschtage- und Zeiträume auswählen' (restriction) sayfası ──

async def handle_restriction_page(page, timeout: float = 8) -> bool:
    """Portal 'Buchbare Tage anzeigen' butonlu ara sayfaya atarsa geçer."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            is_restriction = "time/restriction" in page.url
            if not is_restriction:
                body = (await page.inner_text("body"))[:800]
                is_restriction = "Wunschtage" in body or "Zeiträume auswählen" in body
        except Exception:
            return False

        if not is_restriction:
            return False

        for text in ("Buchbare Tage anzeigen", "Buchbare Tage", "Tage anzeigen"):
            try:
                loc = page.get_by_role("button", name=re.compile(text, re.I)).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    await log_and_broadcast("➡️ 'Buchbare Tage anzeigen' tıklandı.")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    await asyncio.sleep(3)
                    return True
            except Exception:
                continue

        try:
            loc = page.locator(
                "form[action*='restriction'] button[type=submit], "
                "button:has-text('Buchbare Tage'), "
                "input[type=submit][value*='Buchbare' i]"
            ).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                await log_and_broadcast("➡️ 'Buchbare Tage anzeigen' tıklandı (selector).")
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=60000)
                except Exception:
                    pass
                await asyncio.sleep(3)
                return True
        except Exception:
            pass

        await asyncio.sleep(0.5)
    return False


# ── Cooldown yönetimi ────────────────────────────────────────

async def get_cooldown_seconds(page) -> Optional[int]:
    """'Terminsuche erneut ausführbar in MM:SS' metnini okur."""
    try:
        text = await page.inner_text("body")
        m = re.search(r"Terminsuche erneut ausführbar in\s+(\d{1,2}):(\d{2})", text)
        if m:
            return int(m.group(1)) * 60 + int(m.group(2))
    except Exception:
        pass
    return None


async def click_repeat_search(page) -> bool:
    """'Terminsuche wiederholen' benzeri butonu bulup tıklar."""
    for text in ("Terminsuche wiederholen", "Terminsuche erneut",
                 "Erneut suchen", "Wiederholen", "Erneut"):
        try:
            loc = page.get_by_role("button", name=re.compile(text, re.I)).first
            if await loc.count() > 0 and await loc.is_visible() and await loc.is_enabled():
                await loc.click()
                return True
        except Exception:
            continue
    try:
        loc = page.locator(
            "button:has-text('wiederholen'), "
            "a:has-text('wiederholen'), "
            "input[type=submit][value*='wiederholen' i]"
        ).first
        if await loc.count() > 0 and await loc.is_visible() and await loc.is_enabled():
            await loc.click()
            return True
    except Exception:
        pass
    return False


# ── 'Ich bin kein Bot' onay kutusu ──────────────────────────

async def _force_check_js(handle) -> bool:
    try:
        await handle.evaluate("""
            el => {
                if (!el.checked) {
                    el.checked = true;
                    el.dispatchEvent(new Event('input',  {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('click',  {bubbles: true}));
                }
            }
        """)
        return True
    except Exception:
        return False


async def find_bot_checkbox(page):
    # 0) Portalın kendi ID'si
    for sel in ("input#payload_checkbox",
                "input[type=checkbox][id*='payload']"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue

    # 1) 'kein Bot' / 'Roboter' / Datenschutz / gelesen / AGB
    for pattern in (r"kein\s*Bot", r"Roboter", r"kein\s*Roboter",
                    r"Datenschutz", r"gelesen", r"AGB",
                    r"einverstanden", r"akzeptier"):
        try:
            loc = page.get_by_label(re.compile(pattern, re.I)).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue

    # 2) name/id ipuçları
    for sel in ("input[type=checkbox][name*='bot' i]",
                "input[type=checkbox][id*='bot' i]",
                "input[type=checkbox][name*='gelesen' i]",
                "input[type=checkbox][id*='datenschutz' i]"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue

    # 3) Tek checkbox + submit'li form
    try:
        forms = page.locator("form:visible")
        for i in range(await forms.count()):
            f = forms.nth(i)
            boxes = f.locator("input[type=checkbox]")
            btns  = f.locator("button[type=submit], input[type=submit]")
            if await boxes.count() == 1 and await btns.count() >= 1:
                cb = boxes.first
                if await cb.is_visible():
                    return cb
    except Exception:
        pass

    return None


async def _click_associated_label(page, cb) -> bool:
    try:
        cb_id = await cb.get_attribute("id")
        if cb_id:
            label = page.locator(f"label[for='{cb_id}']").first
            if await label.count() > 0 and await label.is_visible():
                await label.click()
                return True
        parent_label = cb.locator("xpath=ancestor::label[1]")
        if await parent_label.count() > 0 and await parent_label.is_visible():
            await parent_label.click()
            return True
    except Exception:
        pass
    return False


async def find_continue_button(page):
    for text in ("Termine anzeigen", "Termine suchen", "Weiter",
                 "Anzeigen", "Suchen", "Bestätigen"):
        try:
            loc = page.get_by_role("button", name=re.compile(text, re.I)).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue
    for sel in ("form button[type=submit]:visible",
                "form input[type=submit]:visible",
                "button[type=submit]:visible"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def accept_bot_checkbox(page, timeout: float = 20) -> bool:
    """'Ich bin kein Bot' onayını verir ve devam butonuna basar."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        cb = await find_bot_checkbox(page)
        if cb is None:
            await asyncio.sleep(0.5)
            continue

        already = False
        try:
            already = await cb.is_checked()
        except Exception:
            pass

        if not already:
            checked = False
            # 1) check(force=True)
            try:
                await cb.check(force=True, timeout=2000)
                checked = True
            except Exception:
                pass
            # 2) label / ebeveyn tıklama
            if not checked and await _click_associated_label(page, cb):
                await asyncio.sleep(0.4)
                try:
                    checked = await cb.is_checked()
                except Exception:
                    pass
            # 3) JavaScript ile zorla
            if not checked:
                try:
                    handle = await cb.element_handle()
                    if handle and await _force_check_js(handle):
                        await asyncio.sleep(0.4)
                        checked = await cb.is_checked()
                except Exception:
                    pass
            if not checked:
                await log_and_broadcast("[HATA] Onay kutusu işaretlenemedi.", "error")
                return False
            await log_and_broadcast("✅ 'Ich bin kein Bot' onayı verildi.")

        btn = await find_continue_button(page)
        if btn is None:
            try:
                await cb.evaluate("el => el.form && el.form.submit()")
                await asyncio.sleep(3)
                return True
            except Exception:
                return False
        try:
            await btn.click()
        except Exception:
            return False
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
            pass
        await asyncio.sleep(3)
        return True
    return False


# ── Slot bulma ───────────────────────────────────────────────

def extract_date(url: str) -> str:
    m = re.search(r"/(\d{8})/", url)
    if m:
        d = m.group(1)
        return f"{d[6:8]}.{d[4:6]}.{d[0:4]}"
    # Berlin gün sayfaları unix epoch kullanır: /termin/time/<epoch>/
    m = re.search(r"/time/(\d{9,11})/", url)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1))).strftime("%d.%m.%Y")
        except Exception:
            pass
    return "?"


# Saat sayfasındaki gezinme/başlık linkleri (gerçek randevu saati DEĞİL)
_NAV_TEXTS = (
    "zeitraum", "buchbare tage", "tage anzeigen", "zurück", "weiter",
    "abbrechen", "wiederhol", "erneut", "startseite", "nächst", "vorherig",
    "ändern", "mehr", "menü", "suche", "anzeigen", "auswähl", "impressum",
    "datenschutz", "barrierefrei",
)
# Gerçek bir randevu saati: metninde "09:30" / "9.30" gibi bir zaman geçer
_TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b")


async def is_stop_page(page) -> bool:
    """Portalın 'Es ist ein Fehler aufgetreten' / /termin/stop/ hata sayfası."""
    try:
        if "/termin/stop/" in page.url:
            return True
        body = (await page.inner_text("body"))[:600]
        return ("Zu ihrer Suche konnten keine Daten" in body
                or "Es ist ein Fehler aufgetreten" in body)
    except Exception:
        return False


async def _open_and_prepare(page, url: str) -> bool:
    """URL'yi açar, koruma ve restriction sayfalarını geçer."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        await log_and_broadcast(f"Sayfa açılamadı: {e}", "error")
        return False
    await human_delay(1.0, 2.5)
    if not await wait_out_challenge(page, url):
        await log_and_broadcast("Bot koruması aşılamadı.", "warn")
        return False
    await handle_restriction_page(page)
    if await is_stop_page(page):
        await log_and_broadcast(
            "⛔ Portal 'Es ist ein Fehler aufgetreten' (/termin/stop/) döndü. "
            "Termin-URL geçersiz/eski — Einstellungen'de 'tag.php' ile başlayan "
            "takvim URL'sini kullanın (…/termin/time/<id>/ değil).", "error")
        return False
    return True


async def find_earliest_slot(page, settings: dict) -> Optional[dict]:
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.mouse.wheel(0, random.randint(50, 200))
    await human_delay(0.5, 1.2)

    manuel_tarih = settings.get("manuel_tarih", "").strip()
    manuel_saat  = settings.get("manuel_saat",  "").strip()

    day_sel = "td.buchbar a, td.frei a, td.buchbar a[href*='/termin/time/']"

    # İstenen tarih varsa önce onu ara
    if manuel_tarih:
        for link in await page.locator(day_sel).all():
            text = await link.inner_text()
            if manuel_tarih in text:
                href = await link.get_attribute("href")
                await page.goto(_abs_url(page, href), wait_until="domcontentloaded")
                await asyncio.sleep(2)
                return await _earliest_time(page, manuel_saat)

    buchbar = page.locator(day_sel).first
    if not await buchbar.count():
        # Sonraki aya geçmeyi dene
        nxt = page.locator("a.next, .next-month, [title*='nächste']").first
        if await nxt.count():
            await human_click(page, "a.next, .next-month")
            await asyncio.sleep(2)
            buchbar = page.locator(day_sel).first

    if not await buchbar.count():
        return None

    href = await buchbar.get_attribute("href")
    text = (await buchbar.inner_text()).strip()
    await log_and_broadcast(f"📅 En erken gün: {text}")
    await human_delay(0.6, 1.4)
    await page.goto(_abs_url(page, href), wait_until="domcontentloaded")
    await asyncio.sleep(2)
    await handle_restriction_page(page)
    slot = await _earliest_time(page, manuel_saat)
    if slot is None:
        await log_and_broadcast(
            f"ℹ️ Gün ({text}) açıldı ama gerçek saat bulunamadı "
            "(slotlar dolmuş olabilir) — aramaya devam.", "warn")
    return slot


async def _earliest_time(page, preferred: str = "") -> Optional[dict]:
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    tarih = extract_date(page.url)
    raw = await page.locator(
        "td.buchbar a, td.frei a, .timeslot a, a[href*='/termin/time/'], "
        "a[href*='appointment'], li a[href*='/termin/']"
    ).all()

    # Sadece GERÇEK saat linklerini al: metninde SS:DD olmalı ve 'Zeitraum
    # ändern', 'Buchbare Tage' gibi gezinme linkleri elenmeli. Aksi halde
    # yanlış pozitif ('? Zeitraum ändern') oluşuyordu.
    slots = []
    seen = set()
    for link in raw:
        try:
            text = (await link.inner_text()).strip()
            href = await link.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue
        low = text.lower()
        if any(nav in low for nav in _NAV_TEXTS):
            continue
        if not _TIME_RE.search(text):
            continue
        url = _abs_url(page, href)
        if url in seen:
            continue
        seen.add(url)
        slots.append({"saat": text, "url": url})

    if not slots:
        return None
    if preferred:
        for s in slots:
            if preferred in s["saat"]:
                return {"tarih": tarih, **s}
    return {"tarih": tarih, **slots[0]}


# ── Form doldurma ────────────────────────────────────────────

async def _fill_field(page, selector, value, fast) -> bool:
    """Hızlı modda anında (.fill), normal modda insanımsı yazar."""
    if fast:
        await page.fill(selector, value, timeout=4000)
    else:
        await human_type(page, selector, value)
    return True


async def complete_booking(page, kisi: dict, slot: dict, settings: dict) -> bool:
    fast = bool(settings.get("fast_booking", True))
    t0   = time.monotonic()

    await page.goto(slot["url"], wait_until="domcontentloaded")
    # Hızlı modda sadece sayfanın oturması için kısa bekleme
    await (asyncio.sleep(0.5) if fast else human_delay(1.5, 3.0))

    # Saat/onay sayfası da onay kutusu isteyebilir
    await handle_restriction_page(page)

    parts   = kisi["isim"].split()
    soyisim = parts[-1] if parts else kisi["isim"]
    isim    = " ".join(parts[:-1]) or kisi["isim"]
    email   = settings.get("email_to", "") or "bot@example.com"

    for selectors, value in [
        (["#familyName", "#nachname", "input[name*='nachname' i]", "input[name*='name']"], soyisim),
        (["#firstName",  "#vorname",  "input[name*='vorname' i]"],                          isim),
        (["#birthday", "#geburtsdatum", "input[name*='birth' i]", "input[name*='geburt' i]"], kisi.get("dogum_tarihi", "")),
        (["#email", "input[type='email']", "input[name*='mail' i]"],                        email),
    ]:
        if not value:
            continue
        for sel in selectors:
            try:
                if await page.locator(sel).count():
                    await _fill_field(page, sel, value, fast)
                    await log_and_broadcast(f"   ✎ {sel.split('[')[0].replace('#','')} → {value}")
                    break
            except Exception:
                continue
        if not fast:
            await human_delay(0.3, 0.8)

    # Zorunlu onay kutuları (Datenschutz/AGB)
    for pattern in (r"Datenschutz", r"AGB", r"gelesen", r"einverstanden", r"akzeptier"):
        try:
            cb = page.get_by_label(re.compile(pattern, re.I)).first
            if await cb.count() > 0 and await cb.is_visible() and not await cb.is_checked():
                try:
                    await cb.check(force=True, timeout=1500)
                except Exception:
                    handle = await cb.element_handle()
                    if handle:
                        await _force_check_js(handle)
        except Exception:
            continue

    # CAPTCHA kontrolü
    captcha = False
    for s in ("iframe[src*='recaptcha']", ".g-recaptcha", "div[data-sitekey]"):
        try:
            if await page.locator(s).count() > 0:
                captcha = True
                break
        except Exception:
            continue
    if captcha:
        await log_and_broadcast("⚠️  CAPTCHA tespit edildi — tarayıcıda manuel çöz!", "warn")
        for _ in range(36):
            await asyncio.sleep(5)
            if any(k in page.url for k in ("/confirm", "/success", "/bestaetigung", "/danke")):
                return True
        return False

    if not settings.get("auto_submit"):
        await log_and_broadcast(
            f"ℹ️ auto_submit kapalı — form {time.monotonic()-t0:.1f} sn'de dolduruldu, "
            "gönderimi elle yap.", "warn")
        return False

    # Submit
    for sel in ("button[type='submit']", "button:has-text('Buchen')",
                "button:has-text('Termin buchen')", "button:has-text('Weiter')",
                "input[type='submit']"):
        try:
            if await page.locator(sel).count():
                if fast:
                    await page.click(sel, timeout=4000)
                else:
                    await human_delay(0.5, 1.2)
                    await human_click(page, sel)
                await asyncio.sleep(2)
                await log_and_broadcast(
                    f"   → Submit gönderildi ({time.monotonic()-t0:.1f} sn'de tamamlandı)")
                return True
        except Exception:
            continue
    return False


# ── Bildirimler ──────────────────────────────────────────────

async def send_email_async(kisi: dict, slot: dict, settings: dict, bekleyen: list):
    if not settings.get("email_enabled"):
        return
    if not (settings.get("email_from") and settings.get("email_pass") and settings.get("email_to")):
        await log_and_broadcast("E-posta etkin ama bilgiler eksik.", "warn")
        return
    bek_str = "\n".join(f"  {i+1}. {k['isim']} ({k.get('dogum_tarihi','')})"
                        for i, k in enumerate(bekleyen)) or "  —"
    body = f"""
✅ RANDEVU ALINDI
{'='*45}
Kişi         : {kisi['isim']}
Doğum Tarihi : {kisi.get('dogum_tarihi','')}
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

    def _send():
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as s:
            s.ehlo(); s.starttls()
            s.login(settings["email_from"], settings["email_pass"])
            s.sendmail(settings["email_from"], settings["email_to"], msg.as_string())

    try:
        await asyncio.to_thread(_send)
        await log_and_broadcast(f"📧 E-posta gönderildi → {settings['email_to']}")
    except Exception as e:
        await log_and_broadcast(f"E-posta hatası: {e}", "error")


async def send_telegram_async(settings: dict, text: str):
    if not settings.get("telegram_enabled"):
        return
    token = settings.get("telegram_bot_token", "").strip()
    chat  = settings.get("telegram_chat_id", "").strip()
    if not token or not chat:
        await log_and_broadcast("Telegram etkin ama token/chat_id eksik.", "warn")
        return

    def _send():
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)

    try:
        await asyncio.to_thread(_send)
        await log_and_broadcast("📨 Telegram bildirimi gönderildi.")
    except Exception as e:
        await log_and_broadcast(f"Telegram hatası: {e}", "error")


async def notify(settings: dict, text: str, kisi: dict = None,
                 slot: dict = None, bekleyen: list = None):
    await send_telegram_async(settings, text)
    if kisi and slot is not None:
        await send_email_async(kisi, slot, settings, bekleyen or [])


# ── Ana Bot Görevi ───────────────────────────────────────────

def _is_fatal_browser_error(page, e) -> bool:
    """Hata tarayıcı/sekme çökmesi mi (yeniden başlatma gerektirir) yoksa
    geçici bir sayfa hatası mı (yerinde tekrar denenir)?"""
    try:
        if page.is_closed():
            return True
    except Exception:
        return True
    msg = str(e).lower()
    return any(t in msg for t in (
        "target closed", "has been closed", "context was closed",
        "page crashed", "crashed", "connection closed", "browser closed",
        "target page, context or browser"))


async def _run_search_loop(page):
    """Bekleyen tüm kişileri sırayla işler. Normal bitişte/durdurulunca döner;
    tarayıcı çökerse exception yükseltir (dış döngü yeniden başlatır)."""
    aktif = [k for k in load_kisiler() if not k.get("tamamlandi")]
    if not aktif:
        await log_and_broadcast("Bekleyen kişi yok.", "warn")
        return

    for kisi in aktif:
        if not bot_running:
            return
        # Bu kişi arada (başka tur/oturumda) tamamlanmış olabilir
        fresh = {k["id"]: k for k in load_kisiler()}
        if fresh.get(kisi["id"], {}).get("tamamlandi"):
            continue

        await log_and_broadcast(f"▶ Sıradaki: {kisi['isim']} ({kisi.get('dogum_tarihi','')})")
        await broadcast({"type": "current_person", "isim": kisi["isim"]})
        attempt = 0

        while bot_running:
            attempt += 1
            # Ayarları her turda tazele — UI'daki değişiklik (URL, aralık,
            # auto_submit…) yeniden başlatmadan etkili olur.
            settings = load_settings()
            await log_and_broadcast(f"[#{attempt}] Kontrol ediliyor...")

            try:
                if not await _open_and_prepare(page, settings["termin_url"]):
                    await _sleep_interval(settings, extra="koruma")
                    continue

                # Onay kutusu (Ich bin kein Bot) çıkarsa geç
                await accept_bot_checkbox(page, timeout=8)

                slot = await find_earliest_slot(page, settings)

                if not slot:
                    cooldown = await get_cooldown_seconds(page)
                    if cooldown is not None:
                        await log_and_broadcast(
                            f"❌ Müsait slot yok. Portal cooldown: {cooldown} sn.")
                        await _sleep_interval(settings, cooldown=cooldown)
                        # Cooldown bitti — yerinde 'wiederholen' dene, olmazsa
                        # bir sonraki tur URL'yi yeniden açar.
                        if await click_repeat_search(page):
                            await log_and_broadcast("🔄 'Terminsuche wiederholen' tıklandı.")
                    else:
                        await log_and_broadcast("❌ Müsait slot yok.")
                        await _sleep_interval(settings)
                    continue

                await log_and_broadcast(
                    f"✅ Slot bulundu: {slot['tarih']} {slot['saat']}", "success")
                await broadcast({"type": "slot_found",
                                 "tarih": slot["tarih"], "saat": slot["saat"]})
                await notify(
                    settings,
                    f"🚨 Berlin randevusu bulundu! {kisi['isim']} → "
                    f"{slot['tarih']} {slot['saat']}",
                )

                ok = await complete_booking(page, kisi, slot, settings)
                if ok:
                    kisiler = load_kisiler()
                    for k in kisiler:
                        if k["id"] == kisi["id"]:
                            k["tamamlandi"]     = True
                            k["randevu_tarihi"] = slot["tarih"]
                            k["randevu_saati"]  = slot["saat"]
                    save_kisiler(kisiler)

                    bekleyen = [k for k in kisiler if not k.get("tamamlandi")]
                    await notify(
                        settings,
                        f"✅ Randevu alındı: {kisi['isim']} → "
                        f"{slot['tarih']} {slot['saat']}",
                        kisi=kisi, slot=slot, bekleyen=bekleyen,
                    )
                    await log_and_broadcast(
                        f"🎉 RANDEVU ALINDI! {kisi['isim']} → "
                        f"{slot['tarih']} {slot['saat']}", "success")
                    await broadcast({"type": "kisi_tamamlandi", "id": kisi["id"],
                                     "tarih": slot["tarih"], "saat": slot["saat"]})
                    break
                else:
                    await log_and_broadcast(
                        "Rezervasyon tamamlanamadı, tekrar denenecek.", "warn")
                    await _sleep_interval(settings)

            except Exception as e:
                if _is_fatal_browser_error(page, e):
                    raise   # dış döngü tarayıcıyı yeniden başlatsın
                await log_and_broadcast(f"Hata: {type(e).__name__}: {e}", "error")
                await _sleep_interval(settings)


async def bot_main():
    global bot_running
    from playwright.async_api import async_playwright

    if not [k for k in load_kisiler() if not k.get("tamamlandi")]:
        await log_and_broadcast("Tüm kişiler zaten tamamlandı.", "warn")
        bot_running = False
        await broadcast({"type": "status", "running": False})
        return

    settings = load_settings()
    # Masaüstünde HEADLESS=0 ile tarayıcı görünür açılır (CAPTCHA için).
    headless = os.environ.get("HEADLESS", "1").strip().lower() not in ("0", "false", "no")

    await log_and_broadcast(
        f"Bot başlatıldı — headless={headless} · auto_submit={settings.get('auto_submit')} "
        f"· fast_booking={settings.get('fast_booking')}")
    if settings.get("auto_submit") and not settings.get("email_to"):
        await log_and_broadcast(
            "⚠️ auto_submit açık ama alıcı E-posta boş — form e-posta isteyebilir. "
            "Einstellungen'de e-posta gir.", "warn")
    await broadcast({"type": "status", "running": True})

    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--window-size=1280,800",
    ]

    async with async_playwright() as pw:
        crash = 0
        # Dış döngü: tarayıcı çökerse yeniden başlatır (7/24 dayanıklılık).
        while bot_running:
            browser = None
            try:
                browser = await pw.chromium.launch(
                    headless=headless, args=launch_args,
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
                crash = 0
                await _run_search_loop(page)
                break   # normal bitiş: tümü tamam ya da durduruldu
            except Exception as e:
                if not bot_running:
                    break
                crash += 1
                await log_and_broadcast(
                    f"[TARAYICI HATASI] {type(e).__name__}: {e} — yeniden "
                    f"başlatılıyor ({crash}).", "error")
                backoff = min(60, 5 * crash)
                for _ in range(backoff * 4):
                    if not bot_running:
                        break
                    await asyncio.sleep(0.25)
            finally:
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass

    bot_running = False
    await broadcast({"type": "status", "running": False})
    await log_and_broadcast("Bot durduruldu.")


async def _sleep_interval(settings: dict, cooldown: int = None, extra: str = ""):
    """Bekleme süresi kadar bekler; bot_running False olursa anında çıkar.

    Portal bir cooldown bildirdiyse (Terminsuche erneut ausführbar in MM:SS),
    TAM o süreye uyulur — sadece 2 sn güvenlik tamponu eklenir, boşa fazladan
    beklenmez. Böylece süre biter bitmez tekrar sorulur (item 5). Cooldown
    yoksa güvenli taban (MIN_INTERVAL) ile hafif jitter kullanılır."""
    if cooldown is not None:
        wait = max(1, int(cooldown) + 2)          # portalın dediği an + 2 sn tampon
    else:
        wait = max(MIN_INTERVAL, int(settings.get("check_interval", 60)))
        wait += random.randint(0, 3)
    label = f" ({extra})" if extra else ""
    await log_and_broadcast(f"   → {wait} sn sonra tekrar{label}...")
    # 0.25 sn adımlarla bekle: durdurma emri ve süre bitişi anında yakalanır
    steps = max(1, int(wait / 0.25))
    for _ in range(steps):
        if not bot_running:
            return
        await asyncio.sleep(0.25)


# ── FastAPI ──────────────────────────────────────────────────

app = FastAPI(title="Berlin Termin Bot")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html bulunamadı</h1>")


def _serve_file(name: str, media_type: str):
    path = BASE_DIR / name
    if path.exists():
        return FileResponse(path, media_type=media_type)
    return Response(status_code=404)


@app.get("/manifest.json")
async def manifest():
    return _serve_file("manifest.json", "application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return _serve_file("sw.js", "application/javascript")


@app.get("/icon-192.png")
async def icon192():
    return _serve_file("icon-192.png", "image/png")


@app.get("/icon-512.png")
async def icon512():
    return _serve_file("icon-512.png", "image/png")


@app.get("/health")
async def health():
    return {"ok": True, "running": bot_running}


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
    if not kisi.isim.strip() or not kisi.dogum_tarihi.strip():
        return {"ok": False, "msg": "İsim ve doğum tarihi zorunlu"}
    kisiler = load_kisiler()
    new_id  = max((k["id"] for k in kisiler), default=0) + 1
    kisiler.append({
        "id"            : new_id,
        "isim"          : kisi.isim.strip(),
        "dogum_tarihi"  : kisi.dogum_tarihi.strip(),
        "not"           : kisi.not_.strip(),
        "tamamlandi"    : False,
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
            k["tamamlandi"]     = False
            k["randevu_tarihi"] = ""
            k["randevu_saati"]  = ""
    save_kisiler(kisiler)
    await broadcast({"type": "kisiler_updated"})
    return {"ok": True}


# -- Bot kontrolü --

def _start_bot_task():
    """Bot görevini başlatır ve beklenmedik bitişte durumu sıfırlar."""
    global bot_task, bot_running
    bot_running = True
    bot_task = asyncio.create_task(bot_main())

    def _done(t: asyncio.Task):
        global bot_running
        bot_running = False
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            exc = None
        if exc:
            print(f"[bot_main beklenmedik bitiş] {exc!r}", flush=True)

    bot_task.add_done_callback(_done)


@app.post("/api/bot/baslat")
async def baslat_bot():
    if bot_running:
        return {"ok": False, "msg": "Bot zaten çalışıyor"}
    if not [k for k in load_kisiler() if not k.get("tamamlandi")]:
        return {"ok": False, "msg": "Bekleyen kişi yok"}
    _start_bot_task()
    return {"ok": True}


@app.on_event("startup")
async def _maybe_autostart():
    """AUTOSTART=1 ise (7/24 sunucu/Railway) bot, sunucu açılışında
    otomatik başlar — kimsenin 'Başlat'a basmasına gerek kalmaz."""
    if os.environ.get("AUTOSTART", "").strip().lower() not in ("1", "true", "yes"):
        return
    if bot_running:
        return
    if not [k for k in load_kisiler() if not k.get("tamamlandi")]:
        print("[AUTOSTART] Bekleyen kişi yok — bot başlatılmadı.", flush=True)
        return
    print("[AUTOSTART] Bot otomatik başlatılıyor…", flush=True)
    _start_bot_task()


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

SECRET_KEYS = ("email_pass", "telegram_bot_token")
MASK = "********"


@app.get("/api/ayarlar")
async def get_ayarlar():
    s = dict(load_settings())
    # Sırları istemciye maskele
    for key in SECRET_KEYS:
        if s.get(key):
            s[key] = MASK
    return s


@app.post("/api/ayarlar")
async def save_ayarlar(data: dict):
    current = load_settings()
    # Maskeli sır geldiyse mevcut değeri koru
    for key in SECRET_KEYS:
        if data.get(key) in (MASK, None, ""):
            data[key] = current.get(key, "")
    merged = {**current, **data}
    save_settings(merged)
    return {"ok": True}


# -- Log --

@app.get("/api/log")
async def get_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
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
    except Exception:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ── Başlat ───────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n  Berlin Termin Bot çalışıyor → port {port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
