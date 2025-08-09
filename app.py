from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
import time
import os
from datetime import datetime
from collections import defaultdict, deque
import logging

# Simple logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler('requests.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=["*"])

# Simple Configuration
BACKEND_URL = os.getenv('BACKEND_URL', 'https://httpbin.org')
REQUESTS_PER_HOUR = int(os.getenv('REQUESTS_PER_HOUR', '60'))
DDOS_REQUESTS_PER_MINUTE = int(os.getenv('DDOS_REQUESTS_PER_MINUTE', '20'))
BLOCK_DURATION_MINUTES = int(os.getenv('BLOCK_DURATION_MINUTES', '60'))

# Data storage
ip_requests = defaultdict(deque)  # IP -> list of request times
blocked_ips = {}  # IP -> unblock_time
stats = defaultdict(int)  # Simple statistics

def get_real_ip():
    """Get the real client IP address"""
    # Check common proxy headers
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()
    
    real_ip = request.headers.get('X-Real-IP')
    if real_ip:
        return real_ip
    
    return request.remote_addr or 'unknown'

def cleanup_old_data():
    """Remove old request data"""
    current_time = time.time()
    one_hour_ago = current_time - 3600
    one_minute_ago = current_time - 60
    
    # Clean hourly request data
    for ip in list(ip_requests.keys()):
        while ip_requests[ip] and ip_requests[ip][0] < one_hour_ago:
            ip_requests[ip].popleft()
        if not ip_requests[ip]:
            del ip_requests[ip]
    
    # Remove expired blocks
    for ip in list(blocked_ips.keys()):
        if blocked_ips[ip] < current_time:
            del blocked_ips[ip]
            logger.info(f"✅ Unblocked IP: {ip}")

def is_ip_blocked(ip):
    """Check if IP is currently blocked"""
    return ip in blocked_ips and blocked_ips[ip] > time.time()

def check_rate_limit(ip):
    """Check if IP exceeded rate limit"""
    current_time = time.time()
    
    # Add current request
    ip_requests[ip].append(current_time)
    
    # Count requests in last hour
    hour_count = len(ip_requests[ip])
    
    # Count requests in last minute (for DDoS detection)
    minute_requests = sum(1 for t in ip_requests[ip] if t > current_time - 60)
    
    # Check DDoS (too many requests per minute)
    if minute_requests >= DDOS_REQUESTS_PER_MINUTE:
        block_until = current_time + (BLOCK_DURATION_MINUTES * 60)
        blocked_ips[ip] = block_until
        logger.warning(f"🚨 BLOCKED IP {ip}: {minute_requests} requests/minute (DDoS)")
        return False, hour_count, "DDoS detected"
    
    # Check hourly rate limit
    if hour_count > REQUESTS_PER_HOUR:
        logger.warning(f"⚠️ RATE LIMITED IP {ip}: {hour_count}/{REQUESTS_PER_HOUR} requests/hour")
        return False, hour_count, "Rate limit exceeded"
    
    return True, hour_count, "OK"

def log_request(ip, method, path, status, details=""):
    """Log request details"""
    stats[status] += 1
    stats['total'] += 1
    
    log_msg = f"IP:{ip} | {method} {path} | {status}"
    if details:
        log_msg += f" | {details}"
    
    logger.info(log_msg)

@app.route('/')
def home():
    """Service information"""
    return jsonify({
        'service': 'API Rate Limiter',
        'status': 'running',
        'backend_url': BACKEND_URL,
        'limits': {
            'requests_per_hour': REQUESTS_PER_HOUR,
            'ddos_threshold': f"{DDOS_REQUESTS_PER_MINUTE}/minute",
            'block_duration': f"{BLOCK_DURATION_MINUTES} minutes"
        },
        'endpoints': {
            'health': '/health',
            'stats': '/stats',
            'unblock': '/unblock/<ip>'
        }
    })

