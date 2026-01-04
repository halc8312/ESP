"""
Main routes: index and dashboard.
"""
from flask import Blueprint, render_template, request, session, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy.orm import subqueryload
from sqlalchemy import func

from database import SessionLocal
from models import Shop, Product, Variant
from services.validation_service import validate_product, get_issue_summary

main_bp = Blueprint('main', __name__)

PAGE_SIZE = 50


@main_bp.route("/dashboard")
@login_required
def dashboard():
    session_db = SessionLocal()
    try:
        current_shop_id = session.get('current_shop_id')
        
        # Base query for user's products
        base_query = session_db.query(Product).filter(Product.user_id == current_user.id)
        if current_shop_id:
            base_query = base_query.filter(Product.shop_id == current_shop_id)

        # 1. Total Count
        total_items = base_query.count()

        # 2. Status Counts
        # func.count(Product.id) is cleaner, grouping by status
        status_counts = (
            session_db.query(Product.last_status, func.count(Product.id))
            .filter(Product.user_id == current_user.id)
        )
        if current_shop_id:
            status_counts = status_counts.filter(Product.shop_id == current_shop_id)
        
        status_counts = status_counts.group_by(Product.last_status).all()
        # Convert to dict for easy access: {'active': 10, 'sold': 2, ...}
        status_map = {s[0]: s[1] for s in status_counts}

        # 3. Sold Out / Low Stock Variants
        # This is a bit complex. We want to find variants with qty=0 linked to our products.
        # Join Product and Variant
        sold_out_query = (
            session_db.query(func.count(Variant.id))
            .join(Product)
            .filter(Product.user_id == current_user.id)
            .filter(Variant.inventory_qty == 0)
        )
        if current_shop_id:
            sold_out_query = sold_out_query.filter(Product.shop_id == current_shop_id)
        
        sold_out_count = sold_out_query.scalar()

        # 4. Recent Activity (Last 5 updated) with validation
        recent_items = (
            base_query
            .options(subqueryload(Product.snapshots))
            .order_by(Product.updated_at.desc())
            .limit(5)
            .all()
        )
        
        # 5. Validate recent items and count issues
        products_with_issues = []
        for p in recent_items:
            snapshot = p.snapshots[0] if p.snapshots else None
            issues = validate_product(p, snapshot)
            p.validation_issues = issues  # Attach to product for template access
            products_with_issues.append((p, issues))
        
        validation_summary = get_issue_summary(products_with_issues)

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()

        return render_template(
            "dashboard.html",
            total_items=total_items,
            status_map=status_map,
            sold_out_count=sold_out_count,
            recent_items=recent_items,
            validation_summary=validation_summary,
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()



@main_bp.route("/")
@login_required
def index():
    session_db = SessionLocal()
    try:
        page = int(request.args.get("page", 1))
        selected_site = request.args.get("site")
        selected_status = request.args.get("status")
        selected_change_filter = request.args.get("change_filter")

        # Filter query by user_id and exclude archived
        base_query = session_db.query(Product).filter(
            Product.user_id == current_user.id,
            Product.archived != True  # Exclude archived products
        )

        sites = [s[0] for s in base_query.with_entities(Product.site).distinct().all()]
        statuses = [s[0] for s in base_query.with_entities(Product.last_status).distinct().all()]
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        query = base_query
        if current_shop_id:
            query = query.filter(Product.shop_id == current_shop_id)
        if selected_site:
            query = query.filter(Product.site == selected_site)
        if selected_status:
            query = query.filter(Product.last_status == selected_status)

        all_products = query.options(subqueryload(Product.snapshots)).order_by(Product.updated_at.desc()).all()

        products_to_display = []
        for p in all_products:
            p.has_changed = False
            if len(p.snapshots) >= 2:
                sorted_snapshots = sorted(p.snapshots, key=lambda s: s.scraped_at, reverse=True)
                latest = sorted_snapshots[0]
                previous = sorted_snapshots[1]
                if latest.price != previous.price or latest.status != previous.status:
                    p.has_changed = True
            
            if selected_change_filter == 'changed':
                if p.has_changed:
                    products_to_display.append(p)
            else:
                products_to_display.append(p)

        total_items = len(products_to_display)
        total_pages = (total_items + PAGE_SIZE - 1) // PAGE_SIZE
        offset = (page - 1) * PAGE_SIZE
        paginated_products = products_to_display[offset : offset + PAGE_SIZE]

        has_prev = page > 1
        has_next = page < total_pages

        defaults = {
            "markup": request.args.get("markup", "1.2"),
            "qty": request.args.get("qty", "1"),
            "rate": request.args.get("rate", "155"),
            "ebay_category_id": request.args.get("ebay_category_id", ""),
            "ebay_condition_id": request.args.get("ebay_condition_id", "3000"),
            "ebay_payment_profile": request.args.get("ebay_payment_profile", ""),
            "ebay_return_profile": request.args.get("ebay_return_profile", ""),
            "ebay_shipping_profile": request.args.get("ebay_shipping_profile", ""),
            "ebay_paypal_email": request.args.get("ebay_paypal_email", ""),
        }

        return render_template(
            "index.html",
            products=paginated_products,
            sites=sites,
            statuses=statuses,
            selected_site=selected_site,
            selected_status=selected_status,
            selected_change_filter=selected_change_filter,
            page=page,
            total_pages=total_pages,
            has_prev=has_prev,
            has_next=has_next,
            default_markup=defaults["markup"],
            default_qty=defaults["qty"],
            default_rate=defaults["rate"],
            default_ebay_category_id=defaults["ebay_category_id"],
            default_ebay_condition_id=defaults["ebay_condition_id"],
            default_ebay_payment_profile=defaults["ebay_payment_profile"],
            default_ebay_return_profile=defaults["ebay_return_profile"],
            default_ebay_shipping_profile=defaults["ebay_shipping_profile"],
            default_ebay_paypal_email=defaults["ebay_paypal_email"],
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()


@main_bp.route("/batch-edit", methods=["POST"])
@login_required
def batch_edit():
    """Handle batch title editing operations."""
    action = request.form.get("action")
    input_value = request.form.get("input", "")
    input2_value = request.form.get("input2", "")
    product_ids = request.form.getlist("ids")
    
    if not product_ids or not action:
        return redirect(url_for('main.index'))
    
    session_db = SessionLocal()
    try:
        # Get products owned by current user
        products = session_db.query(Product).filter(
            Product.id.in_([int(pid) for pid in product_ids]),
            Product.user_id == current_user.id
        ).all()
        
        updated_count = 0
        for product in products:
            original_title = product.custom_title or product.last_title or ""
            new_title = original_title
            
            if action == "prefix":
                new_title = input_value + original_title
            elif action == "suffix":
                new_title = original_title + input_value
            elif action == "replace":
                new_title = original_title.replace(input_value, input2_value)
            
            if new_title != original_title:
                product.custom_title = new_title
                updated_count += 1
        
        session_db.commit()
        
        # Flash message would be ideal, but redirect with success param works too
        return redirect(url_for('main.index'))
    except Exception as e:
        session_db.rollback()
        print(f"Batch edit error: {e}")
        return redirect(url_for('main.index'))
    finally:
        session_db.close()

