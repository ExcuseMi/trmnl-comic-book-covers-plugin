from flask import Flask, request, send_file, jsonify, abort
import requests
from io import BytesIO
from functools import lru_cache, wraps
from urllib.parse import quote, unquote
import logging
import time
import threading
from datetime import datetime
import httpx
import asyncio
import os

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Note: CORS is handled by Caddy reverse proxy, not here

# Configuration
ENABLE_IP_WHITELIST = os.getenv('ENABLE_IP_WHITELIST', 'true').lower() == 'true'
IP_REFRESH_HOURS = int(os.getenv('IP_REFRESH_HOURS', '24'))
COMIC_VINE_API_KEY = os.getenv('COMIC_VINE_API_KEY')

# TRMNL API endpoint for IP addresses
TRMNL_IPS_API = 'https://usetrmnl.com/api/ips'

# Global variables for IP management
TRMNL_IPS = set()
TRMNL_IPS_LOCK = threading.Lock()
last_ip_refresh = None

# Global variables for series data caching
SERIES_DATA = []
SERIES_DATA_LOCK = threading.Lock()
last_series_refresh = None

# Always allow localhost
LOCALHOST_IPS = ['127.0.0.1', '::1']

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


async def fetch_trmnl_ips():
    """Fetch current TRMNL server IPs from their API"""
    try:
        logger.info(f"Fetching TRMNL IPs from {TRMNL_IPS_API}")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(TRMNL_IPS_API)
            response.raise_for_status()
            data = response.json()

            # Extract IPv4 and IPv6 addresses
            ipv4_list = data.get('data', {}).get('ipv4', [])
            ipv6_list = data.get('data', {}).get('ipv6', [])

            # Combine into set
            ips = set(ipv4_list + ipv6_list + LOCALHOST_IPS)

            logger.info(f"Fetched {len(ips)} TRMNL IPs ({len(ipv4_list)} IPv4, {len(ipv6_list)} IPv6)")
            return ips

    except Exception as e:
        logger.error(f"Failed to fetch TRMNL IPs: {e}")
        logger.warning("IP whitelist will use fallback IPs only")
        return set(LOCALHOST_IPS)


def update_trmnl_ips_sync():
    """Update TRMNL IPs - sync wrapper for background thread"""
    global TRMNL_IPS, last_ip_refresh

    try:
        logger.info("Starting scheduled TRMNL IP refresh")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ips = loop.run_until_complete(fetch_trmnl_ips())
            with TRMNL_IPS_LOCK:
                TRMNL_IPS = ips
                last_ip_refresh = datetime.now()
            logger.info(f"TRMNL IPs updated successfully")
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Error updating TRMNL IPs: {e}")


def ip_refresh_worker():
    """Background worker that refreshes TRMNL IPs periodically"""
    while True:
        try:
            time.sleep(IP_REFRESH_HOURS * 3600)
            update_trmnl_ips_sync()
        except Exception as e:
            logger.error(f"IP refresh worker error: {e}")
            time.sleep(3600)


def start_ip_refresh_worker():
    """Start background thread for IP refresh"""
    if not ENABLE_IP_WHITELIST:
        logger.info("IP whitelist disabled, skipping refresh scheduler")
        return

    worker_thread = threading.Thread(
        target=ip_refresh_worker,
        daemon=True,
        name='IP-Refresh-Worker'
    )
    worker_thread.start()
    logger.info(f"Started IP refresh worker (refresh every {IP_REFRESH_HOURS} hours)")


async def fetch_popular_series():
    """Fetch popular comic series from Comic Vine API"""
    if not COMIC_VINE_API_KEY:
        logger.error("Cannot fetch series: COMIC_VINE_API_KEY not configured")
        return []

    try:
        logger.info("Fetching popular series from Comic Vine API...")

        all_series = []
        offset = 0
        limit = 100
        max_series = 1000  # Get top 1000 series

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
        }

        while len(all_series) < max_series:
            params = {
                'api_key': COMIC_VINE_API_KEY,
                'format': 'json',
                'offset': offset,
                'limit': limit,
                'sort': 'count_of_issues:desc',
                'field_list': 'id,name,count_of_issues,start_year,publisher'
            }

            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    'https://comicvine.gamespot.com/api/volumes/',  # Note trailing slash
                    params=params,
                    headers=headers
                )
                response.raise_for_status()
                data = response.json()

            if not data.get('results'):
                break

            for volume in data['results']:
                if len(all_series) >= max_series:
                    break

                # Skip invalid volumes
                if not volume or not volume.get('id') or not volume.get('name'):
                    continue

                issue_count = volume.get('count_of_issues', 0)
                if issue_count < 1:
                    continue

                publisher = volume.get('publisher') or {}
                publisher_name = publisher.get('name', 'Unknown') if isinstance(publisher, dict) else 'Unknown'

                all_series.append({
                    'id': volume['id'],
                    'name': volume['name'],
                    'start_year': volume.get('start_year', 0),
                    'issue_count': issue_count,
                    'publisher_name': publisher_name
                })

            offset += limit

            if len(data['results']) < limit:
                break

            # Rate limit
            await asyncio.sleep(1)

        # Sort alphabetically for easier searching
        all_series.sort(key=lambda x: x['name'].lower())

        logger.info(f"Fetched {len(all_series)} popular series")
        return all_series

    except Exception as e:
        logger.error(f"Failed to fetch series: {e}")
        return []


