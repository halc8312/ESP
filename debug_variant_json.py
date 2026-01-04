
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
import json
import time

def debug_json():
    url = "https://store.shopping.yahoo.co.jp/nissoplus/np-ql21.html"
    
    options = Options()
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    try:
        driver.get(url)
        time.sleep(2)
        
        script_el = driver.find_elements(By.ID, "__NEXT_DATA__")
        if script_el:
            json_str = script_el[0].get_attribute("innerHTML")
            data = json.loads(json_str)
            item = data.get("props", {}).get("pageProps", {}).get("item", {})
            
            print(f"Axis Num: {item.get('axisNum')}")
            
            print("\n--- Spec List ---")
            print(json.dumps(item.get("specList", []), indent=2, ensure_ascii=False))

            print("\n--- Stock Table Two Axis (KEYS) ---")
            two_axis = item.get("stockTableTwoAxis")
            if isinstance(two_axis, dict):
                print(f"Keys: {list(two_axis.keys())}")
                print(json.dumps(two_axis, indent=2, ensure_ascii=False)[:1000]) # First 1000 chars
            else:
                print(f"Type: {type(two_axis)}")
                
            print("\n--- Stock Table One Axis ---")
            one_axis = item.get("stockTableOneAxis")
            print(json.dumps(one_axis, indent=2, ensure_ascii=False)[:1000])

    finally:
        driver.quit()

if __name__ == "__main__":
    debug_json()
