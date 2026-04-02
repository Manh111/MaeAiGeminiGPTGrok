# ── Base image: Python 3.11 slim ──────────────────────────────────────────────
FROM python:3.11-slim

# ── System dependencies cần cho Chromium headless ─────────────────────────────
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    # Chromium deps
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libpangocairo-1.0-0 libgtk-3-0 \
    libx11-xcb1 libxcb-dri3-0 fonts-liberation \
    xvfb dbus-x11 \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ──────────────────────────────────────────────────────────
WORKDIR /app

# ── Cài Python packages ────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Cài Playwright + Chromium browser ─────────────────────────────────────────
RUN playwright install chromium \
    && playwright install-deps chromium

# ── Copy source code ───────────────────────────────────────────────────────────
COPY . .

# ── Tạo thư mục cần thiết ─────────────────────────────────────────────────────
RUN mkdir -p config/cookies logs data

# ── Port mặc định Railway dùng ────────────────────────────────────────────────
ENV PORT=8000
EXPOSE 8000

# ── Chạy web server (nhận webhook + quản lý scraper) ──────────────────────────
CMD ["python", "server.py"]
