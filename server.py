"""
server.py - Chạy trên Railway
- API để nhận lệnh từ web của bạn
- Scheduler tự động chạy scraper theo lịch
- Dashboard xem kết quả
"""

import asyncio
import json
import logging
import os
import time
import random
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("server")

# ─── Config từ environment variables (Railway) ────────────────────────────────
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "").strip()   # URL nhận kết quả
if WEBHOOK_URL.startswith("https://http://"):
    WEBHOOK_URL = WEBHOOK_URL.replace("https://", "", 1)
if WEBHOOK_URL.startswith("http://http://"):
    WEBHOOK_URL = WEBHOOK_URL.replace("http://", "", 1)
WEBHOOK_URL = WEBHOOK_URL.rstrip("/")
PROMPT_SOURCE  = os.getenv("PROMPT_SOURCE", "env")      # "env", "file", "url"
PROMPT_URL     = os.getenv("PROMPT_URL", "")            # URL lấy prompts
SELECTED_AIS   = os.getenv("SELECTED_AIS", "gemini,chatgpt,grok").split(",")
API_SECRET     = os.getenv("API_SECRET", "change-me").strip()  # Bảo vệ API
LEGACY_API_KEYS = {k for k in {API_SECRET, os.getenv("LEGACY_API_SECRET", "").strip(), "silas123"} if k}
ALLOW_PUBLIC_COMPLETIONS = os.getenv("ALLOW_PUBLIC_COMPLETIONS", "1").strip().lower() not in {"0", "false", "no"}
PORT           = int(os.getenv("PORT", 8000))
ASK_TIMEOUT_SECONDS = int(os.getenv("ASK_TIMEOUT_SECONDS", "180"))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "12000"))
NO_LOGIN_MODE = os.getenv("NO_LOGIN_MODE", "1").strip().lower() not in {"0", "false", "no"}

# Cookies lưu dưới dạng JSON string trong env vars
GEMINI_COOKIES  = os.getenv("GEMINI_COOKIES", "")
CHATGPT_COOKIES = os.getenv("CHATGPT_COOKIES", "")
GROK_COOKIES    = os.getenv("GROK_COOKIES", "")

COOKIE_MAP = {
    "gemini":  GEMINI_COOKIES,
    "chatgpt": CHATGPT_COOKIES,
    "grok":    GROK_COOKIES,
}

# ─── State ─────────────────────────────────────────────────────────────────────
results_store: list[dict] = []   # Lưu kết quả trong memory (hoặc dùng DB)
is_running = False
current_task = None
SUPPORTED_AIS = {"gemini", "chatgpt", "grok"}


class AskRequest(BaseModel):
    ai: str
    prompt: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "gemini:default"
    messages: list[ChatMessage]
    stream: bool = False
    max_tokens: int | None = None


# ─── Helpers ───────────────────────────────────────────────────────────────────

async def get_prompts_from_url() -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(PROMPT_URL)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return [(item["prompt"] if isinstance(item, dict) else item) for item in data]
        return [str(data)]


async def send_webhook(payload: dict):
    if not WEBHOOK_URL:
        log.info("Không có WEBHOOK_URL, bỏ qua gửi")
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(WEBHOOK_URL, json=payload)
            r.raise_for_status()
            log.info(f"✅ Webhook sent → {r.status_code}")
    except Exception as e:
        log.error(f"❌ Webhook lỗi: {e}")


# ─── Playwright Scrapers ───────────────────────────────────────────────────────

