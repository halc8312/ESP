from mercari_db import create_driver
from selenium.webdriver.common.by import By
import time

url = "https://jp.mercari.com/shops/product/xhF79ibHz3KwucSXev32C5"
driver = create_driver(headless=True)

try:
    driver.get(url)
    time.sleep(5)
    
    print("--- SIBLING ANALYSIS ---")
    
    # Check '種類' parent's sibling
    print("\n--- Analyze '種類' Layout ---")
    els = driver.find_elements(By.XPATH, "//*[contains(text(), '種類')]")
    for i, el in enumerate(els):
        if el.tag_name == 'script': continue
        print(f"Label Element [{i}] <{el.tag_name}>: {el.text}")
        try:
             parent = el.find_element(By.XPATH, "..")
             print(f"  Parent <{parent.tag_name}>")
             
             sibling = driver.execute_script("return arguments[0].nextElementSibling", parent)
             if sibling:
                 print(f"  Parent Sibling <{sibling.tag_name}> Text: {sibling.text.replace('\n', ' ')[:50]}...")
                 btns = sibling.find_elements(By.TAG_NAME, "button")
                 print(f"  Buttons in Sibling: {len(btns)}")
                 for b in btns:
                     print(f"    Button: {b.text.replace('\n', ' ')}")
             else:
                 print("  No Parent Sibling")
        except Exception as e:
             print(f"  Error: {e}")

    # Check 'カラー' parent's sibling
    print("\n--- Analyze 'カラー' Layout ---")
    els = driver.find_elements(By.XPATH, "//*[contains(text(), 'カラー')]")
    for i, el in enumerate(els):
        if el.tag_name == 'script': continue
        print(f"Label Element [{i}] <{el.tag_name}>: {el.text}")
        try:
             parent = el.find_element(By.XPATH, "..")
             print(f"  Parent <{parent.tag_name}>")
             
             sibling = driver.execute_script("return arguments[0].nextElementSibling", parent)
             if sibling:
                 print(f"  Parent Sibling <{sibling.tag_name}> Text: {sibling.text.replace('\n', ' ')[:50]}...")
                 btns = sibling.find_elements(By.TAG_NAME, "button")
                 print(f"  Buttons in Sibling: {len(btns)}")
                 for b in btns:
                     print(f"    Button: {b.text.replace('\n', ' ')}")
             else:
                 print("  No Parent Sibling")
        except Exception as e:
             print(f"  Error: {e}")

except Exception as e:
    print(f"Error: {e}")
finally:
    driver.quit()
