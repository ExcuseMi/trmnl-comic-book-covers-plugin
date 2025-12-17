from flask import Flask, request, send_file, jsonify, abort
import requests
from io import BytesIO
from functools import lru_cache
from urllib.parse import unquote
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# Cache images for 1 hour
@lru_cache(maxsize=200)
def fetch_comic_vine_image(url):
    """Fetch and cache Comic Vine images with proper headers"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://comicvine.gamespot.com/',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        logger.info(f"Fetching image: {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        logger.info(f"Successfully fetched image: {url} ({len(response.content)} bytes)")
        return response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None


@app.route('/image')
def proxy_image():
    """Proxy Comic Vine images to avoid hotlinking protection"""
    url = request.args.get('url')

    if not url:
        logger.warning("Missing url parameter")
        abort(400, 'Missing url parameter')

    # Decode if URL encoded
    url = unquote(url)

    # Security check - only allow Comic Vine images
    if 'comicvine.gamespot.com' not in url:
        logger.warning(f"Invalid URL domain: {url}")
        abort(400, 'Invalid URL domain')

    content = fetch_comic_vine_image(url)

    if content is None:
        logger.error(f"Image not found: {url}")
        abort(404, 'Image not found')

    # Determine content type from URL
    content_type = 'image/jpeg'
    if url.lower().endswith('.png'):
        content_type = 'image/png'
    elif url.lower().endswith('.webp'):
        content_type = 'image/webp'

    return send_file(
        BytesIO(content),
        mimetype=content_type,
        as_attachment=False,
        download_name='cover.jpg'
    )


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok', 'mode': 'image-only-proxy'})


@app.route('/')
def index():
    """Root endpoint with usage info"""
    return jsonify({
        'service': 'Comic Vine Image Proxy (Image-Only Mode)',
        'note': 'This version ONLY proxies images, not API calls',
        'usage': {
            'step1': 'Keep using Comic Vine API directly in your TRMNL polling URL',
            'step2': 'Add this JavaScript to your TRMNL plugin markup to rewrite image URLs',
            'javascript': '''
<script>
document.addEventListener('DOMContentLoaded', function() {
    const baseUrl = 'https://trmnl.bettens.dev';
    document.querySelectorAll('img').forEach(img => {
        if (img.src && img.src.includes('comicvine.gamespot.com')) {
            img.src = baseUrl + '/image?url=' + encodeURIComponent(img.src);
        }
    });
});
</script>
            '''
        },
        'endpoints': {
            '/image?url=<url>': 'Proxy individual Comic Vine images',
            '/health': 'Health check'
        }
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)