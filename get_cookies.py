"""
get_cookies.py - Chạy LOCAL để lấy cookies, sau đó paste lên Railway env vars
Chỉ cần chạy 1 lần!
"""

import asyncio
import json
import sys
from playwright.async_api import async_playwright

URLS = {
    "gemini":  "https://gemini.google.com/",
    "chatgpt": "https://chatgpt.com/",
    "grok":    "https://grok.com/",
}


async def get_cookies(ai_name: str):
    print(f"\n{'='*55}")
    print(f"  🔐 Lấy cookies cho: {ai_name.upper()}")
    print(f"{'='*55}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = await browser.new_context(viewport=None)
        page = await ctx.new_page()

        print(f"\n👉 Browser đang mở {URLS[ai_name]}")
        print("👉 Hãy đăng nhập vào tài khoản của bạn")
        print("👉 Sau khi đăng nhập xong → nhấn ENTER ở đây\n")

        await page.goto(URLS[ai_name])
        input("⏎  Nhấn ENTER sau khi đăng nhập xong...")

        cookies = await ctx.cookies()
        await browser.close()

    cookie_json = json.dumps(cookies)
    env_var = f"{ai_name.upper()}_COOKIES"

    print(f"\n✅ Lấy được {len(cookies)} cookies!")
    print(f"\n{'─'*55}")
    print(f"📋 Copy đoạn này lên Railway → Variables → New Variable:")
    print(f"{'─'*55}")
    print(f"\nKey:   {env_var}")
    print(f"Value: {cookie_json}")
    print(f"\n{'─'*55}")

    # Lưu ra file backup
    with open(f"{ai_name}_cookies.json", "w") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"💾 Đã lưu backup: {ai_name}_cookies.json")


async def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(URLS.keys())
    valid = [t for t in targets if t in URLS]

    if not valid:
        print(f"Usage: python get_cookies.py [gemini] [chatgpt] [grok]")
        print(f"       python get_cookies.py          ← lấy tất cả")
        return

    for ai in valid:
        await get_cookies(ai)
        print()

    print("\n🎉 Xong! Copy các env vars trên lên Railway là tool chạy được.")


if __name__ == "__main__":
    asyncio.run(main())
