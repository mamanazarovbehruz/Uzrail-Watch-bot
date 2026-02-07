FROM python:3.12-slim-bookworm

WORKDIR /app

# 🔴 Playwright/Chromium uchun kerakli system kutubxonalar
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libnss3 libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 \
    libgbm1 libxcb1 libxkbcommon0 \
    libasound2 \
    libexpat1 \
    ca-certificates \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browser (Chromium)
RUN python -m playwright install chromium

# App fayllari
COPY . .

# Start
CMD ["python", "bot.py"]
