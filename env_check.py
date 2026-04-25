import sys
print(sys.executable)
try:
    import playwright
    print("Playwright is installed")
except ImportError:
    print("Playwright is NOT installed")