@app.route('/health')
def health():
    """Health check with basic stats"""
    cleanup_old_data()
    
    return jsonify({
        'status': 'healthy',
        'backend_url': BACKEND_URL,
        'active_ips': len(ip_requests),
        'blocked_ips': len(blocked_ips),
        'total_requests': stats.get('total', 0),
        'successful_requests': stats.get('SUCCESS', 0),
        'blocked_requests': stats.get('BLOCKED', 0),
        'rate_limited_requests': stats.get('RATE_LIMITED', 0),
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

@app.route('/stats')
def get_stats():
    """Detailed statistics"""
    cleanup_old_data()
    current_time = time.time()
    
    # Current blocked IPs
    blocked_list = []
    for ip, unblock_time in blocked_ips.items():
        remaining_minutes = max(0, int((unblock_time - current_time) / 60))
        blocked_list.append({
            'ip': ip,
            'remaining_minutes': remaining_minutes,
            'unblock_time': datetime.fromtimestamp(unblock_time).strftime('%Y-%m-%d %H:%M:%S')
        })
    
    # Current IP usage
    ip_usage = {}
    for ip, requests in ip_requests.items():
        ip_usage[ip] = {
            'requests_this_hour': len(requests),
            'remaining_requests': max(0, REQUESTS_PER_HOUR - len(requests))
        }
    
    return jsonify({
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'blocked_ips': blocked_list,
        'ip_usage': ip_usage,
        'statistics': dict(stats),
        'configuration': {
            'backend_url': BACKEND_URL,
            'requests_per_hour': REQUESTS_PER_HOUR,
            'ddos_threshold': DDOS_REQUESTS_PER_MINUTE,
            'block_duration_minutes': BLOCK_DURATION_MINUTES
        }
    })

@app.route('/unblock/<ip>')
def unblock_ip(ip):
    """Manually unblock an IP"""
    if ip in blocked_ips:
        del blocked_ips[ip]
        logger.info(f"🔓 MANUALLY UNBLOCKED: {ip}")
        return jsonify({'message': f'IP {ip} has been unblocked', 'success': True})
    else:
        return jsonify({'message': f'IP {ip} was not blocked', 'success': False})

# Main proxy route - handles all requests
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def proxy_request(path=""):
    """Main proxy function"""
    ip = get_real_ip()
    method = request.method
    url_path = f"/{path}"
    
    # Clean old data periodically
    cleanup_old_data()
    
    # Check if IP is blocked
    if is_ip_blocked(ip):
        remaining_time = int((blocked_ips[ip] - time.time()) / 60)
        log_request(ip, method, url_path, "BLOCKED", f"{remaining_time} minutes remaining")
        return jsonify({
            'error': 'IP temporarily blocked',
            'reason': 'Too many requests detected',
            'remaining_minutes': remaining_time,
            'unblock_time': datetime.fromtimestamp(blocked_ips[ip]).strftime('%Y-%m-%d %H:%M:%S')
        }), 429
    
    # Check rate limits
    allowed, request_count, reason = check_rate_limit(ip)
    if not allowed:
        if "DDoS" in reason:
            log_request(ip, method, url_path, "DDOS_BLOCKED", reason)
            return jsonify({
                'error': 'IP blocked due to suspicious activity',
                'reason': reason,
                'block_duration_minutes': BLOCK_DURATION_MINUTES
            }), 429
        else:
            log_request(ip, method, url_path, "RATE_LIMITED", f"{request_count}/{REQUESTS_PER_HOUR}")
            return jsonify({
                'error': 'Rate limit exceeded',
                'limit': f"{REQUESTS_PER_HOUR} requests per hour",
                'current_count': request_count,
                'reset_in_minutes': 60
            }), 429
    
    # Forward request to backend
    try:
        backend_url = f"{BACKEND_URL.rstrip('/')}/{path}"
        
        # Copy headers (remove problematic ones)
        headers = {k: v for k, v in request.headers.items() 
                  if k.lower() not in ['host', 'content-length']}
        
        # Handle different request types
        if method == 'GET':
            response = requests.get(backend_url, params=request.args, headers=headers, timeout=30)
        
        elif method == 'POST':
            if request.content_type and 'multipart/form-data' in request.content_type:
                # File uploads
                files = {name: (file.filename, file.stream, file.content_type) 
                        for name, file in request.files.items()}
                data = request.form.to_dict()
                response = requests.post(backend_url, data=data, files=files, 
                                       headers={k: v for k, v in headers.items() 
                                              if k.lower() != 'content-type'}, timeout=30)
            elif request.is_json:
                # JSON data
                response = requests.post(backend_url, json=request.get_json(), 
                                       headers=headers, timeout=30)
            else:
                # Raw data
                response = requests.post(backend_url, data=request.get_data(), 
                                       headers=headers, timeout=30)
        
        else:
            # Other methods (PUT, DELETE, etc.)
            response = requests.request(method, backend_url, headers=headers, 
                                      data=request.get_data(), params=request.args, timeout=30)
        
        # Log success
        log_request(ip, method, url_path, "SUCCESS", 
                   f"→ Backend {response.status_code} | {request_count}/{REQUESTS_PER_HOUR}")
        
        # Return response
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        response_headers = {k: v for k, v in response.headers.items() 
                          if k.lower() not in excluded_headers}
        
        return Response(
            response.content,
            status=response.status_code,
            headers=response_headers
        )
    
    except requests.exceptions.Timeout:
        log_request(ip, method, url_path, "TIMEOUT", "Backend timeout")
        return jsonify({'error': 'Backend service timeout'}), 504
    
    except requests.exceptions.ConnectionError:
        log_request(ip, method, url_path, "CONNECTION_ERROR", "Backend unavailable")
        return jsonify({'error': 'Backend service unavailable'}), 502
    
    except Exception as e:
        log_request(ip, method, url_path, "ERROR", f"Proxy error: {str(e)}")
        return jsonify({'error': 'Proxy server error', 'details': str(e)}), 500

@app.errorhandler(404)
def not_found(e):
    ip = get_real_ip()
    log_request(ip, request.method, request.path, "NOT_FOUND", "")
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def server_error(e):
    ip = get_real_ip()
    log_request(ip, request.method, request.path, "SERVER_ERROR", str(e))
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀 Simple API Rate Limiter - Starting...")
    print("="*50)
    print(f"🎯 Backend URL: {BACKEND_URL}")
    print(f"⏰ Rate Limit: {REQUESTS_PER_HOUR} requests/hour")
    print(f"🛡️ DDoS Threshold: {DDOS_REQUESTS_PER_MINUTE} requests/minute")
    print(f"🚫 Block Duration: {BLOCK_DURATION_MINUTES} minutes")
    print(f"📝 Logs: requests.log")
    print("="*50)
    print("📋 Endpoints:")
    print("   /health    - Health check")
    print("   /stats     - Detailed statistics")  
    print("   /unblock/<ip> - Unblock IP")
    print("   /*         - Proxy to backend")
    print("="*50 + "\n")
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
