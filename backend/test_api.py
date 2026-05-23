import os, json
os.environ['GEMINI_API_KEY'] = 'AIzaSyAX2N4IIbpg4Z2CjNUrktKb3KrthOU094Y'
os.environ['FAL_KEY'] = 'f7a5e217-b7ca-4c8a-b852-f53b25610f11:5b6396d8af1370127eb244c4460f6109'

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
