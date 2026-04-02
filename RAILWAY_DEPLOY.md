# 🚂 Deploy lên Railway

## Tổng quan kiến trúc

```
[Web của bạn] ──POST /run──▶ [Railway Server] ──headless──▶ [Gemini/ChatGPT/Grok]
                                     │
                                     └──POST──▶ [WEBHOOK_URL của bạn]
```

---

## BƯỚC 1: Lấy cookies (chạy LOCAL 1 lần)

Cài Playwright local nếu chưa có:
```bash
pip install playwright
playwright install chromium
```

Chạy script lấy cookies:
```bash
python get_cookies.py                    # Lấy tất cả (gemini, chatgpt, grok)
python get_cookies.py gemini chatgpt     # Hoặc chỉ một số
```

> Browser thật mở ra → bạn đăng nhập → nhấn Enter → script in ra env var để copy.

---

## BƯỚC 2: Deploy lên Railway

### Cách 1: Dùng GitHub (khuyên dùng)

1. Push code lên GitHub repo
2. Vào [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Chọn repo → Railway tự detect `Dockerfile`

### Cách 2: Railway CLI

```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

---

## BƯỚC 3: Cấu hình Environment Variables trên Railway

Vào **Railway Dashboard → Project → Variables**, thêm:

| Variable | Giá trị | Bắt buộc |
|---|---|---|
| `API_SECRET` | mật-khẩu-tùy-chọn | ✅ |
| `WEBHOOK_URL` | https://your-site.com/api/ai-results | ✅ |
| `SELECTED_AIS` | gemini,chatgpt,grok | ✅ |
| `PROMPT_URL` | https://your-site.com/api/get-prompts | Nếu dùng URL |
| `GEMINI_COOKIES` | (paste từ get_cookies.py) | Nếu dùng Gemini |
| `CHATGPT_COOKIES` | (paste từ get_cookies.py) | Nếu dùng ChatGPT |
| `GROK_COOKIES` | (paste từ get_cookies.py) | Nếu dùng Grok |

---

## BƯỚC 4: Sử dụng API

Railway sẽ cho bạn 1 URL dạng: `https://your-app.up.railway.app`

### Dashboard
Mở trình duyệt vào URL đó → Dashboard trực quan

### Chạy với prompt tùy chỉnh
```bash
curl -X POST https://your-app.up.railway.app/run \
  -H "X-API-Secret: mật-khẩu-của-bạn" \
  -H "Content-Type: application/json" \
  -d '{"prompts": ["AI là gì?", "Python có ưu điểm gì?"]}'
```

### Lấy prompts từ web của bạn rồi chạy
```bash
curl -X POST https://your-app.up.railway.app/run-from-url \
  -H "X-API-Secret: mật-khẩu-của-bạn"
```

### Xem kết quả
```bash
curl https://your-app.up.railway.app/results?secret=mật-khẩu-của-bạn
```

### Kiểm tra trạng thái
```bash
curl https://your-app.up.railway.app/status
```

---

## Cấu trúc Webhook payload (gửi về web của bạn)

```json
{
  "timestamp": "2025-01-15T10:30:00",
  "prompt": "AI là gì?",
  "results": {
    "gemini":  "Trí tuệ nhân tạo (AI) là...",
    "chatgpt": "AI, hay Artificial Intelligence...",
    "grok":    "AI là công nghệ..."
  }
}
```

---

## API endpoint web của bạn cần có

### Nhận kết quả (`WEBHOOK_URL`)
```
POST /api/ai-results
Body: JSON payload như trên
```

### Cung cấp prompts (nếu dùng `PROMPT_URL`)
```
GET /api/get-prompts
Response: ["prompt 1", "prompt 2"]
hoặc:    [{"prompt": "prompt 1"}, {"prompt": "prompt 2"}]
```

---

## Lưu ý

- **Cookies hết hạn** sau 7-30 ngày → chạy lại `get_cookies.py` và cập nhật env vars
- **Railway free tier**: 500 giờ/tháng, đủ dùng nếu không chạy liên tục
- **Memory**: Kết quả lưu trong RAM, restart sẽ mất → nên dùng webhook để lưu vào DB của bạn
