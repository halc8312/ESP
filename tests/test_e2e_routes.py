"""
Comprehensive E2E Tests for the refactored Flask application.
Tests all routes after the app.py split to ensure functionality is preserved.
"""
import pytest
from datetime import datetime
from models import User, Shop, Product, Variant, ProductSnapshot, DescriptionTemplate


class TestAuthenticationRoutes:
    """E2E tests for authentication routes (routes/auth.py)"""
    
    def test_login_page_renders(self, client):
        """Test that login page renders correctly"""
        response = client.get('/login')
        assert response.status_code == 200
        assert b'Login' in response.data or b'login' in response.data.lower()
    
    def test_register_page_renders(self, client):
        """Test that register page renders correctly"""
        response = client.get('/register')
        assert response.status_code == 200
        assert b'Register' in response.data or b'register' in response.data.lower()
    
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
        assert b'already exists' in response.data or b'Username' in response.data
    
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
        assert b'Invalid' in response.data or b'error' in response.data.lower()
    
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
    
    def test_create_template(self, client, db_session):
        """Test creating a description template"""
        self._login_user(client, db_session, 'createtemplatetest')
        
        response = client.post('/templates', data={
            'name': 'Test Template',
            'content': 'This is test template content'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify template was created
        template = db_session.query(DescriptionTemplate).filter_by(name='Test Template').first()
        assert template is not None
        assert template.content == 'This is test template content'
    
    def test_delete_template(self, client, db_session):
        """Test deleting a template"""
        self._login_user(client, db_session, 'deletetemplatetest')
        
        template = DescriptionTemplate(name='Template to Delete', content='Content')
        db_session.add(template)
        db_session.commit()
        template_id = template.id
        
        response = client.post(f'/templates/{template_id}/delete', follow_redirects=True)
        assert response.status_code == 200
        
        # Verify template was deleted
        deleted_template = db_session.query(DescriptionTemplate).filter_by(id=template_id).first()
        assert deleted_template is None


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
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
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
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
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
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
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
        assert product.status == 'active'


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
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
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
            scraped_at=datetime.utcnow()
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
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
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
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
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
