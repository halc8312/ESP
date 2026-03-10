"""
Mercari lightweight patrol scraper.
Playwright（Scrapling StealthyFetcher）を使用。
Stage 2で Selenium から移行。
"""
import re
import logging
from typing import Optional
from scrapling import StealthyFetcher

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.mercari")


class MercariPatrol(BasePatrol):
    """Lightweight Mercari price/stock scraper using Playwright."""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Playwright（StealthyFetcher）でメルカリの価格・在庫を取得。
        
        driver 引数は後方互換のために保持するが、使用しない。
        monitor_service.py の _BROWSER_SITES から "mercari" が削除された後は
        driver が渡されなくなる。
        """
        try:
            page = StealthyFetcher.fetch(
                url,
                headless=True,
                network_idle=True,  # JS ロード完了を待機
            )
            
            body_els = page.css("body")
            body_text = body_els[0].text if body_els else ""
            
            price = self._extract_price(page, body_text)
            status = self._extract_status(page, body_text)
            variants = self._extract_variants(page)
            
            return PatrolResult(
                price=price,
                status=status,
                variants=variants,
            )
            
        except Exception as e:
            logger.error(f"Patrol error for {url}: {e}")
            return PatrolResult(error=str(e))
    
    def _extract_price(self, page, body_text: str) -> Optional[int]:
        """Scrapling の CSS セレクタで価格を取得"""
        # data-testid='price' を優先
        try:
            price_nodes = page.css("[data-testid='price']")
            price_el = price_nodes[0] if price_nodes else None
            if price_el:
                price_text = price_el.text or ""
                m = re.search(r"[¥￥]\s*([\d,]+)", price_text) or re.search(r"([\d,]+)", price_text)
                if m:
                    try:
                        return int(m.group(1).replace(",", ""))
                    except ValueError:
                        pass
        except Exception:
            pass
        
        # フォールバック: body テキストから regex
        if body_text:
            m = re.search(r"[¥￥]\s*([\d,]+)", body_text)
            if m:
                try:
                    return int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        
        return None
    
    def _extract_status(self, page, body_text: str) -> str:
        """ページのテキストとボタン状態からステータスを判定"""
        if "売り切れ" in body_text or "Sold" in body_text:
            return "sold"
        
        # ボタンの状態チェック（Scrapling では attrib で確認）
        try:
            buttons = page.css("button")
            for btn in buttons:
                btn_text = (btn.text or "").lower()
                if "購入" in btn_text or "buy" in btn_text:
                    # disabled 属性の確認
                    disabled = btn.attrib.get("disabled")
                    aria_disabled = btn.attrib.get("aria-disabled", "false")
                    if disabled is None and aria_disabled != "true":
                        return "active"
                    else:
                        return "sold"
        except Exception:
            pass
        
        return "active" if body_text else "unknown"
    
    def _extract_variants(self, page) -> list:
        """
        メルカリShopsのバリエーション情報を取得。
        Scrapling の CSS セレクタと attrib を使用。
        """
        variants = []
        try:
            var_labels = page.css("[data-testid='variation-label']")
            for label in var_labels:
                name = label.text.strip() if label.text else ""
                
                # HTML 内容で売り切れを確認
                label_html = label.html or ""
                label_class = label.attrib.get("class", "")
                
                is_sold = "売り切れ" in label_html or "disabled" in label_class
                
                variants.append({
                    "name": name,
                    "stock": 0 if is_sold else 1,
                    "price": None,
                })
        except Exception:
            pass
            
        return variants


# Convenience function
def fetch_mercari(url: str, driver=None) -> PatrolResult:
    """Quick access to Mercari patrol."""
    return MercariPatrol().fetch(url, driver)
