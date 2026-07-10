"""Smoke test：本機跑前需 export GEMINI_API_KEY + FAL_KEY 環境變數。
直接執行：python3 test_api.py（缺 key 會 exit 1）。
pytest 收集時缺 key 改用 skip——原本 sys.exit(1) 會讓整個 pytest collection
掛掉（INTERNALERROR），連其他測試檔都跑不了。"""
import os, sys, json

if not os.environ.get('GEMINI_API_KEY') or not os.environ.get('FAL_KEY'):
    _msg = "需要 GEMINI_API_KEY + FAL_KEY 環境變數（真呼叫外部 API 的 smoke test）"
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(_msg, allow_module_level=True)
    else:
        print(f"ERROR: {_msg}")
        sys.exit(1)

# Test 1: Gemini JSON mode
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
resp = client.models.generate_content(
    model='gemini-3.5-flash',
    contents='Return JSON: {"status": "ok", "msg": "Gemini connected"}',
    config=types.GenerateContentConfig(response_mime_type='application/json')
)
data = json.loads(resp.text)
print(f"[1] Gemini JSON: {data}")

# Test 2: furniture_match
from furniture_match import load_catalog, match_furniture
catalog = load_catalog()
items = match_furniture("modern", "white oak panels, linen sofa, recessed LED", catalog, top_n=3)
print(f"\n[2] Furniture match (modern, top 3):")
for item in items:
    print(f"    {item['name_zh']} | NT${item['price_twd']:,} | {item['category']}")

# Test 3: fal.ai connection
import fal_client
print(f"\n[3] fal_client imported OK, key set: {bool(os.environ.get('FAL_KEY'))}")

print("\n=== All systems GO ===")