def update_series_data_sync():
    """Update series data - sync wrapper for background thread"""
    global SERIES_DATA, last_series_refresh

    try:
        logger.info("Starting series data refresh")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            series = loop.run_until_complete(fetch_popular_series())
            with SERIES_DATA_LOCK:
                SERIES_DATA = series
                last_series_refresh = datetime.now()
            logger.info(f"Series data updated successfully ({len(series)} series)")
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Error updating series data: {e}")


def series_refresh_worker():
    """Background worker that refreshes series data daily"""
    while True:
        try:
            time.sleep(24 * 3600)  # 24 hours
            update_series_data_sync()
        except Exception as e:
            logger.error(f"Series refresh worker error: {e}")
            time.sleep(3600)


def start_series_refresh_worker():
    """Start background thread for series data refresh"""
    if not COMIC_VINE_API_KEY:
        logger.warning("API key not configured, skipping series refresh worker")
        return

    worker_thread = threading.Thread(
        target=series_refresh_worker,
        daemon=True,
        name='Series-Refresh-Worker'
    )
    worker_thread.start()
    logger.info("Started series refresh worker (refresh every 24 hours)")


def get_allowed_ips():
    """Get current list of allowed IPs from TRMNL API"""
    with TRMNL_IPS_LOCK:
        return TRMNL_IPS.copy()


def get_client_ip():
    """Get the real client IP address, accounting for Cloudflare Tunnel"""
    # Check CF-Connecting-IP FIRST (Cloudflare Tunnel)
    if request.headers.get('CF-Connecting-IP'):
        return request.headers.get('CF-Connecting-IP').strip()

    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()

    if request.headers.get('X-Real-IP'):
        return request.headers.get('X-Real-IP').strip()

    return request.remote_addr


