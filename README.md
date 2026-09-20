# Berlin Termin Bot

`service.berlin.de` üzerinde randevu (Termin) izleyen, boş slot bulunca formu
otomatik dolduran ve isteğe bağlı olarak rezerve eden **web tabanlı** bir bot.
FastAPI backend + tek sayfalık PWA arayüz. Railway / Docker üzerinde 7/24
çalışacak şekilde tasarlandı.

> ⚠️ **Sorumlu kullanım:** Bu araç, kendi kişisel randevunu almanı
> kolaylaştırmak içindir. Tarama aralığı en az **30 saniyeye** sabitlenmiştir
> ve portalın kendi bekleme süresine (cooldown) uyar — sunucuyu yormamak ve IP
> engellenmemek için bu değerleri düşürme. Randevu ticareti / toplu talep için
> kullanma.

---

## Özellikler

- 🔎 **Sağlam portal akışı** (masaüstü `bot.py`'den taşındı):
  - Bot koruması / `Unerwünschter Zugriff` challenge sayfasını aşar
  - `Wunschtage- und Zeiträume auswählen` ara sayfasını geçer
    (`Buchbare Tage anzeigen`)
  - Portalın `Terminsuche erneut ausführbar in MM:SS` cooldown süresini
    okuyup **tam o an** tekrar sorar (boşa bekleme yok, +2 sn güvenlik tamponu)
  - `Ich bin kein Bot` onay kutusunu çok yöntemli (check / label / JS / click)
    işaretler
- ⚡ **Blitz-Buchung (`fast_booking`)** — slot bulununca formu char-by-char
  değil **anında** doldurup gönderir; kritik saniyeleri kısaltır
- 🔁 **7/24 dayanıklılık** — tarayıcı çökerse otomatik yeniden başlar;
  `AUTOSTART=1` ile sunucu açılışında bot kendiliğinden çalışır
- 👥 **Çok kişilik kuyruk** — birden fazla kişiyi sırayla işler, her biri için
  en erken günü/saati bulur
- 🕐 **Wunschdatum / Wunschuhrzeit** — belirli gün veya saat tercih edilebilir
  (boş bırakılırsa en erken)
- ✍️ **Otomatik form doldurma** + isteğe bağlı **otomatik gönderim**
  (`auto_submit`)
- 🔔 **Bildirimler:** E-posta (Gmail) **ve/veya** Telegram
- ⚙️ Ayarlar her turda tazelenir — UI'da değişiklik yeniden başlatma gerektirmez
- 📊 Canlı log konsolu (WebSocket) + kişi istatistikleri
- 📱 PWA — telefonda ana ekrana eklenebilir

---

## Hızlı Başlangıç (yerel)

**En kolay yol (Windows):** `BASLAT.bat` dosyasına çift tıkla. İlk çalıştırmada
eksik paketleri ve Chromium'u otomatik kurar, sonra tarayıcıda arayüzü açar.
Bu başlatıcı botu **görünür tarayıcıyla** (`HEADLESS=0`) çalıştırır; böylece bir
CAPTCHA çıkarsa elle çözebilirsin.

**Elle (her platform):**

```bash
pip install -r requirements.txt
playwright install chromium
python app.py
# → http://localhost:8000
```

> Tarayıcının görünür mü yoksa arka planda mı açılacağını `HEADLESS` ortam
> değişkeni belirler: `HEADLESS=0` görünür (masaüstü için önerilir),
> `HEADLESS=1` (varsayılan) arka plan / sunucu.

Ardından tarayıcıda:

1. **+ Hinzufügen** sekmesinden kişileri ekle (ad-soyad + doğum tarihi).
2. **⚙ Einstellungen**'de Termin-URL'yi ve tarama aralığını gir; istersen
   e-posta/Telegram bildirimini ve `auto_submit`'i aç.
3. **▶ Bot starten**'a bas. Log konsolundan canlı takip et.

---

## Ayarlar

Ayarlar arayüzden kaydedilir ve `ayarlar.json` dosyasında tutulur.

| Anahtar | Açıklama | Varsayılan |
|---|---|---|
| `termin_url` | İzlenecek `service.berlin.de` **takvim** URL'si — `tag.php` ile başlamalı (oturuma bağlı `…/termin/time/<id>/` linkleri geçersiz olup `/termin/stop/` hatasına yol açar) | `tag.php` örneği |
| `check_interval` | Tarama aralığı (sn, min **30**) | `60` |
| `manuel_tarih` | İstenen gün (`GG.AA.YYYY`), boş = en erken | `""` |
| `manuel_saat` | İstenen saat (`SS:DD`), boş = en erken | `""` |
| `auto_submit` | Formu otomatik gönder (kapalıysa sadece doldurur) | `false` |
| `fast_booking` | Blitz-Buchung: slot bulununca formu anında doldur/gönder | `true` |
| `email_enabled` | E-posta bildirimini aç | `false` |
| `email_from` / `email_pass` / `email_to` | Gmail gönderici / **uygulama şifresi** / alıcı | `""` |
| `telegram_enabled` | Telegram bildirimini aç | `false` |
| `telegram_bot_token` / `telegram_chat_id` | @BotFather token'ı / hedef chat | `""` |

> Sırlar (e-posta şifresi, Telegram token) API üzerinden `********` olarak
> maskelenir; boş/maskeli kaydedersen mevcut değer korunur.

### Ortam değişkenleri

| Değişken | Açıklama | Varsayılan |
|---|---|---|
| `PORT` | Sunucu portu | `8000` |
| `HEADLESS` | `0` = tarayıcı görünür (masaüstü, CAPTCHA için), `1` = arka plan | `1` |
| `AUTOSTART` | `1` = sunucu açılışında bot otomatik başlar (7/24 için) | (kapalı) |
| `DATA_FILE` / `SETTINGS_FILE` / `LOG_FILE` | Veri/ayar/log dosya yolları | proje klasörü |

---

## Docker / Railway ile 7/24 dağıtım

Depoda hazır `Dockerfile` ve `railway.json` var:

```bash
docker build -t termin-bot .
docker run -p 8000:8000 -e AUTOSTART=1 termin-bot
```

Railway'de repoyu bağla → `DOCKERFILE` builder otomatik algılanır.
`PORT`'u Railway sağlar. **7/24 çalışsın** istiyorsan:

1. Railway → **Variables** kısmına `AUTOSTART=1` ekle → bot, konteyner her
   açıldığında kimse "Başlat"a basmadan çalışmaya başlar.
2. Tarayıcı çökerse bot kendini **otomatik yeniden başlatır**; sağlayıcının
   `restartPolicy`'si de tüm süreç çökerse konteyneri yeniden ayağa kaldırır.

> **⚠️ Kalıcılık (önemli):** Railway dosya sistemi geçicidir — her yeni
> dağıtımda `kisiler.json` / `ayarlar.json` **sıfırlanır**. 7/24 için ya bir
> **kalıcı disk (Volume)** bağlayıp `DATA_FILE`/`SETTINGS_FILE` yollarını oraya
> ver, ya da ilk açılıştan sonra kişileri/ayarları arayüzden bir kez daha gir.

> Bulut ortamında tarayıcı **headless** çalışır; `Ich bin kein Bot` onayı
> otomatik verilir, ancak gerçek bir CAPTCHA çıkarsa çözülemez — bu durumda
> `auto_submit` kapalı tutup Telegram bildirimiyle elle bitirmek daha güvenli.

---

## Dosya yapısı

```
app.py            FastAPI backend + Playwright bot motoru
index.html        Tek sayfalık web arayüz (PWA)
manifest.json     PWA manifest
sw.js             Service worker (offline + push)
Dockerfile        Chromium'lu Python imajı
railway.json      Railway dağıtım ayarı
requirements.txt  Python bağımlılıkları
```

`kisiler.json`, `ayarlar.json`, `termin_bot.log` çalışma zamanında oluşur ve
`.gitignore` ile hariç tutulur.
