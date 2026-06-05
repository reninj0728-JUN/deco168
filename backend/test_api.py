"""Smoke test：本機跑前需 export GEMINI_API_KEY + FAL_KEY 環境變數。"""
import os, sys, json

if not os.environ.get('GEMINI_API_KEY'):
    print("ERROR: 請先設 GEMINI_API_KEY 環境變數再執行此測試")
    sys.exit(1)
if not os.environ.get('FAL_KEY'):
    print("ERROR: 請先設 FAL_KEY 環境變數再執行此測試")
    sys.exit(1)

# Test 1: Gemini JSON mode
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
resp = client.models.generate_content(
    model='gemini-3.1-flash-lite',
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
