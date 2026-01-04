
import json
import re

def analyze():
    path = "yahoo_dump.html"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            
        pattern = r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>'
        m = re.search(pattern, content)
        if m:
            data = json.loads(m.group(1))
            item = data.get("props", {}).get("pageProps", {}).get("item", {})
            
            print(f"Axis Num: {item.get('axisNum')}")
            
            print("\n--- Spec List (Option Names) ---")
            spec_list = item.get('specList', [])
            print(json.dumps(spec_list, indent=2, ensure_ascii=False)[:500])
             
            print("\n--- Stock Table Two Axis ---")
            two_axis = item.get('stockTableTwoAxis', [])
            # Print first 2 items
            print(json.dumps(two_axis[:2], indent=2, ensure_ascii=False))

            print("\n--- Stock Table One Axis ---")
            one_axis = item.get('stockTableOneAxis', [])
            print(json.dumps(one_axis[:2], indent=2, ensure_ascii=False))
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    analyze()
