"""
AI Multi-Scraper Tool
Tự động gửi prompt đến Gemini, ChatGPT, Grok và gửi kết quả về webhook
"""

import asyncio
import json
import logging
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─── Load config ───────────────────────────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent / "config" / "config.json"

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

WEBHOOK_URL   = CONFIG["webhook_url"]
PROMPT_SOURCE = CONFIG["prompt_source"]   # "file" hoặc "url"
PROMPT_FILE   = CONFIG.get("prompt_file", "data/prompts.txt")
PROMPT_URL    = CONFIG.get("prompt_url", "")
SELECTED_AIS  = CONFIG.get("selected_ais", ["gemini", "chatgpt", "grok"])
COOKIES_DIR   = Path(__file__).parent / "config" / "cookies"
LOG_DIR       = Path(__file__).parent / "logs"

COOKIES_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ai_scraper")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def load_prompts_from_file() -> list[str]:
    path = Path(__file__).parent / PROMPT_FILE
    if not path.exists():
        log.warning(f"Không tìm thấy file prompt: {path}")
        return []
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    log.info(f"Đọc {len(lines)} prompt từ file")
    return lines


async def load_prompts_from_url() -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(PROMPT_URL)
        resp.raise_for_status()
        data = resp.json()
        # Hỗ trợ 2 format: list string hoặc list object có key "prompt"
        if isinstance(data, list):
            prompts = [
                (item["prompt"] if isinstance(item, dict) else item)
                for item in data
            ]
        else:
            prompts = [data.get("prompt", str(data))]
        log.info(f"Đọc {len(prompts)} prompt từ URL")
        return prompts


async def get_prompts() -> list[str]:
    if PROMPT_SOURCE == "url":
        return await load_prompts_from_url()
    return load_prompts_from_file()