async def scrape_ai(ai_name: str, prompt: str, cookies_json: str) -> str | None:
    """Chạy Playwright headless, scrape 1 AI"""
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error("Playwright chưa được cài!")
        return None

    cookies = []
    if cookies_json:
        try:
            cookies = json.loads(cookies_json)
        except Exception:
            log.warning(f"{ai_name}: Cookie JSON không hợp lệ")

    urls = {
        "gemini":  "https://gemini.google.com/app",
        "chatgpt": "https://chatgpt.com/",
        "grok":    "https://grok.com/",
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--single-process",
            ]
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        if cookies:
            await ctx.add_cookies(cookies)
            log.info(f"🔐 {ai_name}: đã nạp cookies đăng nhập ({len(cookies)} cookies)")
        elif NO_LOGIN_MODE:
            log.info(f"🔓 {ai_name}: chạy chế độ guest/no-login, mở tab mới không dùng cookie")

        page = await ctx.new_page()

        # Chặn media để load nhanh
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3}",
            lambda r: r.abort()
        )

        try:
            log.info(f"🌐 {ai_name}: Mở {urls[ai_name]}")
            await page.goto(urls[ai_name], wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            result = None

            if ai_name == "gemini":
                result = await _scrape_gemini(page, prompt)
            elif ai_name == "chatgpt":
                result = await _scrape_chatgpt(page, prompt)
            elif ai_name == "grok":
                result = await _scrape_grok(page, prompt)

            if not result and NO_LOGIN_MODE and not cookies:
                log.warning(f"{ai_name}: không lấy được nội dung ở chế độ no-login; provider có thể đang yêu cầu đăng nhập")

            return result

        except Exception as e:
            log.error(f"{ai_name} lỗi: {e}")
            return None
        finally:
            await browser.close()


async def _find_input(page, selectors: list[str], timeout=8000):
    from playwright.async_api import TimeoutError as PWTimeout
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout)
            if el:
                return el
        except PWTimeout:
            continue
    return None


async def _scrape_gemini(page, prompt: str) -> str | None:
    from playwright.async_api import TimeoutError as PWTimeout
    el = await _find_input(page, [
        "rich-textarea div[contenteditable='true']",
        "textarea[placeholder]",
        ".ql-editor",
    ])
    if not el:
        log.error("Gemini: Không tìm thấy input")
        return None

    await el.click()
    await page.keyboard.type(prompt, delay=25)
    await asyncio.sleep(random.uniform(0.5, 1.2))
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(5000)

    try:
        await page.wait_for_selector(
            "model-response .response-content, .model-response-text",
            timeout=60000, state="visible"
        )
    except PWTimeout:
        pass

    await page.wait_for_timeout(3000)

    for sel in ["model-response .response-content", ".model-response-text"]:
        els = await page.query_selector_all(sel)
        if els:
            texts = [await e.inner_text() for e in els]
            result = "\n".join(t for t in texts if t.strip())
            if result:
                if "You stopped this response" in result:
                    await page.wait_for_timeout(2500)
                    retry_texts = [await e.inner_text() for e in els]
                    retried = "\n".join(t for t in retry_texts if t.strip())
                    if retried and "You stopped this response" not in retried:
                        return retried
                    return None
                return result
    return None


async def _scrape_chatgpt(page, prompt: str) -> str | None:
    from playwright.async_api import TimeoutError as PWTimeout
    el = await _find_input(page, [
        "#prompt-textarea",
        "textarea[placeholder]",
        "div[contenteditable='true']",
    ])
    if not el:
        log.error("ChatGPT: Không tìm thấy input")
        return None

    await el.click()
    await page.keyboard.type(prompt, delay=25)
    await asyncio.sleep(random.uniform(0.5, 1.2))

    btn = await page.query_selector(
        "button[data-testid='send-button'], button[aria-label='Send prompt']"
    )
    if btn:
        await btn.click()
    else:
        await page.keyboard.press("Enter")

    await page.wait_for_timeout(4000)

    try:
        await page.wait_for_selector("button[aria-label='Stop streaming']",
                                     timeout=10000, state="visible")
        await page.wait_for_selector("button[aria-label='Stop streaming']",
                                     timeout=90000, state="hidden")
    except PWTimeout:
        pass

    await page.wait_for_timeout(2000)

    for sel in [
        "[data-message-author-role='assistant'] .markdown",
        "div[data-message-author-role='assistant']",
    ]:
        els = await page.query_selector_all(sel)
        if els:
            return await els[-1].inner_text()
    return None


