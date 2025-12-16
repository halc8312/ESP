from mercari_db import create_driver
from selenium.webdriver.common.by import By
import time

url = "https://jp.mercari.com/shops/product/xhF79ibHz3KwucSXev32C5"
driver = create_driver(headless=True)

try:
    driver.get(url)
    time.sleep(5)
    
    print("--- CHILD ANALYSIS 2 ---")
    
    label_texts = ['種類', 'カラー']
    for label_text in label_texts:
        xpath = f"//*[contains(text(), '{label_text}')]"
        labels = driver.find_elements(By.XPATH, xpath)
        for label in labels:
            if label.tag_name in ['script', 'style']: continue
            if len(label.text) > 30: continue # Skip long blocks
            
            print(f"Checking Label: '{label.text}' ({label.tag_name})")
            
            try:
                parent = label.find_element(By.XPATH, "..")
                print(f"  Parent: <{parent.tag_name}> class='{parent.get_attribute('class')}'")
                
                sibling = driver.execute_script("return arguments[0].nextElementSibling", parent)
                if sibling:
                    text_preview = sibling.text.replace('\n', ' ')[:50]
                    print(f"  Sibling found (<{sibling.tag_name}>). Text: {text_preview}...")
                    
                    # Check children
                    children = sibling.find_elements(By.XPATH, "./*")
                    print(f"  Direct Children count: {len(children)}")
                    for i, child in enumerate(children):
                        print(f"    [{i}] <{child.tag_name}>: {child.text.replace('\n',' ')}")
                        
                else:
                    print("  NO SIBLING found.")

            except Exception as e:
                print(f"  Error: {e}")

except Exception as e:
    print(f"Error: {e}")
finally:
    driver.quit()
