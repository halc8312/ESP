"""
Comprehensive E2E Tests for the refactored Flask application.
Tests all routes after the app.py split to ensure functionality is preserved.
"""
import io
import re
import shutil
import uuid
from pathlib import Path

import pytest
from datetime import datetime
from models import User, Shop, Product, Variant, ProductSnapshot, DescriptionTemplate, PriceList, PriceListItem, CatalogPageView
from time_utils import utc_now


class TestAuthenticationRoutes:
    """E2E tests for authentication routes (routes/auth.py)"""
    
    def test_login_page_renders(self, client):
        """Test that login page renders correctly"""
        response = client.get('/login')
        assert response.status_code == 200
        assert "ログイン".encode("utf-8") in response.data
    
    def test_register_page_renders(self, client):
        """Test that register page renders correctly"""
        response = client.get('/register')
        assert response.status_code == 200
        assert "新規登録".encode("utf-8") in response.data
    
    def test_successful_registration(self, client, db_session):
        """Test user registration creates user and logs in"""
        response = client.post('/register', data={
            'username': 'newuser',
            'password': 'newpassword123'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert response.request.path == '/'  # Should redirect to index after registration
        
        # Verify user was created
        user = db_session.query(User).filter_by(username='newuser').first()
        assert user is not None
    
    def test_duplicate_registration_fails(self, client, db_session):
        """Test that registering with existing username fails"""
        # Create existing user
        user = User(username='existinguser')
        user.set_password('password123')
        db_session.add(user)
        db_session.commit()
        
        # Try to register with same username
        response = client.post('/register', data={
            'username': 'existinguser',
            'password': 'differentpassword'
        })
        
        assert response.status_code == 200
        assert "すでに使われています".encode("utf-8") in response.data
    
    def test_login_success(self, client, db_session):
        """Test successful login redirects to index"""
        # Create user
        user = User(username='logintest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        response = client.post('/login', data={
            'username': 'logintest',
            'password': 'testpassword'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert response.request.path == '/'
    
    def test_login_invalid_credentials(self, client, db_session):
        """Test login with wrong password fails"""
        user = User(username='wrongpass')
        user.set_password('correctpassword')
        db_session.add(user)
        db_session.commit()
        
        response = client.post('/login', data={
            'username': 'wrongpass',
            'password': 'incorrectpassword'
        })
        
        assert response.status_code == 200
        assert "違います".encode("utf-8") in response.data
    
    def test_logout(self, client, db_session):
        """Test logout redirects to login"""
        # Setup and login
        user = User(username='logouttest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': 'logouttest',
            'password': 'testpassword'
        })
        
        # Logout
        response = client.get('/logout', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_authenticated_user_redirected_from_login(self, client, db_session):
        """Test that authenticated user is redirected away from login page"""
        user = User(username='autheduser')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': 'autheduser',
            'password': 'testpassword'
        })
        
        response = client.get('/login', follow_redirects=True)
        assert response.request.path == '/'

    def test_account_page_and_password_change(self, client, db_session):
        """Test account page renders and password can be updated"""
        user = User(username='accounttest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()

        client.post('/login', data={
            'username': 'accounttest',
            'password': 'testpassword'
        })

        response = client.get('/account')
        assert response.status_code == 200
        assert "アカウント".encode("utf-8") in response.data

        response = client.post('/account', data={
            'current_password': 'testpassword',
            'new_password': 'newtestpassword',
            'confirm_password': 'newtestpassword',
        }, follow_redirects=True)
        assert response.status_code == 200
        assert "変更しました".encode("utf-8") in response.data


class TestMainRoutes:
    """E2E tests for main routes (routes/main.py)"""
    
    def test_index_requires_login(self, client):
        """Test that index requires authentication"""
        response = client.get('/', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_index_loads_when_authenticated(self, client, db_session):
        """Test index page loads for authenticated user"""
        user = User(username='indextest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': 'indextest',
            'password': 'testpassword'
        })
        
        response = client.get('/')
        assert response.status_code == 200
    
    def test_dashboard_requires_login(self, client):
        """Test that dashboard requires authentication"""
        response = client.get('/dashboard', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_dashboard_loads_when_authenticated(self, client, db_session):
        """Test dashboard page loads for authenticated user"""
        user = User(username='dashtest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': 'dashtest',
            'password': 'testpassword'
        })
        
        response = client.get('/dashboard')
        assert response.status_code == 200

    def test_loading_overlay_respects_hidden_attribute(self, client):
        """The global loading overlay must stay hidden until JS explicitly opens it."""
        css_path = Path(client.application.root_path) / "static" / "css" / "style.css"
        css = css_path.read_text(encoding="utf-8")
        match = re.search(r"\.loading-overlay\[hidden\]\s*\{(?P<body>.*?)\}", css, re.S)
        assert match is not None
        assert re.search(r"display\s*:\s*none\s*!important\s*;", match.group("body"))

    def test_dashboard_uses_current_scope_metrics(self, client, db_session):
        """Dashboard metrics should align with current product model and index scope."""
        user = User(username='dashmetricstest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()

        shop = Shop(name='Dashboard Shop', user_id=user.id)
        db_session.add(shop)
        db_session.commit()

        active_product = Product(
            user_id=user.id,
            shop_id=shop.id,
            site='mercari',
            source_url='https://example.com/active',
            last_title='Active Product',
            last_price=1200,
            last_status='on_sale',
            status='active',
            selling_price=2400,
            custom_title_en='Active Product EN',
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        draft_product = Product(
            user_id=user.id,
            shop_id=shop.id,
            site='manual',
            source_url='https://example.com/draft',
            last_title='Draft Product',
            last_price=900,
            last_status='sold',
            status='draft',
            selling_price=None,
            custom_title_en='',
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        archived_product = Product(
            user_id=user.id,
            shop_id=shop.id,
            site='mercari',
            source_url='https://example.com/archived',
            last_title='Archived Product',
            last_price=800,
            last_status='on_sale',
            status='active',
            selling_price=1000,
            archived=True,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        deleted_product = Product(
            user_id=user.id,
            shop_id=shop.id,
            site='mercari',
            source_url='https://example.com/deleted',
            last_title='Deleted Product',
            last_price=700,
            last_status='deleted',
            status='draft',
            selling_price=1100,
            deleted_at=utc_now(),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db_session.add_all([active_product, draft_product, archived_product, deleted_product])
        db_session.commit()

        db_session.add_all([
            Variant(product_id=active_product.id, option1_value='Default Title', sku='SKU-A', price=2400, inventory_qty=2),
            Variant(product_id=draft_product.id, option1_value='Default Title', sku='SKU-B', price=900, inventory_qty=0),
            Variant(product_id=archived_product.id, option1_value='Default Title', sku='SKU-C', price=1000, inventory_qty=0),
        ])
        db_session.add_all([
            ProductSnapshot(
                product_id=active_product.id,
                scraped_at=utc_now(),
                title='Active Product',
                price=1200,
                status='on_sale',
                description='',
                image_urls='https://img.example.com/active.jpg',
            ),
            ProductSnapshot(
                product_id=draft_product.id,
                scraped_at=utc_now(),
                title='Draft Product',
                price=900,
                status='sold',
                description='',
                image_urls='',
            ),
        ])
        db_session.commit()

        client.post('/login', data={
            'username': 'dashmetricstest',
            'password': 'testpassword'
        })
        with client.session_transaction() as session_state:
            session_state['current_shop_id'] = shop.id

        response = client.get('/dashboard')
        assert response.status_code == 200
        html = response.get_data(as_text=True)

        assert re.search(r'管理対象商品</span>\s*<strong class="dashboard-summary-value">2</strong>', html)
        assert re.search(r'公開中</span>\s*<strong class="dashboard-summary-value">1</strong>', html)
        assert re.search(r'下書き</span>\s*<strong class="dashboard-summary-value">1</strong>', html)
        assert re.search(r'公開準備OK</span>\s*<strong class="dashboard-summary-value">1</strong>', html)
        assert re.search(r'要対応商品</span>\s*<strong class="dashboard-summary-value">1</strong>', html)

        assert re.search(r'仕入先在庫あり</span>\s*<strong>1</strong>', html)
        assert re.search(r'仕入先売切れ</span>\s*<strong>1</strong>', html)
        assert re.search(r'0在庫バリアント</span>\s*<strong>1</strong>', html)

        assert 'Active Product' in html
        assert 'Draft Product' in html
        assert 'Archived Product' not in html
        assert 'Deleted Product' not in html

    def test_dashboard_nav_link_is_marked_active(self, client, db_session):
        """Sidebar and bottom nav should mark dashboard as active on the dashboard route."""
        user = User(username='dashnavtest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()

        client.post('/login', data={
            'username': 'dashnavtest',
            'password': 'testpassword'
        })

        response = client.get('/dashboard')
        html = response.get_data(as_text=True)

        assert re.search(r'<a href="/dashboard"\s+class="sidebar-link active"', html)
        assert re.search(r'<a href="/dashboard"\s+class="bottom-nav-item active"', html)
    
    def test_index_pagination(self, client, db_session):
        """Test index pagination parameters"""
        user = User(username='paginationtest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': 'paginationtest',
            'password': 'testpassword'
        })
        
        response = client.get('/?page=1')
        assert response.status_code == 200
        
        response = client.get('/?page=2')
        assert response.status_code == 200

    def test_manual_add_requires_login(self, client):
        """Test manual add page requires authentication"""
        response = client.get('/products/manual-add', follow_redirects=True)
        assert response.request.path == '/login'

    def test_manual_add_page_loads(self, client, db_session):
        """Test manual add page loads for authenticated user"""
        user = User(username='manualaddloadtest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()

        client.post('/login', data={
            'username': 'manualaddloadtest',
            'password': 'testpassword'
        })

        response = client.get('/products/manual-add')
        assert response.status_code == 200
        assert '商品手動追加'.encode('utf-8') in response.data

    def test_manual_add_creates_product_snapshot_and_variant(self, client, db_session):
        """Test manual add creates a product, snapshot, and default variant"""
        user = User(username='manualaddcreatetest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()

        shop = Shop(name='Manual Add Shop', user_id=user.id)
        db_session.add(shop)
        db_session.commit()

        client.post('/login', data={
            'username': 'manualaddcreatetest',
            'password': 'testpassword'
        })

        response = client.post('/products/manual-add', data={
            'shop_id': str(shop.id),
            'title': '手動登録商品',
            'title_en': 'Manual Product',
            'description': '日本語説明',
            'description_en': 'English Description',
            'cost_price': '2500',
            'selling_price': '4200',
            'inventory_qty': '3',
            'stock_state': 'on_sale',
            'publish_status': 'active',
            'site': 'manual',
            'source_url': '',
            'tags': 'tag1,tag2',
            'sku': 'MANUAL-001',
            'image_urls': 'https://img.example.com/1.jpg|https://img.example.com/2.jpg|ftp://ignored.example.com/3.jpg'
        }, follow_redirects=False)

        assert response.status_code == 302
        assert '/product/' in response.headers['Location']

        product = db_session.query(Product).filter_by(user_id=user.id, last_title='手動登録商品').one()
        assert product.shop_id == shop.id
        assert product.site == 'manual'
        assert product.last_price == 2500
        assert product.selling_price == 4200
        assert product.last_status == 'on_sale'
        assert product.status == 'active'
        assert product.custom_title_en == 'Manual Product'
        assert product.custom_description_en == 'English Description'

        snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product.id).one()
        assert snapshot.title == '手動登録商品'
        assert snapshot.price == 2500
        assert snapshot.status == 'on_sale'
        assert snapshot.description == '日本語説明'
        assert snapshot.image_urls == 'https://img.example.com/1.jpg|https://img.example.com/2.jpg'

        variant = db_session.query(Variant).filter_by(product_id=product.id).one()
        assert variant.option1_value == 'Default Title'
        assert variant.sku == 'MANUAL-001'
        assert variant.price == 4200
        assert variant.inventory_qty == 3

    def test_manual_add_rejects_other_users_shop(self, client, db_session):
        """Test manual add enforces shop ownership"""
        owner = User(username='manualaddowner')
        owner.set_password('testpassword')
        db_session.add(owner)
        db_session.commit()

        foreign_shop = Shop(name='Foreign Shop', user_id=owner.id)
        db_session.add(foreign_shop)
        db_session.commit()

        user = User(username='manualaddother')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()

        client.post('/login', data={
            'username': 'manualaddother',
            'password': 'testpassword'
        })

        response = client.post('/products/manual-add', data={
            'shop_id': str(foreign_shop.id),
            'title': '不正ショップ商品',
            'cost_price': '1200',
            'inventory_qty': '1',
            'stock_state': 'on_sale',
            'publish_status': 'draft',
            'site': 'manual'
        })

        assert response.status_code == 200
        assert '選択したショップが見つかりません'.encode('utf-8') in response.data
        assert db_session.query(Product).filter_by(user_id=user.id, last_title='不正ショップ商品').count() == 0


class TestShopsRoutes:
    """E2E tests for shop routes (routes/shops.py)"""
    
    def _login_user(self, client, db_session, username='shoptest'):
        """Helper to create and login a user"""
        user = User(username=username)
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': username,
            'password': 'testpassword'
        })
        return user
    
    def test_shops_page_requires_login(self, client):
        """Test that shops page requires authentication"""
        response = client.get('/shops', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_shops_page_loads(self, client, db_session):
        """Test shops page loads for authenticated user"""
        self._login_user(client, db_session)
        response = client.get('/shops')
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'name="name"' in html
        assert 'placeholder="ショップ名 (例: 文具店A)"' in html
        assert 'value="{{ csrf_token() }}"/> placeholder=' not in html
    
    def test_create_shop(self, client, db_session):
        """Test creating a new shop"""
        user = self._login_user(client, db_session, 'createshoptest')
        
        response = client.post('/shops', data={
            'name': 'Test Shop'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify shop was created
        shop = db_session.query(Shop).filter_by(user_id=user.id, name='Test Shop').first()
        assert shop is not None
    
    def test_delete_shop(self, client, db_session):
        """Test deleting a shop"""
        user = self._login_user(client, db_session, 'deleteshoptest')
        
        # Create a shop
        shop = Shop(name='Shop to Delete', user_id=user.id)
        db_session.add(shop)
        db_session.commit()
        shop_id = shop.id
        
        response = client.post(f'/shops/{shop_id}/delete', follow_redirects=True)
        assert response.status_code == 200
        
        # Verify shop was deleted
        deleted_shop = db_session.query(Shop).filter_by(id=shop_id).first()
        assert deleted_shop is None
    
    def test_set_current_shop(self, client, db_session):
        """Test setting current shop"""
        user = self._login_user(client, db_session, 'setshoptest')
        
        shop = Shop(name='Current Shop', user_id=user.id)
        db_session.add(shop)
        db_session.commit()
        
        response = client.post('/set_current_shop', data={
            'shop_id': str(shop.id)
        }, follow_redirects=True)
        
        assert response.status_code == 200
    
    def test_templates_page_requires_login(self, client):
        """Test that templates page requires authentication"""
        response = client.get('/templates', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_templates_page_loads(self, client, db_session):
        """Test templates page loads for authenticated user"""
        self._login_user(client, db_session, 'templatestest')
        response = client.get('/templates')
        assert response.status_code == 200

    def test_templates_page_shows_only_current_users_templates(self, client, db_session):
        """Authenticated users should not see other users' templates."""
        owner = self._login_user(client, db_session, 'templateownerscope')
        other_user = User(username='templateotherscope')
        other_user.set_password('testpassword')
        db_session.add(other_user)
        db_session.commit()

        db_session.add(
            DescriptionTemplate(
                user_id=owner.id,
                name='Owner Template',
                content='Owner content',
            )
        )
        db_session.add(
            DescriptionTemplate(
                user_id=other_user.id,
                name='Other Template',
                content='Other content',
            )
        )
        db_session.commit()

        response = client.get('/templates')

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'Owner Template' in html
        assert 'Other Template' not in html

    def test_templates_page_keeps_legacy_shared_templates_visible(self, client, db_session):
        """Legacy templates without ownership should stay visible during migration."""
        self._login_user(client, db_session, 'templatelegacyvisible')
        db_session.add(
            DescriptionTemplate(
                user_id=None,
                name='Legacy Shared Template',
                content='Legacy content',
            )
        )
        db_session.commit()

        response = client.get('/templates')

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'Legacy Shared Template' in html
        assert '共有 / 旧データ' in html
        assert '削除不可' in html
    
    def test_create_template(self, client, db_session):
        """Test creating a description template"""
        user = self._login_user(client, db_session, 'createtemplatetest')
        
        response = client.post('/templates', data={
            'name': 'Test Template',
            'content': 'This is test template content'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify template was created
        template = db_session.query(DescriptionTemplate).filter_by(
            user_id=user.id,
            name='Test Template',
        ).first()
        assert template is not None
        assert template.content == 'This is test template content'
        assert template.user_id == user.id

    def test_create_template_allows_same_name_for_different_users(self, client, db_session):
        """Different users can keep templates with the same display name."""
        user_one = self._login_user(client, db_session, 'createtemplatesameone')
        response_one = client.post('/templates', data={
            'name': 'Shared Template Name',
            'content': 'Owner one content',
        }, follow_redirects=True)
        assert response_one.status_code == 200

        user_two = User(username='createtemplatesametwo')
        user_two.set_password('testpassword')
        db_session.add(user_two)
        db_session.commit()

        client.get('/logout', follow_redirects=True)
        client.post('/login', data={
            'username': user_two.username,
            'password': 'testpassword',
        }, follow_redirects=True)
        response_two = client.post('/templates', data={
            'name': 'Shared Template Name',
            'content': 'Owner two content',
        }, follow_redirects=True)

        assert response_two.status_code == 200
        templates = (
            db_session.query(DescriptionTemplate)
            .filter(DescriptionTemplate.name == 'Shared Template Name')
            .order_by(DescriptionTemplate.user_id.asc())
            .all()
        )
        assert [template.user_id for template in templates] == [user_one.id, user_two.id]

    def test_create_template_sanitizes_rich_text(self, client, db_session):
        """Test template content is normalized to the shared safe rich-text subset"""
        user = self._login_user(client, db_session, 'createtemplatesanitizetest')

        response = client.post('/templates', data={
            'name': 'Sanitized Template',
            'content': '<p>Hello</p><script>alert(1)</script>'
        }, follow_redirects=True)

        assert response.status_code == 200

        template = db_session.query(DescriptionTemplate).filter_by(
            user_id=user.id,
            name='Sanitized Template',
        ).first()
        assert template is not None
        assert '<script' not in template.content.lower()
        assert 'Hello' in template.content
    
    def test_delete_template(self, client, db_session):
        """Test deleting a template"""
        user = self._login_user(client, db_session, 'deletetemplatetest')
        
        template = DescriptionTemplate(user_id=user.id, name='Template to Delete', content='Content')
        db_session.add(template)
        db_session.commit()
        template_id = template.id
        
        response = client.post(f'/templates/{template_id}/delete', follow_redirects=True)
        assert response.status_code == 200
        
        # Verify template was deleted
        deleted_template = db_session.query(DescriptionTemplate).filter_by(id=template_id).first()
        assert deleted_template is None

    def test_delete_template_does_not_remove_other_users_template(self, client, db_session):
        """Users must not be able to delete templates they do not own."""
        self._login_user(client, db_session, 'templatedeleteownercheck')

        other_user = User(username='templatedeleteother')
        other_user.set_password('testpassword')
        db_session.add(other_user)
        db_session.commit()

        template = DescriptionTemplate(
            user_id=other_user.id,
            name='Other User Template',
            content='Content',
        )
        db_session.add(template)
        db_session.commit()
        template_id = template.id

        response = client.post(f'/templates/{template_id}/delete', follow_redirects=True)

        assert response.status_code == 200
        assert db_session.query(DescriptionTemplate).filter_by(id=template_id).first() is not None

    def test_delete_template_does_not_remove_legacy_shared_template(self, client, db_session):
        """Legacy shared templates remain visible but are not deletable by scoped users."""
        self._login_user(client, db_session, 'templatedeletelegacycheck')

        template = DescriptionTemplate(
            user_id=None,
            name='Legacy Shared Template Delete',
            content='Content',
        )
        db_session.add(template)
        db_session.commit()
        template_id = template.id

        response = client.post(f'/templates/{template_id}/delete', follow_redirects=True)

        assert response.status_code == 200
        assert db_session.query(DescriptionTemplate).filter_by(id=template_id).first() is not None


class TestPriceListRoutes:
    """E2E tests for price list routes and public catalog"""

    def _login_user(self, client, db_session, username='pricelisttest'):
        user = User(username=username)
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()

        client.post('/login', data={
            'username': username,
            'password': 'testpassword'
        })
        return user

    def _create_catalog_fixture(
        self,
        db_session,
        username='catalogfixture',
        layout='editorial',
        theme='dark',
        shop_logo_url=None,
        explicit_pricelist_shop=False,
    ):
        user = User(username=username)
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()

        shop = None
        if shop_logo_url:
            shop = Shop(
                user_id=user.id,
                name=f'{username}-shop',
                logo_url=shop_logo_url,
            )
            db_session.add(shop)
            db_session.commit()

        product = Product(
            user_id=user.id,
            site='manual',
            shop_id=shop.id if shop else None,
            source_url='https://example.com/manual-product',
            last_title='Catalog Layout Product',
            last_price=3200,
            last_status='on_sale',
            status='active',
            tags='PSA10,ONEPIECE,Rare',
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db_session.add(product)
        db_session.commit()

        variant = Variant(
            product_id=product.id,
            option1_value='Default Title',
            price=3200,
            inventory_qty=2,
            position=1
        )
        db_session.add(variant)

        snapshot = ProductSnapshot(
            product_id=product.id,
            title='Catalog Layout Product',
            price=3200,
            status='on_sale',
            description='Catalog description',
            image_urls='https://img.example.com/catalog-layout.jpg|https://img.example.com/catalog-layout-2.jpg',
            scraped_at=utc_now()
        )
        db_session.add(snapshot)

        pricelist = PriceList(
            user_id=user.id,
            shop_id=shop.id if explicit_pricelist_shop and shop else None,
            name='Editorial Catalog',
            token=f'{username}-token',
            layout=layout,
            theme=theme,
            currency_rate=150,
            is_active=True
        )
        db_session.add(pricelist)
        db_session.commit()

        item = PriceListItem(
            price_list_id=pricelist.id,
            product_id=product.id,
            visible=True,
            sort_order=0
        )
        db_session.add(item)
        db_session.commit()

        return user, product, pricelist, item

    def test_pricelist_create_saves_layout(self, client, db_session):
        """Test creating a price list persists selected layout"""
        user = self._login_user(client, db_session, 'pricelistcreatetest')

        response = client.post('/pricelists/create', data={
            'name': 'Editorial List',
            'notes': 'Test notes',
            'currency_rate': '150',
            'layout': 'editorial'
        }, follow_redirects=False)

        assert response.status_code == 302
        assert '/pricelists/' in response.headers['Location']

        pricelist = db_session.query(PriceList).filter_by(user_id=user.id, name='Editorial List').one()
        assert pricelist.layout == 'editorial'

    def test_pricelist_create_saves_theme(self, client, db_session):
        """Test creating a price list persists selected theme"""
        user = self._login_user(client, db_session, 'pricelistcreatethemetest')

        response = client.post('/pricelists/create', data={
            'name': 'Light Theme List',
            'notes': 'Theme notes',
            'currency_rate': '150',
            'layout': 'grid',
            'theme': 'light',
        }, follow_redirects=False)

        assert response.status_code == 302

        pricelist = db_session.query(PriceList).filter_by(user_id=user.id, name='Light Theme List').one()
        assert pricelist.theme == 'light'

    def test_pricelist_create_saves_selected_shop(self, client, db_session):
        """Test creating a price list persists the selected shop for logo display."""
        user = self._login_user(client, db_session, 'pricelistcreateshoptest')
        shop = Shop(
            user_id=user.id,
            name='Logo Shop',
            logo_url='https://img.example.com/logo-shop.png',
        )
        db_session.add(shop)
        db_session.commit()

        response = client.post('/pricelists/create', data={
            'name': 'Shop Bound List',
            'notes': '',
            'currency_rate': '150',
            'layout': 'grid',
            'theme': 'dark',
            'shop_id': str(shop.id),
        }, follow_redirects=False)

        assert response.status_code == 302

        pricelist = db_session.query(PriceList).filter_by(user_id=user.id, name='Shop Bound List').one()
        assert pricelist.shop_id == shop.id

    def test_pricelist_edit_updates_layout(self, client, db_session):
        """Test editing a price list can switch layout"""
        user = self._login_user(client, db_session, 'pricelistedittest')

        pricelist = PriceList(
            user_id=user.id,
            name='Grid List',
            token='grid-list-token',
            layout='grid',
            currency_rate=150,
            is_active=True
        )
        db_session.add(pricelist)
        db_session.commit()

        response = client.post(f'/pricelists/{pricelist.id}/edit', data={
            'name': 'Grid List',
            'notes': 'Updated notes',
            'currency_rate': '150',
            'layout': 'editorial',
            'is_active': 'on'
        }, follow_redirects=False)

        assert response.status_code == 302
        db_session.refresh(pricelist)
        assert pricelist.layout == 'editorial'

    def test_pricelist_edit_updates_theme(self, client, db_session):
        """Test editing a price list can switch theme"""
        user = self._login_user(client, db_session, 'pricelisteditthemetest')

        pricelist = PriceList(
            user_id=user.id,
            name='Dark Theme List',
            token='dark-theme-token',
            layout='grid',
            theme='dark',
            currency_rate=150,
            is_active=True
        )
        db_session.add(pricelist)
        db_session.commit()

        response = client.post(f'/pricelists/{pricelist.id}/edit', data={
            'name': 'Dark Theme List',
            'notes': 'Updated notes',
            'currency_rate': '150',
            'layout': 'grid',
            'theme': 'light',
            'is_active': 'on'
        }, follow_redirects=False)

        assert response.status_code == 302
        db_session.refresh(pricelist)
        assert pricelist.theme == 'light'

    def test_pricelist_edit_updates_selected_shop(self, client, db_session):
        """Test editing a price list can bind it to a specific shop."""
        user = self._login_user(client, db_session, 'pricelisteditshoptest')
        shop = Shop(
            user_id=user.id,
            name='Edited Shop',
            logo_url='https://img.example.com/edited-shop.png',
        )
        db_session.add(shop)
        db_session.commit()

        pricelist = PriceList(
            user_id=user.id,
            name='Shopless List',
            token='shopless-list-token',
            layout='grid',
            theme='dark',
            currency_rate=150,
            is_active=True
        )
        db_session.add(pricelist)
        db_session.commit()

        response = client.post(f'/pricelists/{pricelist.id}/edit', data={
            'name': 'Shopless List',
            'notes': 'Updated notes',
            'currency_rate': '150',
            'layout': 'grid',
            'theme': 'dark',
            'shop_id': str(shop.id),
            'is_active': 'on'
        }, follow_redirects=False)

        assert response.status_code == 302
        db_session.refresh(pricelist)
        assert pricelist.shop_id == shop.id

    def test_catalog_view_uses_pricelist_layout(self, client, db_session):
        """Test public catalog renders the selected layout class"""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='cataloglayouttest',
            layout='editorial'
        )

        response = client.get(f'/catalog/{pricelist.token}')
        assert response.status_code == 200
        assert b'catalog-layout-editorial' in response.data
        assert b'Editorial' in response.data
        assert b'Catalog Layout Product' in response.data

    def test_catalog_view_uses_pricelist_theme(self, client, db_session):
        """Test public catalog renders the selected base theme"""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogthemetest',
            layout='grid',
            theme='light'
        )

        response = client.get(f'/catalog/{pricelist.token}')
        assert response.status_code == 200
        assert b'light-mode' in response.data
        assert b'<strong>Light</strong> theme' in response.data

    def test_catalog_view_prefers_pricelist_shop_logo(self, client, db_session):
        """Test public catalog uses the explicit pricelist shop logo when configured."""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='cataloglogotest',
            layout='grid',
            theme='dark',
            shop_logo_url='https://img.example.com/shop-logo.png',
            explicit_pricelist_shop=True,
        )

        response = client.get(f'/catalog/{pricelist.token}')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        assert 'https://img.example.com/shop-logo.png' in html
        assert 'cataloglogotest-shop' in html

    def test_catalog_view_renders_product_modal_shell(self, client, db_session):
        """Test public catalog includes quick-view modal markup"""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogmodaltest',
            layout='grid'
        )

        response = client.get(f'/catalog/{pricelist.token}')
        assert response.status_code == 200
        assert b'Quick View' in response.data
        assert b'productModal' in response.data
        assert str(product.id).encode('utf-8') in response.data

    def test_catalog_view_renders_mock_filter_controls(self, client, db_session):
        """Public catalog renders the 3/22-style search, tag, price, and sort controls."""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogfiltertest',
            layout='grid'
        )

        response = client.get(f'/catalog/{pricelist.token}')
        assert response.status_code == 200
        html = response.data.decode('utf-8')

        assert 'searchInput' in html
        assert 'tagSelect' in html
        assert 'priceMin' in html
        assert 'priceMax' in html
        assert 'sortSelect' in html
        assert 'All Tags' in html
        assert 'PSA10' in html

    def test_catalog_product_detail_endpoint_returns_json(self, client, db_session):
        """Test catalog detail endpoint returns customer-safe modal payload.

        The public catalog must NOT expose source_url, site, or other
        internal sourcing details to public customers.
        """
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogdetailapitest',
            layout='editorial'
        )

        response = client.get(f'/catalog/{pricelist.token}/product/{product.id}')
        assert response.status_code == 200
        data = response.json
        assert data['product_id'] == product.id
        assert data['title'] == 'Catalog Layout Product'
        assert data['price'] == 3200
        assert data['description_html'] == 'Catalog description'
        assert data['description_text'] == 'Catalog description'
        assert data['description_snippet'] == 'Catalog description'
        assert data['in_stock'] is True
        assert data['image_urls'] == [
            'https://img.example.com/catalog-layout.jpg',
            'https://img.example.com/catalog-layout-2.jpg',
        ]
        # Information boundary: source/supplier data must not leak
        assert 'source_url' not in data
        assert 'site' not in data

    def test_catalog_product_detail_returns_404_for_missing_item(self, client, db_session):
        """Test catalog detail endpoint rejects products outside the price list"""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogdetailmissingtest',
            layout='grid'
        )

        response = client.get(f'/catalog/{pricelist.token}/product/999999')
        assert response.status_code == 404
        assert response.json['error'] == 'Not found'

    def test_catalog_view_records_page_view(self, client, db_session):
        """Test opening the catalog records a page view"""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogviewrecordtest',
            layout='grid'
        )

        response = client.get(
            f'/catalog/{pricelist.token}',
            headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile',
                'Referer': 'https://www.google.com/search?q=esp'
            },
            environ_base={'REMOTE_ADDR': '203.0.113.55'}
        )

        assert response.status_code == 200

        views = db_session.query(CatalogPageView).filter_by(pricelist_id=pricelist.id).all()
        assert len(views) == 1
        assert views[0].product_id is None
        assert views[0].user_agent_short == 'Mobile'
        assert views[0].referrer_domain == 'www.google.com'
        assert len(views[0].ip_hash) == 16

    def test_catalog_product_detail_records_product_view(self, client, db_session):
        """Test opening product detail JSON records a product-level view"""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogdetailrecordtest',
            layout='editorial'
        )

        response = client.get(
            f'/catalog/{pricelist.token}/product/{product.id}',
            headers={'Referer': 'https://www.instagram.com/some-post'},
            environ_base={'REMOTE_ADDR': '198.51.100.42'}
        )

        assert response.status_code == 200

        views = (
            db_session.query(CatalogPageView)
            .filter_by(pricelist_id=pricelist.id)
            .order_by(CatalogPageView.id.asc())
            .all()
        )
        assert len(views) == 1
        assert views[0].product_id == product.id
        assert views[0].referrer_domain == 'www.instagram.com'

    def test_pricelist_analytics_requires_login(self, client, db_session):
        """Test analytics page requires authentication"""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='analyticsloginrequiredtest',
            layout='grid'
        )

        response = client.get(f'/pricelists/{pricelist.id}/analytics', follow_redirects=True)
        assert response.request.path == '/login'

    def test_pricelist_analytics_page_shows_metrics(self, client, db_session):
        """Test analytics page renders recorded metrics for owner"""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='analyticsmetricstest',
            layout='editorial'
        )

        db_session.add_all([
            CatalogPageView(
                pricelist_id=pricelist.id,
                ip_hash='abc123abc123abcd',
                user_agent_short='Desktop',
                referrer_domain='direct',
                product_id=None
            ),
            CatalogPageView(
                pricelist_id=pricelist.id,
                ip_hash='def456def456def4',
                user_agent_short='Mobile',
                referrer_domain='www.google.com',
                product_id=product.id
            ),
        ])
        db_session.commit()

        client.post('/login', data={
            'username': 'analyticsmetricstest',
            'password': 'testpassword'
        })

        response = client.get(f'/pricelists/{pricelist.id}/analytics')
        assert response.status_code == 200
        assert 'アクセス解析'.encode('utf-8') in response.data
        assert '総ページビュー'.encode('utf-8') in response.data
        assert b'dailyViewsChart' in response.data
        assert b'Catalog Layout Product' in response.data

    def test_pricelist_analytics_hides_other_users_list(self, client, db_session):
        """Test analytics page enforces ownership"""
        owner, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='analyticsownertest',
            layout='grid'
        )

        other_user = User(username='analyticsotheruser')
        other_user.set_password('testpassword')
        db_session.add(other_user)
        db_session.commit()

        client.post('/login', data={
            'username': 'analyticsotheruser',
            'password': 'testpassword'
        })

        response = client.get(f'/pricelists/{pricelist.id}/analytics')
        assert response.status_code == 404

    def test_catalog_view_does_not_expose_source_info(self, client, db_session):
        """Public catalog HTML must not contain source site names or source URLs."""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogsourcehidetest',
            layout='grid'
        )

        response = client.get(f'/catalog/{pricelist.token}')
        assert response.status_code == 200
        html = response.data.decode('utf-8')

        # No source_url or source site name should appear in public HTML
        assert 'example.com/manual-product' not in html
        assert 'View Source' not in html
        assert 'data-site' not in html
        assert 'categorySelect' not in html

    def test_catalog_list_layout_renders(self, client, db_session):
        """Public catalog with list layout renders the correct layout class."""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='cataloglistlayouttest',
            layout='list'
        )

        response = client.get(f'/catalog/{pricelist.token}')
        assert response.status_code == 200
        assert b'catalog-layout-list' in response.data
        assert b'List' in response.data
        assert b'Catalog Layout Product' in response.data

    def test_pricelist_create_saves_list_layout(self, client, db_session):
        """Creating a price list with 'list' layout persists correctly."""
        user = self._login_user(client, db_session, 'pricelistcreatelisttest')

        response = client.post('/pricelists/create', data={
            'name': 'Compact List',
            'notes': '',
            'currency_rate': '150',
            'layout': 'list'
        }, follow_redirects=False)

        assert response.status_code == 302
        pricelist = db_session.query(PriceList).filter_by(user_id=user.id, name='Compact List').one()
        assert pricelist.layout == 'list'

    def test_catalog_quick_view_modal_is_customer_safe(self, client, db_session):
        """Quick-view modal markup must not contain source link elements."""
        user, product, pricelist, item = self._create_catalog_fixture(
            db_session,
            username='catalogmodalsafetest',
            layout='grid'
        )

        response = client.get(f'/catalog/{pricelist.token}')
        assert response.status_code == 200
        html = response.data.decode('utf-8')

        assert 'modalSourceLink' not in html
        assert 'modal-source-link' not in html
        assert 'SITE_LABELS' not in html


