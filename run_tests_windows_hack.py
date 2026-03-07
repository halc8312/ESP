import sys
import unittest.mock

# Mock fcntl which is Unix-only
sys.modules['fcntl'] = unittest.mock.MagicMock()

import pytest

if __name__ == '__main__':
    pytest.main(['tests/test_scraping_logic.py', '-v'])
