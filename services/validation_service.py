"""
Validation Service

Checks products for issues that would prevent successful export to Shopify/eBay.
"""


def validate_product(product, snapshot=None):
    """
    Validate a single product and return list of issues.
    
    Args:
        product: Product model instance
        snapshot: Optional ProductSnapshot for image data
        
    Returns:
        List of issue dicts: [{"type": "error"|"warning", "message": "..."}]
    """
    issues = []
    
    # 1. No Title
    if not product.last_title or not product.last_title.strip():
        issues.append({
            "type": "error",
            "code": "no_title",
            "message": "タイトルがありません"
        })
    
    # 2. Title Too Long (eBay limit: 80 chars)
    if product.last_title and len(product.last_title) > 80:
        issues.append({
            "type": "warning",
            "code": "title_too_long",
            "message": f"タイトルが長すぎます ({len(product.last_title)}文字 > 80文字)"
        })
    
    # 3. No Price
    if not product.last_price or product.last_price <= 0:
        issues.append({
            "type": "warning",
            "code": "no_price",
            "message": "価格が設定されていません"
        })
    
    # 4. No Images
    has_images = False
    if snapshot and snapshot.image_urls:
        # image_urls is pipe-separated string
        images = [img for img in snapshot.image_urls.split("|") if img.strip()]
        has_images = len(images) > 0
    
    if not has_images:
        issues.append({
            "type": "error",
            "code": "no_images",
            "message": "画像がありません"
        })
    
    # 5. Negative Margin
    if product.selling_price and product.last_price:
        if product.selling_price < product.last_price:
            issues.append({
                "type": "warning",
                "code": "negative_margin",
                "message": f"利益がマイナスです (販売価格 {product.selling_price} < 仕入値 {product.last_price})"
            })
    
    return issues


def get_issue_summary(products_with_issues):
    """
    Get summary counts of issues.
    
    Args:
        products_with_issues: List of (product, issues) tuples
        
    Returns:
        Dict with counts: {"error_count": X, "warning_count": Y, "products_with_issues": Z}
    """
    error_count = 0
    warning_count = 0
    products_with_any_issue = 0
    
    for product, issues in products_with_issues:
        if issues:
            products_with_any_issue += 1
            for issue in issues:
                if issue["type"] == "error":
                    error_count += 1
                else:
                    warning_count += 1
    
    return {
        "error_count": error_count,
        "warning_count": warning_count,
        "products_with_issues": products_with_any_issue
    }
