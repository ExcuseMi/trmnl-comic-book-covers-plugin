from flask import Flask, request, send_file, jsonify, abort
import requests
from io import BytesIO
from functools import lru_cache
from urllib.parse import quote, unquote
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# Cache images for 1 hour (maxsize=200 means ~200 different images cached)
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


@app.route('/comic-book-covers/api/issues')
def proxy_issues():
    """
    Proxy Comic Vine API and rewrite image URLs to use our proxy
    This endpoint replaces direct Comic Vine API calls in TRMNL
    """
    # Get all query params and forward to Comic Vine
    params = dict(request.args)

    logger.info(f"Proxying API request with params: {params}")

    try:
        response = requests.get(
            'https://comicvine.gamespot.com/api/issues',
            params=params,
            timeout=20
        )
        response.raise_for_status()
        data = response.json()

        # Get the base URL for this request
        base_url = request.host_url.rstrip('/')

        # Rewrite image URLs in the response
        if 'results' in data:
            for comic in data['results']:
                if 'image' in comic and comic['image']:
                    for key in ['small_url', 'medium_url', 'screen_url', 'original_url',
                                'icon_url', 'tiny_url', 'thumb_url', 'super_url']:
                        if key in comic['image'] and comic['image'][key]:
                            original = comic['image'][key]
                            # Rewrite to use our proxy
                            comic['image'][key] = f"{base_url}/image?url={quote(original)}"

            logger.info(f"Proxied {len(data['results'])} results with rewritten image URLs")

        return jsonify(data)

    except requests.exceptions.RequestException as e:
        logger.error(f"Error proxying API: {e}")
        abort(500, f'Error proxying Comic Vine API: {str(e)}')
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        abort(500, f'Unexpected error: {str(e)}')


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok'})



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)