async def send_to_webhook(payload: dict):
    """Gửi kết quả về webhook của bạn"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(WEBHOOK_URL, json=payload)
            resp.raise_for_status()
            log.info(f"✅ Đã gửi webhook → {resp.status_code}")
    except Exception as e:
        log.error(f"❌ Lỗi gửi webhook: {e}")
        # Lưu backup vào file nếu webhook lỗi
        backup_path = LOG_DIR / f"failed_{int(time.time())}.json"
        backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info(f"💾 Đã lưu backup: {backup_path}")


def human_delay(min_s=1.5, max_s=3.5):
    """Delay ngẫu nhiên giả lập hành vi người dùng"""
    time.sleep(random.uniform(min_s, max_s))


# ─── AI Scrapers ───────────────────────────────────────────────────────────────

class BaseScraper:
    NAME = "base"
    URL  = ""

    def __init__(self, browser_context):
        self.ctx = browser_context
        self.page = None

    async def new_page(self):
        self.page = await self.ctx.new_page()
        # Chặn ảnh/font để load nhanh hơn
        await self.page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot}",
            lambda route: route.abort()
        )

    async def close(self):
        if self.page:
            await self.page.close()

    async def ask(self, prompt: str) -> Optional[str]:
        raise NotImplementedError


class GeminiScraper(BaseScraper):
    NAME = "gemini"
    URL  = "https://gemini.google.com/app"

    async def ask(self, prompt: str) -> Optional[str]:
        await self.new_page()
        try:
            log.info("🤖 Gemini: Mở trang...")
            await self.page.goto(self.URL, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(3000)

            # Tìm textarea input
            selectors = [
                "rich-textarea div[contenteditable='true']",
                "textarea[placeholder]",
                ".ql-editor",
                "[data-placeholder]",
            ]
            input_el = None
            for sel in selectors:
                try:
                    input_el = await self.page.wait_for_selector(sel, timeout=8000)
                    if input_el:
                        break
                except PlaywrightTimeout:
                    continue

            if not input_el:
                log.error("Gemini: Không tìm thấy input. Có thể cần đăng nhập.")
                return None

            log.info("🤖 Gemini: Gõ prompt...")
            await input_el.click()
            await self.page.keyboard.type(prompt, delay=30)
            human_delay(0.5, 1.5)

            # Gửi prompt
            await self.page.keyboard.press("Enter")

            # Chờ response
            log.info("🤖 Gemini: Chờ phản hồi...")
            await self.page.wait_for_timeout(5000)

            # Chờ loading hoàn thành
            try:
                await self.page.wait_for_selector(
                    "model-response .response-content, .model-response-text",
                    timeout=60000,
                    state="visible"
                )
            except PlaywrightTimeout:
                pass

            await self.page.wait_for_timeout(3000)

            # Lấy response cuối cùng
            response_selectors = [
                "model-response .response-content",
                ".model-response-text",
                "message-content p",
            ]
            for sel in response_selectors:
                els = await self.page.query_selector_all(sel)
                if els:
                    texts = [await el.inner_text() for el in els]
                    result = "\n".join(t for t in texts if t.strip())
                    if result:
                        log.info(f"✅ Gemini: Lấy được {len(result)} ký tự")
                        return result

            return None

        except Exception as e:
            log.error(f"Gemini lỗi: {e}")
            return None
        finally:
            await self.close()


class ChatGPTScraper(BaseScraper):
    NAME = "chatgpt"
    URL  = "https://chatgpt.com/"

    async def ask(self, prompt: str) -> Optional[str]:
        await self.new_page()
        try:
            log.info("🤖 ChatGPT: Mở trang...")
            await self.page.goto(self.URL, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(3000)

            # Tìm input
            input_el = None
            selectors = [
                "#prompt-textarea",
                "textarea[placeholder]",
                "div[contenteditable='true']",
            ]
            for sel in selectors:
                try:
                    input_el = await self.page.wait_for_selector(sel, timeout=8000)
                    if input_el:
                        break
                except PlaywrightTimeout:
                    continue

            if not input_el:
                log.error("ChatGPT: Không tìm thấy input. Có thể cần đăng nhập.")
                return None

            log.info("🤖 ChatGPT: Gõ prompt...")
            await input_el.click()
            await self.page.keyboard.type(prompt, delay=30)
            human_delay(0.5, 1.5)

            # Nhấn gửi
            send_btn = await self.page.query_selector(
                "button[data-testid='send-button'], button[aria-label='Send prompt']"
            )
            if send_btn:
                await send_btn.click()
            else:
                await self.page.keyboard.press("Enter")

            # Chờ response
            log.info("🤖 ChatGPT: Chờ phản hồi...")
            await self.page.wait_for_timeout(4000)

            # Chờ streaming xong (nút stop biến mất)
            try:
                await self.page.wait_for_selector(
                    "button[aria-label='Stop streaming']",
                    timeout=10000, state="visible"
                )
                await self.page.wait_for_selector(
                    "button[aria-label='Stop streaming']",
                    timeout=90000, state="hidden"
                )
            except PlaywrightTimeout:
                pass

            await self.page.wait_for_timeout(2000)

            # Lấy response cuối cùng
            messages = await self.page.query_selector_all(
                "[data-message-author-role='assistant'] .markdown"
            )
            if not messages:
                messages = await self.page.query_selector_all(
                    "div[data-message-author-role='assistant']"
                )

            if messages:
                last = messages[-1]
                result = await last.inner_text()
                log.info(f"✅ ChatGPT: Lấy được {len(result)} ký tự")
                return result

            return None

        except Exception as e:
            log.error(f"ChatGPT lỗi: {e}")
            return None
        finally:
            await self.close()


class GrokScraper(BaseScraper):
    NAME = "grok"
    URL  = "https://grok.com/"

    async def ask(self, prompt: str) -> Optional[str]:
        await self.new_page()
        try:
            log.info("🤖 Grok: Mở trang...")
            await self.page.goto(self.URL, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(3000)

            # Tìm input
            input_el = None
            selectors = [
                "textarea[placeholder]",
                "div[contenteditable='true']",
                ".query-input",
            ]
            for sel in selectors:
                try:
                    input_el = await self.page.wait_for_selector(sel, timeout=8000)
                    if input_el:
                        break
                except PlaywrightTimeout:
                    continue

            if not input_el:
                log.error("Grok: Không tìm thấy input. Có thể cần đăng nhập.")
                return None

            log.info("🤖 Grok: Gõ prompt...")
            await input_el.click()
            await self.page.keyboard.type(prompt, delay=30)
            human_delay(0.5, 1.5)

            await self.page.keyboard.press("Enter")

            # Chờ response
            log.info("🤖 Grok: Chờ phản hồi...")
            await self.page.wait_for_timeout(5000)

            try:
                await self.page.wait_for_selector(
                    ".message-bubble, .response-text, [class*='message'][class*='assistant']",
                    timeout=60000
                )
            except PlaywrightTimeout:
                pass

            await self.page.wait_for_timeout(4000)

            # Lấy response
            response_selectors = [
                ".message-bubble",
                "[class*='AssistantMessage']",
                "[class*='response']",
            ]
            for sel in response_selectors:
                els = await self.page.query_selector_all(sel)
                if els:
                    last = els[-1]
                    result = await last.inner_text()
                    if result.strip():
                        log.info(f"✅ Grok: Lấy được {len(result)} ký tự")
                        return result

            return None

        except Exception as e:
            log.error(f"Grok lỗi: {e}")
            return None
        finally:
            await self.close()


# ─── Cookie Manager ────────────────────────────────────────────────────────────

async def save_cookies(ctx, name: str):
    path = COOKIES_DIR / f"{name}_cookies.json"
    cookies = await ctx.cookies()
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
    log.info(f"💾 Đã lưu cookies: {path}")


async def load_cookies(ctx, name: str) -> bool:
    path = COOKIES_DIR / f"{name}_cookies.json"
    if not path.exists():
        return False
    cookies = json.loads(path.read_text())
    await ctx.add_cookies(cookies)
    log.info(f"🍪 Đã load cookies: {name}")
    return True


# ─── Login Flow ────────────────────────────────────────────────────────────────

async def login_session(playwright, ai_name: str):
    """Mở browser thật để đăng nhập, lưu cookies"""
    log.info(f"\n{'='*50}")
    log.info(f"🔐 Đăng nhập {ai_name.upper()} - Mở browser để bạn tự đăng nhập...")
    log.info(f"{'='*50}")

    browser = await playwright.chromium.launch(
        headless=False,
        args=["--start-maximized"]
    )
    ctx = await browser.new_context(viewport=None)

    urls = {
        "gemini": "https://gemini.google.com/",
        "chatgpt": "https://chatgpt.com/",
        "grok": "https://grok.com/",
    }

    page = await ctx.new_page()
    await page.goto(urls[ai_name])

    print(f"\n👉 Hãy đăng nhập vào {ai_name.upper()} trong browser vừa mở.")
    print("👉 Sau khi đăng nhập xong, nhấn ENTER ở đây để lưu cookies...\n")
    input()

    await save_cookies(ctx, ai_name)
    await browser.close()
    log.info(f"✅ Đã lưu session {ai_name}")


# ─── Main Runner ───────────────────────────────────────────────────────────────

SCRAPER_MAP = {
    "gemini":  GeminiScraper,
    "chatgpt": ChatGPTScraper,
    "grok":    GrokScraper,
}


async def process_prompt(prompt: str, playwright):
    """Xử lý 1 prompt với tất cả AI được chọn (tuần tự)"""
    results = {}

    for ai_name in SELECTED_AIS:
        if ai_name not in SCRAPER_MAP:
            continue

        log.info(f"\n{'─'*40}")
        log.info(f"🚀 Đang xử lý: {ai_name.upper()}")
        log.info(f"📝 Prompt: {prompt[:80]}...")

        # Tạo browser context với cookies đã lưu
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )

        # User agent thật
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="vi-VN",
        )

        # Load cookies
        cookie_loaded = await load_cookies(ctx, ai_name)
        if not cookie_loaded:
            log.warning(f"⚠️  {ai_name}: Chưa có cookies. Chạy --login trước!")
            await browser.close()
            results[ai_name] = None
            continue

        # Scrape
        scraper_cls = SCRAPER_MAP[ai_name]
        scraper = scraper_cls(ctx)

        response = await scraper.ask(prompt)
        results[ai_name] = response

        await browser.close()

        # Delay giữa các AI
        if ai_name != SELECTED_AIS[-1]:
            human_delay(2, 4)

    # Gửi về webhook
    payload = {
        "timestamp": datetime.now().isoformat(),
        "prompt": prompt,
        "results": results,
        "source": PROMPT_SOURCE,
    }

    log.info(f"\n📤 Gửi kết quả về webhook...")
    await send_to_webhook(payload)

    return results


async def main():
    import sys

    async with async_playwright() as playwright:
        # Chế độ đăng nhập
        if "--login" in sys.argv:
            idx = sys.argv.index("--login")
            ai_name = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
            if ai_name and ai_name in SCRAPER_MAP:
                await login_session(playwright, ai_name)
            else:
                # Đăng nhập tất cả
                for name in SELECTED_AIS:
                    await login_session(playwright, name)
            return

        # Chạy bình thường
        log.info("=" * 50)
        log.info("🤖 AI Multi-Scraper khởi động")
        log.info(f"📡 Webhook: {WEBHOOK_URL}")
        log.info(f"🎯 AI targets: {', '.join(SELECTED_AIS)}")
        log.info("=" * 50)

        prompts = await get_prompts()
        if not prompts:
            log.error("❌ Không có prompt nào để xử lý!")
            return

        log.info(f"📋 Tổng cộng {len(prompts)} prompt cần xử lý\n")

        for i, prompt in enumerate(prompts, 1):
            log.info(f"\n{'='*50}")
            log.info(f"📌 Prompt {i}/{len(prompts)}")
            log.info("=" * 50)

            results = await process_prompt(prompt, playwright)

            # Log kết quả tóm tắt
            for ai, resp in results.items():
                status = f"✅ {len(resp)} ký tự" if resp else "❌ Thất bại"
                log.info(f"  {ai.upper()}: {status}")

            # Delay giữa các prompt
            if i < len(prompts):
                delay = random.uniform(5, 10)
                log.info(f"\n⏳ Chờ {delay:.1f}s trước prompt tiếp theo...")
                await asyncio.sleep(delay)

        log.info("\n🎉 Hoàn thành tất cả prompts!")


if __name__ == "__main__":
    asyncio.run(main())
