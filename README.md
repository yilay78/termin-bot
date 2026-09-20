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
    okuyup tam o kadar bekler, ardından `Terminsuche wiederholen`e basar
  - `Ich bin kein Bot` onay kutusunu çok yöntemli (check / label / JS / click)
    işaretler
- 👥 **Çok kişilik kuyruk** — birden fazla kişiyi sırayla işler, her biri için
  en erken günü/saati bulur
- 🕐 **Wunschdatum / Wunschuhrzeit** — belirli gün veya saat tercih edilebilir
  (boş bırakılırsa en erken)
- ✍️ **Otomatik form doldurma** + isteğe bağlı **otomatik gönderim**
  (`auto_submit`)
- 🔔 **Bildirimler:** E-posta (Gmail) **ve/veya** Telegram
- 📊 Canlı log konsolu (WebSocket) + kişi istatistikleri
- 📱 PWA — telefonda ana ekrana eklenebilir

---

## Hızlı Başlangıç (yerel)

```bash
pip install -r requirements.txt
playwright install chromium
python app.py
# → http://localhost:8000
```

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
| `termin_url` | İzlenecek `service.berlin.de` randevu URL'si | Zulassung örneği |
| `check_interval` | Tarama aralığı (sn, min **30**) | `60` |
| `manuel_tarih` | İstenen gün (`GG.AA.YYYY`), boş = en erken | `""` |
| `manuel_saat` | İstenen saat (`SS:DD`), boş = en erken | `""` |
| `auto_submit` | Formu otomatik gönder (kapalıysa sadece doldurur) | `false` |
| `email_enabled` | E-posta bildirimini aç | `false` |
| `email_from` / `email_pass` / `email_to` | Gmail gönderici / **uygulama şifresi** / alıcı | `""` |
| `telegram_enabled` | Telegram bildirimini aç | `false` |
| `telegram_bot_token` / `telegram_chat_id` | @BotFather token'ı / hedef chat | `""` |

> Sırlar (e-posta şifresi, Telegram token) API üzerinden `********` olarak
> maskelenir; boş/maskeli kaydedersen mevcut değer korunur.

Dosya konumları ortam değişkenleriyle değiştirilebilir:
`DATA_FILE`, `SETTINGS_FILE`, `LOG_FILE`, `PORT`.

---

## Docker / Railway ile dağıtım

Depoda hazır `Dockerfile` ve `railway.json` var:

```bash
docker build -t termin-bot .
docker run -p 8000:8000 termin-bot
```

Railway'de repoyu bağla → `DOCKERFILE` builder otomatik algılanır.
`PORT` ortam değişkenini Railway sağlar.

> Bulut ortamında tarayıcı **headless** çalışır; `Ich bin kein Bot` onayı
> otomatik verilir, ancak gerçek bir CAPTCHA çıkarsa manuel çözüm gerekir
> (log'da uyarı görünür).

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