async def _scrape_grok(page, prompt: str) -> str | None:
    from playwright.async_api import TimeoutError as PWTimeout
    el = await _find_input(page, [
        "textarea[placeholder]",
        "div[contenteditable='true']",
        ".query-input",
    ])
    if not el:
        log.error("Grok: Không tìm thấy input")
        return None

    await el.click()
    await page.keyboard.type(prompt, delay=25)
    await asyncio.sleep(random.uniform(0.5, 1.2))
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(5000)

    try:
        await page.wait_for_selector(
            ".message-bubble, [class*='AssistantMessage']",
            timeout=60000
        )
    except PWTimeout:
        pass

    await page.wait_for_timeout(4000)

    for sel in [".message-bubble", "[class*='AssistantMessage']"]:
        els = await page.query_selector_all(sel)
        if els:
            text = await els[-1].inner_text()
            if text.strip():
                return text
    return None


# ─── Core scraping job ────────────────────────────────────────────────────────

async def run_scrape_job(prompts: list[str]):
    global is_running
    is_running = True
    log.info(f"🚀 Bắt đầu job: {len(prompts)} prompt × {len(SELECTED_AIS)} AI")

    for i, prompt in enumerate(prompts, 1):
        log.info(f"\n📌 Prompt {i}/{len(prompts)}: {prompt[:60]}...")
        ai_results = {}

        for ai_name in SELECTED_AIS:
            ai_name = ai_name.strip()
            cookies_json = COOKIE_MAP.get(ai_name, "")
            response = await scrape_ai(ai_name, prompt, cookies_json)
            ai_results[ai_name] = response
            status = f"✅ {len(response)} ký tự" if response else "❌ thất bại"
            log.info(f"  {ai_name.upper()}: {status}")

            if ai_name != SELECTED_AIS[-1]:
                await asyncio.sleep(random.uniform(2, 4))

        payload = {
            "timestamp": datetime.now().isoformat(),
            "prompt": prompt,
            "results": ai_results,
        }

        # Lưu local
        results_store.append(payload)
        if len(results_store) > 200:
            results_store.pop(0)

        # Gửi webhook
        await send_webhook(payload)

        if i < len(prompts):
            delay = random.uniform(5, 10)
            log.info(f"⏳ Chờ {delay:.1f}s...")
            await asyncio.sleep(delay)

    is_running = False
    log.info("🎉 Job hoàn thành!")


# ─── FastAPI App ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"🚀 Server khởi động trên port {PORT}")
    log.info(f"🎯 AI targets: {SELECTED_AIS}")
    log.info(f"📡 Webhook: {WEBHOOK_URL or '(không có)'}")
    yield
    log.info("Server tắt")


app = FastAPI(title="AI Scraper API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def check_auth(request: Request, allow_public: bool = False):
    if allow_public and ALLOW_PUBLIC_COMPLETIONS:
        return

    auth_header = request.headers.get("Authorization", "")
    bearer_secret = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    secret = request.headers.get("X-API-Secret") or bearer_secret or request.query_params.get("secret")
    if secret not in LEGACY_API_KEYS:
        raise HTTPException(status_code=401, detail="Unauthorized - Cần X-API-Secret header")


def parse_model(model: str) -> tuple[str, str]:
    raw = (model or "").strip()
    lower = raw.lower()
    if lower.startswith("gemini:"):
        return "gemini", raw.split(":", 1)[1].strip() or "default"
    if lower.startswith("chatgpt:"):
        return "chatgpt", raw.split(":", 1)[1].strip() or "default"
    if lower.startswith("grok:"):
        return "grok", raw.split(":", 1)[1].strip() or "default"
    if lower in SUPPORTED_AIS:
        return lower, "default"
    return "gemini", raw or "default"


def openai_like_response(model: str, content: str) -> dict:
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }


def build_runtime_message(ai_name: str, result: str | None) -> str:
    if result:
        return result
    if NO_LOGIN_MODE:
        return (
            f"{ai_name} hiện đang yêu cầu đăng nhập khi chạy ở chế độ guest/no-login. "
            f"Hãy bật cookies hoặc đổi sang API chính thức nếu muốn dùng ổn định."
        )
    return f"{ai_name} không trả về nội dung"


