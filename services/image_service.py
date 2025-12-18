"""
Image caching service for Mercari images.
"""
import os
import shutil
import requests

# 画像保存設定
IMAGE_STORAGE_PATH = os.environ.get("IMAGE_STORAGE_PATH", os.path.join('static', 'images'))
os.makedirs(IMAGE_STORAGE_PATH, exist_ok=True)


def cache_mercari_image(mercari_url, product_id, index):
    """
    Download and cache a Mercari image locally.
    Returns the local filename if successful, None otherwise.
    """
    if not mercari_url:
        return None
    filename = f"mercari_{product_id}_{index}.jpg"
    local_path = os.path.join(IMAGE_STORAGE_PATH, filename)
    if os.path.exists(local_path):
        return filename
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://jp.mercari.com/'
        }
        resp = requests.get(mercari_url, headers=headers, stream=True, timeout=10)
        if resp.status_code == 200:
            with open(local_path, 'wb') as f:
                resp.raw.decode_content = True
                shutil.copyfileobj(resp.raw, f)
            return filename
    except Exception as e:
        print(f"Image download failed: {e}")
    return None
