---
description: How to update scraping selectors when site structure changes
---

# Update Scraping Selectors Workflow

Use this workflow when Mercari or Yahoo Shopping stops returning data correctly.

## 1. Diagnose the Issue

// turbo
Check if scraping is failing by inspecting logs:
```bash
python verify_yahoo_fix.py
```

Look for:
- "LOW SUCCESS RATE" warnings in logs
- Empty titles/prices in results
- "Scrape health issues detected" warnings

## 2. Inspect the Target Site

Open the browser tool and navigate to the affected site:
- **Yahoo Search**: `https://shopping.yahoo.co.jp/search?p=nintendo+switch`
- **Yahoo Product**: `https://store.shopping.yahoo.co.jp/...`
- **Mercari Search**: `https://jp.mercari.com/search?keyword=...`
- **Mercari Product**: `https://jp.mercari.com/item/...`

Use browser DevTools or JavaScript to identify new selectors:
```javascript
// Find title element
document.querySelectorAll('h1, [class*="title"], [class*="name"]')

// Find price element  
document.querySelectorAll('[class*="price"]')
```

## 3. Update the Selector Configuration

Edit `config/scraping_selectors.json`:
- Add new selectors at the **beginning** of each array (higher priority)
- Keep old selectors as fallbacks
- Use partial matching: `[class*='styles_itemName']` instead of exact classes

## 4. Test the Changes

// turbo
```bash
python verify_yahoo_fix.py
```

## 5. Deploy

// turbo
```bash
git add config/scraping_selectors.json
git commit -m "Update scraping selectors for [site] structure change"
git push origin main
```