@app.post("/ask")
async def ask_single(payload: AskRequest, request: Request):
    """Chạy đồng bộ 1 prompt cho 1 AI cụ thể (gemini/chatgpt/grok)."""
    check_auth(request)

    ai_name = str(payload.ai or "").strip().lower()
    prompt = str(payload.prompt or "").strip()

    if ai_name not in SUPPORTED_AIS:
        raise HTTPException(status_code=400, detail=f"AI không hỗ trợ: {ai_name}")
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt không được để trống")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=400, detail=f"Prompt quá dài (max {MAX_PROMPT_CHARS} ký tự)")

    cookies_json = COOKIE_MAP.get(ai_name, "")
    started = time.perf_counter()

    try:
        text = await asyncio.wait_for(
            scrape_ai(ai_name, prompt, cookies_json),
            timeout=ASK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"{ai_name} timeout sau {ASK_TIMEOUT_SECONDS}s")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{ai_name} lỗi: {exc}")

    latency_ms = int((time.perf_counter() - started) * 1000)
    if not text:
        return {
            "ok": False,
            "ai": ai_name,
            "text": "",
            "error": f"{ai_name} không trả về nội dung",
            "latency_ms": latency_ms,
        }

    return {
        "ok": True,
        "ai": ai_name,
        "text": text,
        "error": None,
        "latency_ms": latency_ms,
    }


# ─── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Dashboard HTML đơn giản"""
    rows = ""
    for r in reversed(results_store[-20:]):
        ais = ""
        for ai, resp in r.get("results", {}).items():
            ok = "✅" if resp else "❌"
            chars = f"{len(resp)} ký tự" if resp else "—"
            ais += f"<span style='margin-right:12px'>{ok} <b>{ai}</b>: {chars}</span>"
        rows += f"""
        <tr>
          <td style='padding:8px;color:#888;white-space:nowrap'>{r['timestamp'][:19]}</td>
          <td style='padding:8px'>{r['prompt'][:80]}...</td>
          <td style='padding:8px'>{ais}</td>
        </tr>"""

    status_color = "#22c55e" if not is_running else "#f59e0b"
    status_text  = "Đang chạy..." if is_running else "Sẵn sàng"

    return f"""<!DOCTYPE html>
<html><head>
<meta charset='utf-8'>
<title>AI Scraper Dashboard</title>
<meta http-equiv='refresh' content='15'>
<style>
  body{{font-family:sans-serif;margin:0;background:#0f172a;color:#e2e8f0}}
  .header{{background:#1e293b;padding:24px 32px;border-bottom:1px solid #334155}}
  h1{{margin:0;font-size:22px}}
  .badge{{display:inline-block;padding:4px 12px;border-radius:20px;
          background:{status_color};color:#000;font-weight:bold;font-size:13px;margin-left:12px}}
  .container{{padding:24px 32px}}
  .card{{background:#1e293b;border-radius:12px;padding:20px;margin-bottom:20px;border:1px solid #334155}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;padding:10px 8px;border-bottom:1px solid #334155;color:#94a3b8;font-size:13px}}
  tr:hover td{{background:#263045}}
  .btn{{background:#3b82f6;color:#fff;border:none;padding:10px 20px;border-radius:8px;
        cursor:pointer;font-size:14px;margin-right:8px}}
  .btn:hover{{background:#2563eb}}
  input{{background:#0f172a;border:1px solid #334155;color:#e2e8f0;
         padding:10px;border-radius:8px;width:100%;box-sizing:border-box;margin-bottom:10px}}
</style>
</head><body>
<div class='header'>
  <h1>🤖 AI Scraper <span class='badge'>{status_text}</span></h1>
  <small style='color:#94a3b8'>Targets: {', '.join(SELECTED_AIS)} &nbsp;|&nbsp; {len(results_store)} kết quả trong memory</small>
</div>
<div class='container'>

  <div class='card'>
    <h3 style='margin-top:0'>▶️ Chạy ngay</h3>
    <input id='prompt' placeholder='Nhập prompt... (nhiều dòng = nhiều prompt)' />
    <button class='btn' onclick='runPrompt()'>Gửi prompt</button>
    <button class='btn' style='background:#8b5cf6' onclick='runFromUrl()'>Lấy từ URL</button>
    <div id='msg' style='margin-top:10px;color:#94a3b8'></div>
  </div>

  <div class='card'>
    <h3 style='margin-top:0'>📋 Kết quả gần nhất</h3>
    <table>
      <tr><th>Thời gian</th><th>Prompt</th><th>Kết quả</th></tr>
      {rows if rows else "<tr><td colspan='3' style='padding:20px;color:#64748b'>Chưa có kết quả</td></tr>"}
    </table>
  </div>

</div>
<script>
const SECRET = prompt('Nhập API Secret:') || '';
async function post(url, body) {{
  const r = await fetch(url, {{
    method:'POST', headers:{{'Content-Type':'application/json','X-API-Secret':SECRET}},
    body: JSON.stringify(body)
  }});
  return r.json();
}}
async function runPrompt() {{
  const text = document.getElementById('prompt').value.trim();
  if (!text) return;
  const prompts = text.split('\\n').filter(l=>l.trim());
  document.getElementById('msg').textContent = '⏳ Đang gửi...';
  const r = await post('/run', {{prompts}});
  document.getElementById('msg').textContent = r.message || JSON.stringify(r);
}}
async function runFromUrl() {{
  document.getElementById('msg').textContent = '⏳ Đang lấy prompts từ URL...';
  const r = await post('/run-from-url', {{}});
  document.getElementById('msg').textContent = r.message || JSON.stringify(r);
}}
</script>
</body></html>"""