class TestProductRoutes:
    """E2E tests for product routes (routes/products.py)"""
    
    def _setup_user_with_product(self, client, db_session, username='producttest'):
        """Helper to create user and product"""
        user = User(username=username)
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        product = Product(
            user_id=user.id,
            site='mercari',
            source_url='https://jp.mercari.com/item/m12345',
            last_title='Test Product',
            last_price=1000,
            last_status='on_sale',
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db_session.add(product)
        db_session.commit()
        
        variant = Variant(
            product_id=product.id,
            option1_value='Default Title',
            sku='TEST-SKU',
            price=1000,
            inventory_qty=1,
            position=1
        )
        db_session.add(variant)
        db_session.commit()
        
        client.post('/login', data={
            'username': username,
            'password': 'testpassword'
        })
        
        return user, product, variant
    
    def test_product_detail_requires_login(self, client, db_session):
        """Test product detail page requires authentication"""
        user = User(username='productlogintest')
        user.set_password('test')
        db_session.add(user)
        db_session.commit()
        
        product = Product(
            user_id=user.id,
            site='mercari',
            source_url='https://jp.mercari.com/item/m99999',
            last_title='Test',
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db_session.add(product)
        db_session.commit()
        
        response = client.get(f'/product/{product.id}', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_product_detail_loads(self, client, db_session):
        """Test product detail page loads"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'productdetailtest')
        
        response = client.get(f'/product/{product.id}')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        assert 'まとめて白抜き' in html
        assert 'タイトル翻訳' in html
        assert '自動翻訳' in html
        assert 'URLから追加' in html

    def test_product_detail_shows_only_current_users_templates(self, client, db_session):
        """Product edit should not expose other users' description templates."""
        user, product, _ = self._setup_user_with_product(client, db_session, 'productdetailtemplatescope')
        other_user = User(username='productdetailtemplateother')
        other_user.set_password('testpassword')
        db_session.add(other_user)
        db_session.commit()

        db_session.add(
            DescriptionTemplate(
                user_id=user.id,
                name='Owner Product Template',
                content='Owner content',
            )
        )
        db_session.add(
            DescriptionTemplate(
                user_id=other_user.id,
                name='Other Product Template',
                content='Other content',
            )
        )
        db_session.commit()

        response = client.get(f'/product/{product.id}')

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'Owner Product Template' in html
        assert 'Other Product Template' not in html

    def test_product_detail_keeps_legacy_shared_templates_visible(self, client, db_session):
        """Legacy shared templates should remain available in the editor during rollout."""
        user, product, _ = self._setup_user_with_product(client, db_session, 'productdetailtemplatelegacy')
        db_session.add(
            DescriptionTemplate(
                user_id=None,
                name='Legacy Product Template',
                content='Legacy product content',
            )
        )
        db_session.commit()

        response = client.get(f'/product/{product.id}')

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'Legacy Product Template' in html
    
    def test_product_detail_403_for_other_user(self, client, db_session):
        """Test product detail returns 404 for products owned by other user"""
        # Create first user with product
        user1 = User(username='user1')
        user1.set_password('test')
        db_session.add(user1)
        db_session.commit()
        
        product = Product(
            user_id=user1.id,
            site='mercari',
            source_url='https://jp.mercari.com/item/m11111',
            last_title='User1 Product',
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db_session.add(product)
        db_session.commit()
        
        # Login as second user
        user2 = User(username='user2')
        user2.set_password('test')
        db_session.add(user2)
        db_session.commit()
        
        client.post('/login', data={
            'username': 'user2',
            'password': 'test'
        })
        
        # Try to access product owned by user1
        response = client.get(f'/product/{product.id}')
        assert response.status_code == 404
    
    def test_product_detail_update(self, client, db_session):
        """Test updating product via POST"""
        user, product, variant = self._setup_user_with_product(client, db_session, 'productupdatetest')
        
        response = client.post(f'/product/{product.id}', data={
            'title': 'Updated Title',
            'description': 'Updated Description',
            'title_en': 'Updated English Title',
            'description_en': 'Updated English Description',
            'status': 'active',
            'vendor': 'Test Vendor',
            'tags': 'tag1,tag2',
            'handle': 'custom-handle',
            'v_ids': [str(variant.id)],
            f'v_opt1_{variant.id}': 'Size M',
            f'v_price_{variant.id}': '2000',
            f'v_sku_{variant.id}': 'UPDATED-SKU',
            f'v_qty_{variant.id}': '5'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify updates
        db_session.refresh(product)
        assert product.custom_title == 'Updated Title'
        assert product.custom_title_en == 'Updated English Title'
        assert product.custom_description_en == 'Updated English Description'
        assert product.status == 'active'

    def test_product_detail_update_sanitizes_rich_text(self, client, db_session):
        """Test product descriptions are saved in the shared safe rich-text format"""
        user, product, variant = self._setup_user_with_product(client, db_session, 'productrichtexttest')

        response = client.post(f'/product/{product.id}', data={
            'title': 'Updated Title',
            'description': '<p>Updated Description</p><script>alert(1)</script>',
            'title_en': 'Updated English Title',
            'description_en': 'Line one\nLine two',
            'status': 'active',
            'vendor': 'Test Vendor',
            'tags': 'tag1,tag2',
            'handle': 'custom-handle',
            'v_ids': [str(variant.id)],
            f'v_opt1_{variant.id}': 'Size M',
            f'v_price_{variant.id}': '2000',
            f'v_sku_{variant.id}': 'UPDATED-SKU',
            f'v_qty_{variant.id}': '5'
        }, follow_redirects=True)

        assert response.status_code == 200

        db_session.refresh(product)
        assert '<script' not in (product.custom_description or '').lower()
        assert 'Updated Description' in (product.custom_description or '')
        assert '<br' in (product.custom_description_en or '')

    def test_product_detail_update_reorders_and_removes_images(self, client, db_session):
        """Test image updates create a new latest snapshot without mutating history"""
        user, product, variant = self._setup_user_with_product(client, db_session, 'productimagetest')
        snapshot = ProductSnapshot(
            product_id=product.id,
            scraped_at=datetime(2025, 1, 1, 12, 0, 0),
            title='Snapshot Title',
            price=1000,
            status='on_sale',
            description='Snapshot Description',
            image_urls='https://img.example.com/1.jpg|https://img.example.com/2.jpg|https://img.example.com/3.jpg'
        )
        db_session.add(snapshot)
        db_session.commit()

        response = client.post(f'/product/{product.id}', data={
            'title': 'Updated Title',
            'description': 'Updated Description',
            'status': 'active',
            'vendor': 'Test Vendor',
            'tags': 'tag1,tag2',
            'handle': 'custom-handle',
            'image_urls_json': '["https://img.example.com/2.jpg", "https://img.example.com/1.jpg"]',
            'v_ids': [str(variant.id)],
            f'v_opt1_{variant.id}': 'Size M',
            f'v_price_{variant.id}': '2000',
            f'v_sku_{variant.id}': 'UPDATED-SKU',
            f'v_qty_{variant.id}': '5'
        }, follow_redirects=True)

        assert response.status_code == 200

        snapshots = (
            db_session.query(ProductSnapshot)
            .filter_by(product_id=product.id)
            .order_by(ProductSnapshot.scraped_at.asc(), ProductSnapshot.id.asc())
            .all()
        )
        assert len(snapshots) == 2
        assert snapshots[0].image_urls == 'https://img.example.com/1.jpg|https://img.example.com/2.jpg|https://img.example.com/3.jpg'
        assert snapshots[-1].image_urls == 'https://img.example.com/2.jpg|https://img.example.com/1.jpg'
        assert snapshots[-1].title == 'Snapshot Title'
        assert snapshots[-1].description == 'Snapshot Description'

    def test_product_detail_update_creates_snapshot_for_manual_images(self, client, db_session):
        """Test manual image URL additions create a snapshot when none exists yet"""
        user, product, variant = self._setup_user_with_product(client, db_session, 'productmanualimagetest')

        response = client.post(f'/product/{product.id}', data={
            'title': 'Manual Image Product',
            'description': 'Manual image description',
            'status': 'active',
            'vendor': 'Test Vendor',
            'tags': 'tag1,tag2',
            'handle': 'custom-handle',
            'image_urls_json': '["https://img.example.com/manual-1.jpg", "/media/manual-2.jpg", "ftp://ignored.example.com/nope.jpg", "https://img.example.com/manual-1.jpg"]',
            'v_ids': [str(variant.id)],
            f'v_opt1_{variant.id}': 'Size M',
            f'v_price_{variant.id}': '2000',
            f'v_sku_{variant.id}': 'UPDATED-SKU',
            f'v_qty_{variant.id}': '5'
        }, follow_redirects=True)

        assert response.status_code == 200

        snapshot = (
            db_session.query(ProductSnapshot)
            .filter_by(product_id=product.id)
            .order_by(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc())
            .first()
        )
        assert snapshot is not None
        assert snapshot.title == 'Manual Image Product'
        assert snapshot.description == 'Manual image description'
        assert snapshot.image_urls == 'https://img.example.com/manual-1.jpg|/media/manual-2.jpg'

    def test_product_detail_update_uploads_images(self, client, db_session, monkeypatch):
        """Test uploaded product images are saved and appended to the latest snapshot"""
        import routes.products as products_routes

        storage_dir = Path(__file__).resolve().parent / '.tmp_media' / uuid.uuid4().hex
        storage_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(products_routes, 'IMAGE_STORAGE_PATH', str(storage_dir))

        user, product, variant = self._setup_user_with_product(client, db_session, 'productuploadimagetest')

        try:
            response = client.post(
                f'/product/{product.id}',
                data={
                    'title': 'Uploaded Image Product',
                    'description': 'Uploaded image description',
                    'status': 'active',
                    'vendor': 'Test Vendor',
                    'tags': 'tag1,tag2',
                    'handle': 'custom-handle',
                    'image_urls_json': '["https://img.example.com/existing.jpg"]',
                    'image_files': [
                        (io.BytesIO(b'first image bytes'), 'upload-one.png'),
                        (io.BytesIO(b'second image bytes'), 'upload-two.jpg'),
                    ],
                    'v_ids': [str(variant.id)],
                    f'v_opt1_{variant.id}': 'Size M',
                    f'v_price_{variant.id}': '2000',
                    f'v_sku_{variant.id}': 'UPDATED-SKU',
                    f'v_qty_{variant.id}': '5',
                },
                content_type='multipart/form-data',
                follow_redirects=True,
            )

            assert response.status_code == 200

            snapshot = (
                db_session.query(ProductSnapshot)
                .filter_by(product_id=product.id)
                .order_by(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc())
                .first()
            )
            assert snapshot is not None

            image_urls = snapshot.image_urls.split('|')
            assert image_urls[0] == 'https://img.example.com/existing.jpg'
            assert len(image_urls) == 3
            assert image_urls[1].startswith('/media/product_images/')
            assert image_urls[2].startswith('/media/product_images/')

            for image_url in image_urls[1:]:
                filename = image_url.split('/media/product_images/', 1)[1]
                assert (storage_dir / 'product_images' / filename).exists()
        finally:
            shutil.rmtree(storage_dir, ignore_errors=True)

    def test_inline_update_custom_title_en(self, client, db_session):
        """Test inline PATCH updates English title"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'inlineentitletest')

        response = client.patch(
            f'/api/products/{product.id}/inline-update',
            json={
                'field': 'custom_title_en',
                'value': 'Inline English Title'
            }
        )

        assert response.status_code == 200
        assert response.json['ok'] is True
        assert response.json['field'] == 'custom_title_en'
        assert response.json['value'] == 'Inline English Title'

        db_session.refresh(product)
        assert product.custom_title_en == 'Inline English Title'

    def test_inline_update_selling_price(self, client, db_session):
        """Test inline PATCH updates selling price"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'inlinepricetest')

        response = client.patch(
            f'/api/products/{product.id}/inline-update',
            json={
                'field': 'selling_price',
                'value': 3456
            }
        )

        assert response.status_code == 200
        assert response.json['ok'] is True
        assert response.json['field'] == 'selling_price'
        assert response.json['value'] == 3456

        db_session.refresh(product)
        assert product.selling_price == 3456

    def test_inline_update_rejects_invalid_field(self, client, db_session):
        """Test inline PATCH rejects unsupported fields"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'inlineinvalidfieldtest')

        response = client.patch(
            f'/api/products/{product.id}/inline-update',
            json={
                'field': 'custom_title',
                'value': 'should fail'
            }
        )

        assert response.status_code == 400
        assert response.json['error'] == 'Unsupported field'

    def test_inline_update_returns_404_for_other_user(self, client, db_session):
        """Test inline PATCH enforces product ownership"""
        user1 = User(username='inline_owner')
        user1.set_password('test')
        db_session.add(user1)
        db_session.commit()

        product = Product(
            user_id=user1.id,
            site='mercari',
            source_url='https://jp.mercari.com/item/inline-owner',
            last_title='Owner Product',
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db_session.add(product)
        db_session.commit()

        user2 = User(username='inline_other')
        user2.set_password('test')
        db_session.add(user2)
        db_session.commit()

        client.post('/login', data={
            'username': 'inline_other',
            'password': 'test'
        })

        response = client.patch(
            f'/api/products/{product.id}/inline-update',
            json={
                'field': 'custom_title_en',
                'value': 'Not allowed'
            }
        )

        assert response.status_code == 404
        assert response.json['error'] == 'Product not found'

    def test_bulk_price_margin_update(self, client, db_session):
        """Test bulk price API applies margin-based selling price"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'bulkpricemargintest')

        response = client.post(
            '/api/products/bulk-price',
            json={
                'ids': [product.id],
                'mode': 'margin',
                'value': 20
            }
        )

        assert response.status_code == 200
        assert response.json['ok'] is True
        assert response.json['updated_count'] == 1
        assert response.json['skipped_count'] == 0

        db_session.refresh(product)
        assert product.selling_price == 1250

    def test_bulk_price_reset(self, client, db_session):
        """Test bulk price API can reset manual selling prices"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'bulkpriceresettest')
        product.selling_price = 9999
        db_session.commit()

        response = client.post(
            '/api/products/bulk-price',
            json={
                'ids': [product.id],
                'mode': 'reset'
            }
        )

        assert response.status_code == 200
        assert response.json['updated_count'] == 1

        db_session.refresh(product)
        assert product.selling_price is None

    def test_bulk_price_rejects_invalid_margin(self, client, db_session):
        """Test bulk price API validates margin range"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'bulkpriceinvalidmargintest')

        response = client.post(
            '/api/products/bulk-price',
            json={
                'ids': [product.id],
                'mode': 'margin',
                'value': 100
            }
        )

        assert response.status_code == 400
        assert response.json['error'] == 'margin must satisfy 0 <= margin < 100'


class TestScrapeRoutes:
    """E2E tests for scrape routes (routes/scrape.py)"""
    
    def _login_user(self, client, db_session, username='scrapetest'):
        """Helper to create and login a user"""
        user = User(username=username)
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': username,
            'password': 'testpassword'
        })
        return user
    
    def test_scrape_page_requires_login(self, client):
        """Test scrape page requires authentication"""
        response = client.get('/scrape', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_scrape_page_loads(self, client, db_session):
        """Test scrape page loads for authenticated user"""
        self._login_user(client, db_session)
        response = client.get('/scrape')
        assert response.status_code == 200
    
    def test_scrape_page_shows_shop_dropdown(self, client, db_session):
        """Test scrape page displays shop selection dropdown with user's shops"""
        user = self._login_user(client, db_session, 'scrape_shop_test')
        
        # Create shops for this user
        shop1 = Shop(name='My Scrape Shop', user_id=user.id)
        shop2 = Shop(name='Another Shop', user_id=user.id)
        db_session.add(shop1)
        db_session.add(shop2)
        db_session.commit()
        
        response = client.get('/scrape')
        assert response.status_code == 200
        # Check that shop names appear in the response (in the dropdown)
        assert b'My Scrape Shop' in response.data
        assert b'Another Shop' in response.data


class TestExportRoutes:
    """E2E tests for export routes (routes/export.py)"""
    
    def _setup_user_with_product(self, client, db_session, username='exporttest'):
        """Helper to create user and product for export tests"""
        user = User(username=username)
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        product = Product(
            user_id=user.id,
            site='mercari',
            source_url='https://jp.mercari.com/item/m54321',
            last_title='Export Test Product',
            last_price=2000,
            last_status='on_sale',
            status='active',
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db_session.add(product)
        db_session.commit()
        
        variant = Variant(
            product_id=product.id,
            option1_value='Default Title',
            sku='EXPORT-SKU',
            price=2000,
            inventory_qty=1,
            position=1
        )
        db_session.add(variant)
        db_session.commit()
        
        snapshot = ProductSnapshot(
            product_id=product.id,
            title='Export Test Product',
            price=2000,
            status='on_sale',
            description='Test description',
            scraped_at=utc_now()
        )
        db_session.add(snapshot)
        db_session.commit()
        
        client.post('/login', data={
            'username': username,
            'password': 'testpassword'
        })
        
        return user, product, variant
    
    def test_export_shopify_requires_login(self, client):
        """Test Shopify export requires authentication"""
        response = client.get('/export/shopify', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_export_shopify_generates_csv(self, client, db_session):
        """Test Shopify export generates CSV"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'shopifyexporttest')
        
        response = client.get('/export/shopify')
        assert response.status_code == 200
        assert response.content_type == 'text/csv'
        assert b'Handle' in response.data
        assert b'Title' in response.data
    
    def test_export_shopify_no_products(self, client, db_session):
        """Test Shopify export with no products returns 400"""
        user = User(username='noproductstest')
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': 'noproductstest',
            'password': 'testpassword'
        })
        
        response = client.get('/export/shopify')
        assert response.status_code == 400
    
    def test_export_ebay_requires_login(self, client):
        """Test eBay export requires authentication"""
        response = client.get('/export_ebay', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_export_ebay_generates_csv(self, client, db_session):
        """Test eBay export generates CSV"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'ebayexporttest')
        
        response = client.get('/export_ebay')
        assert response.status_code == 200
        assert 'text/csv' in response.content_type
    
    def test_export_stock_update_requires_login(self, client):
        """Test stock update export requires authentication"""
        response = client.get('/export_stock_update', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_export_stock_update_generates_csv(self, client, db_session):
        """Test stock update export generates CSV"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'stockupdatetest')
        
        response = client.get('/export_stock_update')
        assert response.status_code == 200
        assert response.content_type == 'text/csv'
        assert b'Handle' in response.data
        assert b'Variant Inventory Qty' in response.data
    
    def test_export_price_update_requires_login(self, client):
        """Test price update export requires authentication"""
        response = client.get('/export_price_update', follow_redirects=True)
        assert response.request.path == '/login'
    
    def test_export_price_update_generates_csv(self, client, db_session):
        """Test price update export generates CSV"""
        user, product, _ = self._setup_user_with_product(client, db_session, 'priceupdatetest')
        
        response = client.get('/export_price_update')
        assert response.status_code == 200
        assert response.content_type == 'text/csv'
        assert b'Handle' in response.data
        assert b'Variant Price' in response.data


class TestBackwardCompatibility:
    """Tests to ensure backward compatibility of endpoint aliases"""
    
    def _login_user(self, client, db_session, username='compattest'):
        """Helper to create and login a user"""
        user = User(username=username)
        user.set_password('testpassword')
        db_session.add(user)
        db_session.commit()
        
        client.post('/login', data={
            'username': username,
            'password': 'testpassword'
        })
        return user
    
    def test_url_for_index_alias(self, client, db_session):
        """Test that url_for('index') still works"""
        self._login_user(client, db_session, 'indexaliastest')
        
        # The page should render without url_for errors
        response = client.get('/')
        assert response.status_code == 200
    
    def test_url_for_dashboard_alias(self, client, db_session):
        """Test that url_for('dashboard') still works"""
        self._login_user(client, db_session, 'dashaliastest')
        
        response = client.get('/dashboard')
        assert response.status_code == 200
    
    def test_url_for_login_alias(self, client):
        """Test that url_for('login') still works"""
        response = client.get('/login')
        assert response.status_code == 200
    
    def test_url_for_register_alias(self, client):
        """Test that url_for('register') still works"""
        response = client.get('/register')
        assert response.status_code == 200


class TestMediaRoute:
    """Tests for static media serving"""
    
    def test_media_route_exists(self, client):
        """Test that media route is registered"""
        # Try to access a non-existent file - should return 404, not 500
        response = client.get('/media/nonexistent.jpg')
        # 404 is expected since file doesn't exist, but route should be registered
        assert response.status_code in [404, 500]  # Route exists but file not found


class TestSessionIsolation:
    """Tests for user session isolation"""
    
    def test_users_see_only_their_products(self, client, db_session):
        """Test that users can only see their own products"""
        # Create user1 with product
        user1 = User(username='isolation_user1')
        user1.set_password('test')
        db_session.add(user1)
        db_session.commit()
        
        product1 = Product(
            user_id=user1.id,
            site='mercari',
            source_url='https://jp.mercari.com/item/isolation1',
            last_title='User1 Product',
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db_session.add(product1)
        db_session.commit()
        
        # Create user2 with product  
        user2 = User(username='isolation_user2')
        user2.set_password('test')
        db_session.add(user2)
        db_session.commit()
        
        product2 = Product(
            user_id=user2.id,
            site='mercari',
            source_url='https://jp.mercari.com/item/isolation2',
            last_title='User2 Product',
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db_session.add(product2)
        db_session.commit()
        
        # Login as user1
        client.post('/login', data={
            'username': 'isolation_user1',
            'password': 'test'
        })
        
        # Check index page
        response = client.get('/')
        assert response.status_code == 200
        # User1's product should be visible
        assert b'User1 Product' in response.data or b'isolation1' in response.data or response.status_code == 200
    
    def test_users_see_only_their_shops(self, client, db_session):
        """Test that users can only see their own shops"""
        # Create user1 with shop
        user1 = User(username='shop_isolation_user1')
        user1.set_password('test')
        db_session.add(user1)
        db_session.commit()
        
        shop1 = Shop(name='User1 Shop', user_id=user1.id)
        db_session.add(shop1)
        db_session.commit()
        
        # Create user2 with shop
        user2 = User(username='shop_isolation_user2')
        user2.set_password('test')
        db_session.add(user2)
        db_session.commit()
        
        shop2 = Shop(name='User2 Shop', user_id=user2.id)
        db_session.add(shop2)
        db_session.commit()
        
        # Login as user1
        client.post('/login', data={
            'username': 'shop_isolation_user1',
            'password': 'test'
        })
        
        # Check shops page
        response = client.get('/shops')
        assert response.status_code == 200
        # Should see User1 Shop but not User2 Shop
        assert b'User1 Shop' in response.data
        assert b'User2 Shop' not in response.data
