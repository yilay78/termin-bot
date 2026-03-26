FROM python:3.11-slim

# Chromium için sistem bağımlılıkları
RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
    libgtk-3-0 libx11-xcb1 libxcb-dri3-0 \
    fonts-liberation fonts-dejavu-core \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python bağımlılıkları
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium kur
RUN playwright install chromium

# Uygulama dosyaları
COPY app.py .
COPY index.html .

# Port
EXPOSE 8000

CMD ["python", "app.py"]