@app.post("/run")
async def run_manual(request: Request, background_tasks: BackgroundTasks):
    """Chạy scraper với prompt tùy chỉnh"""
    check_auth(request)
    if is_running:
        return JSONResponse({"status": "busy", "message": "Scraper đang chạy, thử lại sau"})

    body = await request.json()
    prompts = body.get("prompts", [])
    if isinstance(prompts, str):
        prompts = [prompts]
    if not prompts:
        raise HTTPException(400, "Thiếu 'prompts'")

    background_tasks.add_task(run_scrape_job, prompts)
    return {"status": "started", "message": f"Đang xử lý {len(prompts)} prompt"}


@app.post("/run-from-url")
async def run_from_url(request: Request, background_tasks: BackgroundTasks):
    """Lấy prompts từ PROMPT_URL rồi chạy"""
    check_auth(request)
    if is_running:
        return JSONResponse({"status": "busy", "message": "Scraper đang chạy"})
    if not PROMPT_URL:
        raise HTTPException(400, "Chưa cấu hình PROMPT_URL")

    prompts = await get_prompts_from_url()
    if not prompts:
        raise HTTPException(400, "Không có prompt nào từ URL")

    background_tasks.add_task(run_scrape_job, prompts)
    return {"status": "started", "message": f"Lấy được {len(prompts)} prompt từ URL"}


@app.get("/results")
async def get_results(request: Request, limit: int = 50):
    """Lấy danh sách kết quả"""
    check_auth(request)
    return results_store[-limit:]


@app.get("/status")
async def status():
    return {
        "running": is_running,
        "results_count": len(results_store),
        "selected_ais": SELECTED_AIS,
        "prompt_url": PROMPT_URL,
        "webhook_url": WEBHOOK_URL,
        "no_login_mode": NO_LOGIN_MODE,
        "allow_public_completions": ALLOW_PUBLIC_COMPLETIONS,
    }


@app.get("/health")
async def health():
    return {"ok": True, "time": datetime.now().isoformat()}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    check_auth(request, allow_public=True)

    if body.stream:
        raise HTTPException(status_code=400, detail="Streaming not supported on this endpoint")
    if not body.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")

    ai_name, resolved_model = parse_model(body.model)
    if ai_name not in SUPPORTED_AIS:
        raise HTTPException(status_code=400, detail=f"Unsupported model provider: {ai_name}")

    prompt = str(body.messages[-1].content or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt must not be empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=400, detail=f"prompt too long (max {MAX_PROMPT_CHARS} chars)")

    cookies_json = COOKIE_MAP.get(ai_name, "")
    try:
        result = await asyncio.wait_for(
            scrape_ai(ai_name, prompt, cookies_json),
            timeout=ASK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"{ai_name} timed out")

    if not result:
        if NO_LOGIN_MODE:
            return openai_like_response(
                f"{ai_name}:{resolved_model}",
                build_runtime_message(ai_name, None),
            )
        raise HTTPException(status_code=502, detail=f"{ai_name} returned empty response")

    return openai_like_response(f"{ai_name}:{resolved_model}", build_runtime_message(ai_name, result))


# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False)
