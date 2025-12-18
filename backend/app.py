from flask import Flask, request, send_file, jsonify, abort
import requests
from io import BytesIO
from functools import lru_cache
from urllib.parse import quote, unquote
import logging
import time
import threading

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Create a requests session that persists cookies
session = requests.Session()

# Rate limiting for API requests (max 1 request per second to avoid triggering Comic Vine's detection)
api_request_lock = threading.Lock()
last_api_request_time = 0


def rate_limit_api_request():
    """Ensure minimum 1 second between API requests"""
    global last_api_request_time
    with api_request_lock:
        current_time = time.time()
        time_since_last = current_time - last_api_request_time
        if time_since_last < 1.0:
            sleep_time = 1.0 - time_since_last
            logger.info(f"Rate limiting: sleeping for {sleep_time:.2f}s")
            time.sleep(sleep_time)
        last_api_request_time = time.time()


# Cache images for 1 hour (maxsize=200 means ~200 different images cached)
@lru_cache(maxsize=200)
def fetch_comic_vine_image(url, use_proxy=True):
    """Fetch and cache Comic Vine images with proper headers"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Referer': 'https://comicvine.gamespot.com/',
        'Sec-Ch-Ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'image',
        'Sec-Fetch-Mode': 'no-cors',
        'Sec-Fetch-Site': 'same-origin',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }

    # Optional: Use a proxy if configured
    # Set these environment variables or hardcode your proxy
    proxies = None
    if use_proxy:
        import os
        proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('HTTPS_PROXY')
        if proxy_url:
            proxies = {
                'http': proxy_url,
                'https': proxy_url
            }
            logger.info(f"Using proxy for request")

    try:
        logger.info(f"Fetching image: {url}")
        response = session.get(url, headers=headers, proxies=proxies, timeout=15, allow_redirects=True)
        response.raise_for_status()
        logger.info(f"Successfully fetched image: {url} ({len(response.content)} bytes)")
        return response.content
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching {url}: {e.response.status_code} - {e.response.reason}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed for {url}: {type(e).__name__} - {str(e)}")
        return None


@app.route('/image')
@app.route('/comic-book-covers/image')
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


@app.route('/api/issues')
@app.route('/comic-book-covers/api/issues')
def proxy_issues():
    """
    Proxy Comic Vine API and rewrite image URLs to use our proxy
    This endpoint replaces direct Comic Vine API calls in TRMNL
    """
    # Get all query params and forward to Comic Vine
    params = dict(request.args)

    # Inject API key from environment if not provided
    import os
    if 'api_key' not in params or not params['api_key']:
        env_api_key = os.environ.get('COMIC_VINE_API_KEY')
        if env_api_key:
            params['api_key'] = env_api_key
            logger.info("Using API key from environment variable")
        else:
            logger.warning("No API key provided in request or environment")

    logger.info(f"Proxying API request with params: {params}")

    # Rate limit to avoid triggering Comic Vine's anti-bot measures
    rate_limit_api_request()

    # Add headers for API requests (different from image requests)
    # Note: Don't manually specify Accept-Encoding - let requests handle it
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/html, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://comicvine.gamespot.com/',
    }

    # Get proxy settings if configured
    import os
    proxies = None
    proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('HTTPS_PROXY')
    if proxy_url:
        proxies = {'http': proxy_url, 'https': proxy_url}
        logger.info("Using proxy for API request")

    try:
        # Use requests.get directly (not session) to avoid cookie interference
        response = requests.get(
            'https://comicvine.gamespot.com/api/issues',
            params=params,
            headers=headers,
            proxies=proxies,
            timeout=20,
            allow_redirects=True
        )

        logger.info(f"API response status: {response.status_code}")
        logger.info(f"API response content-type: {response.headers.get('content-type')}")
        logger.info(f"API response content-encoding: {response.headers.get('content-encoding')}")

        response.raise_for_status()

        try:
            # Use .json() directly - it handles decompression automatically
            data = response.json()
            logger.info(f"Successfully parsed JSON response with {len(data.get('results', []))} results")
        except ValueError as e:
            # Only access .text if JSON parsing fails (for debugging)
            logger.error(f"Failed to parse JSON. Status: {response.status_code}")
            logger.error(f"Content-Encoding: {response.headers.get('content-encoding')}")
            logger.error(f"Content-Type: {response.headers.get('content-type')}")
            logger.error(f"Response text preview: {response.text[:500]}")
            abort(500, f'Comic Vine returned invalid JSON: {str(e)}')

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

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            logger.warning(f"Comic Vine returned 403 - they may be blocking this server's IP")
            logger.warning("Falling back to passthrough mode - API works but images won't be proxied")

            # Return error with helpful message
            return jsonify({
                'error': 'Comic Vine API blocking detected',
                'message': 'Comic Vine is blocking API requests from this server. You have two options:',
                'options': [
                    '1. Use Comic Vine API directly (images still won\'t load in TRMNL)',
                    '2. Try using a VPN or different server IP',
                    '3. Contact Comic Vine to whitelist your server IP'
                ],
                'suggestion': 'Your server IP may be flagged. Try deploying from a residential IP or different cloud provider.',
                'your_ip': request.remote_addr
            }), 403
        else:
            raise
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


@app.route('/')
def index():
    """Root endpoint with usage info"""
    return jsonify({
        'service': 'Comic Vine Image Proxy',
        'endpoints': {
            '/comic-book-covers/api/issues': 'Proxy Comic Vine API with image URL rewriting',
            '/image?url=<url>': 'Proxy individual Comic Vine images',
            '/health': 'Health check'
        },
        'usage': 'Update your TRMNL plugin to use https://your-domain/comic-book-covers/api/issues instead of Comic Vine API directly'
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)