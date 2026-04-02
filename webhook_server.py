"""
Webhook server mẫu - chạy trên web của bạn để nhận kết quả từ scraper
Dùng FastAPI hoặc Flask đều được
"""

# ══════════════════════════════════════════════
# CÁCH 1: FastAPI (khuyên dùng)
# pip install fastapi uvicorn
# ══════════════════════════════════════════════

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import json
from datetime import datetime
from pathlib import Path

app = FastAPI()

RESULTS_DIR = Path("ai_results")
RESULTS_DIR.mkdir(exist_ok=True)


@app.post("/api/ai-results")
async def receive_results(request: Request):
    """Nhận kết quả từ AI scraper"""
    try:
        payload = await request.json()

        # Log
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        print(f"\n📥 Nhận kết quả lúc {timestamp}")
        print(f"📝 Prompt: {payload.get('prompt', '')[:60]}...")

        for ai, response in payload.get("results", {}).items():
            if response:
                print(f"  ✅ {ai.upper()}: {len(response)} ký tự")
            else:
                print(f"  ❌ {ai.upper()}: Không có kết quả")

        # Lưu vào file
        save_path = RESULTS_DIR / f"result_{timestamp}.json"
        save_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # TODO: Lưu vào database của bạn
        # await db.insert("ai_results", payload)

        return JSONResponse({"status": "ok", "saved": str(save_path)})

    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/get-prompts")
async def get_prompts():
    """
    Endpoint trả về danh sách prompt (nếu dùng prompt_source='url')
    Trả về list string hoặc list object {"prompt": "..."}
    """
    prompts = [
        {"prompt": "Giải thích machine learning là gì?"},
        {"prompt": "So sánh Python và JavaScript"},
    ]
    return prompts


@app.get("/api/results")
async def list_results():
    """Xem tất cả kết quả đã lưu"""
    files = sorted(RESULTS_DIR.glob("*.json"), reverse=True)[:20]
    results = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        results.append({
            "file": f.name,
            "timestamp": data.get("timestamp"),
            "prompt": data.get("prompt", "")[:80],
            "ais": list(data.get("results", {}).keys()),
        })
    return results


if __name__ == "__main__":
    import uvicorn
    # Chạy: python webhook_server.py
    uvicorn.run(app, host="0.0.0.0", port=8000)


# ══════════════════════════════════════════════
# CÁCH 2: Flask (nếu bạn đang dùng Flask)
# pip install flask
# ══════════════════════════════════════════════
"""
from flask import Flask, request, jsonify
import json
from datetime import datetime

app = Flask(__name__)

@app.route("/api/ai-results", methods=["POST"])
def receive_results():
    payload = request.get_json()
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"results/result_{timestamp}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
"""