def require_whitelisted_ip(f):
    """Decorator to enforce IP whitelisting on routes"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not ENABLE_IP_WHITELIST:
            return f(*args, **kwargs)

        client_ip = get_client_ip()
        allowed_ips = get_allowed_ips()

        if client_ip not in allowed_ips:
            logger.warning(f"Blocked request from unauthorized IP: {client_ip}")
            return jsonify({
                'error': 'Access denied',
                'message': 'Your IP address is not authorized to access this service'
            }), 403

        logger.debug(f"Allowed request from whitelisted IP: {client_ip}")
        return f(*args, **kwargs)

    return decorated_function


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
@require_whitelisted_ip
def proxy_image():
    """Proxy Comic Vine images to avoid hotlinking protection"""
    url = request.args.get('url')

    if not url:
        logger.warning("Missing url parameter")
        abort(400, 'Missing url parameter')

    # Decode if URL encoded
    url = unquote(url)

    logger.info(f"Decoded image URL: {url}")

    # Security check - prevent infinite loops by rejecting self-referencing URLs
    if 'trmnl.bettens.dev' in url or request.host in url:
        logger.error(f"Rejected self-referencing URL: {url}")
        abort(400, 'Cannot proxy images from this server (infinite loop detected)')

    # Security check - URL must be a valid Comic Vine image URL
    # Check that it starts with Comic Vine domain (not just contains it in query params)
    if not url.startswith('https://comicvine.gamespot.com/') and not url.startswith('http://comicvine.gamespot.com/'):
        logger.warning(f"Invalid URL - must start with Comic Vine domain: {url}")
        abort(400, 'Invalid URL - only Comic Vine images allowed')

    # Additional validation - must be from their CDN path
    if '/a/uploads/' not in url:
        logger.warning(f"Invalid URL - not a Comic Vine image path: {url}")
        abort(400, 'Invalid URL - must be a Comic Vine image')

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


@app.route('/series/debug')
def debug_series():
    """Debug endpoint to inspect series cache"""
    with SERIES_DATA_LOCK:
        series_count = len(SERIES_DATA)
        first_10 = SERIES_DATA[:10] if SERIES_DATA else []
        last_10 = SERIES_DATA[-10:] if len(SERIES_DATA) >= 10 else []

    return jsonify({
        'total_series': series_count,
        'last_refresh': last_series_refresh.isoformat() if last_series_refresh else None,
        'first_10_series': first_10,
        'last_10_series': last_10,
        'alphabetical_check': {
            'first_name': first_10[0]['name'] if first_10 else None,
            'last_name': last_10[-1]['name'] if last_10 else None
        }
    })


@app.route('/health')
def health():
    """Health check endpoint"""
    client_ip = get_client_ip()
    allowed_ips = get_allowed_ips()
    is_whitelisted = client_ip in allowed_ips if ENABLE_IP_WHITELIST else True

    health_data = {
        'status': 'ok',
        'service': 'comic-vine-proxy',
        'timestamp': datetime.now().isoformat()
    }

    if ENABLE_IP_WHITELIST:
        with TRMNL_IPS_LOCK:
            trmnl_count = len(TRMNL_IPS)
            last_refresh = last_ip_refresh.isoformat() if last_ip_refresh else None

        health_data['ip_whitelist'] = {
            'enabled': True,
            'your_ip': client_ip,
            'whitelisted': is_whitelisted,
            'ips_loaded': trmnl_count,
            'last_refresh': last_refresh,
            'refresh_interval_hours': IP_REFRESH_HOURS
        }
    else:
        health_data['ip_whitelist'] = {
            'enabled': False,
            'your_ip': client_ip
        }

    if COMIC_VINE_API_KEY:
        health_data['api_key_configured'] = True

    # Add series data status
    with SERIES_DATA_LOCK:
        health_data['series_cache'] = {
            'count': len(SERIES_DATA),
            'last_refresh': last_series_refresh.isoformat() if last_series_refresh else None
        }

    return jsonify(health_data)


@app.route('/comics/random', methods=['GET'])
@require_whitelisted_ip
def get_random_comics():
    """
    Get random comic issues based on selected series and display style

    Query params:
    - series_ids: comma-separated list of series IDs (e.g., "6306,2340,1234")
    - count: number of comics to return (default: 3, max: 10)
    - seed: optional seed for reproducible randomization (default: current hour)

    Returns: JSON with comic issues ready for display
    """
    import random
    from datetime import datetime

    # Get parameters
    series_ids_str = request.args.get('series_ids', '')
    count = min(int(request.args.get('count', 3)), 10)
    seed = request.args.get('seed')

    # Use current hour as default seed for hourly rotation
    if not seed:
        seed = datetime.utcnow().strftime('%Y%m%d%H')

    logger.info(f"Random comics request: series_ids={series_ids_str}, count={count}, seed={seed}")

    if not series_ids_str:
        return jsonify({
            'error': 'Missing required parameter: series_ids',
            'example': '/comics/random?series_ids=6306,2340&count=3'
        }), 400

    # Parse series IDs
    series_ids = [sid.strip() for sid in series_ids_str.split(',') if sid.strip()]

    if not series_ids:
        return jsonify({'error': 'No valid series IDs provided'}), 400

    # Initialize random with seed for reproducibility
    rng = random.Random(seed)

    all_issues = []

    try:
        # Determine strategy based on series count
        if len(series_ids) == 1:
            # Single series: fetch multiple issues from that one series
            series_id = series_ids[0]
            offset = rng.randint(0, 100)

            params = {
                'api_key': COMIC_VINE_API_KEY,
                'format': 'json',
                'field_list': 'name,image,cover_date,issue_number,volume,description,character_credits,team_credits,location_credits,concept_credits,person_credits,site_detail_url',
                'limit': count,  # Fetch all at once
                'filter': f'volume:{series_id}',
                'offset': offset,
                'sort': 'cover_date'
            }

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
                'Referer': 'https://comicvine.gamespot.com/'
            }

            rate_limit_api_request()

            response = session.get(
                'https://comicvine.gamespot.com/api/issues/',
                params=params,
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get('results'):
                all_issues = data['results']
        else:
            # Multiple series: fetch 1 issue from each series (round-robin)
            # If we need more issues than series, cycle through them
            series_cycle = series_ids * ((count // len(series_ids)) + 1)

            for i in range(count):
                series_id = series_cycle[i]
                offset = rng.randint(0, 100) + i  # Vary offset slightly

                params = {
                    'api_key': COMIC_VINE_API_KEY,
                    'format': 'json',
                    'field_list': 'name,image,cover_date,issue_number,volume,description,character_credits,team_credits,location_credits,concept_credits,person_credits,site_detail_url',
                    'limit': 1,
                    'filter': f'volume:{series_id}',
                    'offset': offset,
                    'sort': 'cover_date'
                }

                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                    'Referer': 'https://comicvine.gamespot.com/'
                }

                rate_limit_api_request()

                response = session.get(
                    'https://comicvine.gamespot.com/api/issues/',
                    params=params,
                    headers=headers,
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()

                if data.get('results'):
                    all_issues.extend(data['results'])

        # Rewrite all image URLs to use our proxy
        for issue in all_issues:
            if 'image' in issue and issue['image']:
                for key in ['small_url', 'medium_url', 'screen_url', 'original_url']:
                    if key in issue['image'] and issue['image'][key]:
                        original_url = issue['image'][key]
                        if 'comicvine.gamespot.com' in original_url:
                            scheme = request.scheme
                            host = request.host
                            issue['image'][key] = f"{scheme}://{host}/comic-book-covers/image?url={quote(original_url)}"

        logger.info(f"Returning {len(all_issues)} random comics")

        return jsonify({
            'success': True,
            'count': len(all_issues),
            'series_ids': series_ids,
            'seed': seed,
            'results': all_issues
        })

    except Exception as e:
        logger.error(f"Error fetching random comics: {e}")
        return jsonify({
            'error': str(e),
            'success': False
        }), 500


@app.route('/series/search', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/comic-book-covers/series/search', methods=['GET', 'POST', 'OPTIONS'])
def search_series():
    """
    Search endpoint for TRMNL xhrSelect field - NO IP WHITELIST
    Returns series list

    Expected response format for xhrSelect: [{ 'Display Name' => 'value' }]
    """
    # Handle preflight OPTIONS request
    if request.method == 'OPTIONS':
        return '', 204

    logger.info(f"Series list request from IP: {get_client_ip()}")

    with SERIES_DATA_LOCK:
        series_list = SERIES_DATA.copy()

    logger.info(f"Series cache has {len(series_list)} series")

    if not series_list:
        logger.error("No series data available - returning empty results")
        return jsonify([])

    # Return top 250 most popular series for xhrSelect dropdown
    results = series_list[:250]

    logger.info(f"Returning {len(results)} series (top 250)")

    # Log first and last series for debugging
    if results:
        logger.info(f"First series: {results[0]['name']}")
        logger.info(f"Last series: {results[-1]['name']}")

    # Format for TRMNL xhrSelect
    # Expected format: [{ 'Display Name' => 'id' }]
    formatted_results = []
    for series in results:
        # Build display name
        display_name = series['name']
        if series.get('start_year'):
            display_name += f" ({series['start_year']})"
        if series.get('publisher_name'):
            display_name += f" - {series['publisher_name']}"
        display_name += f" [{series.get('issue_count', 0)} issues]"

        # xhrSelect format: { 'Display Name': 'stored_value' }
        formatted_results.append({
            display_name: str(series['id'])
        })

    logger.info(f"Formatted {len(formatted_results)} results for xhrSelect")
    return jsonify(formatted_results)
    logger.info(f"Returning {len(formatted_results)} series for xhrSelect")
    return jsonify(formatted_results)


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


# Initialize TRMNL IPs on startup
async def startup_init():
    """Initialize TRMNL IPs and series data on startup"""
    global TRMNL_IPS, last_ip_refresh, SERIES_DATA, last_series_refresh

    logger.info("=" * 60)
    logger.info("Starting Comic Vine Proxy")
    logger.info(f"IP Whitelist: {'Enabled' if ENABLE_IP_WHITELIST else 'Disabled'}")
    logger.info(f"API Key: {'Configured' if COMIC_VINE_API_KEY else 'Not configured'}")

    if ENABLE_IP_WHITELIST:
        ips = await fetch_trmnl_ips()
        with TRMNL_IPS_LOCK:
            TRMNL_IPS = ips
            last_ip_refresh = datetime.now()

        start_ip_refresh_worker()
    else:
        logger.warning("IP whitelist is disabled - all IPs will be allowed!")

    # Fetch series data on startup
    if COMIC_VINE_API_KEY:
        logger.info("Fetching series data on startup...")
        series = await fetch_popular_series()
        with SERIES_DATA_LOCK:
            SERIES_DATA = series
            last_series_refresh = datetime.now()

        start_series_refresh_worker()
        logger.info(f"Series data initialized: {len(series)} series cached")
    else:
        logger.warning("API key not configured - series search will not be available")

    logger.info("Startup Complete - Ready to accept requests")
    logger.info("=" * 60)


# Run startup initialization
try:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(startup_init())
    loop.close()
except Exception as e:
    logger.error(f"Startup error: {e}")
    logger.warning("Continuing with fallback IPs (localhost only)")