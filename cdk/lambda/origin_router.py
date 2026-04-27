"""
Lambda@Edge Origin Router for MACP DR Architecture

This function runs on CloudFront origin-request events and routes traffic
to the active region based on a DynamoDB control signal.

Supports:
- Server-side redirects to Amazon Connect Admin (admin subdomain)
- S3 buckets for static content (agent, chat subdomains)
- API Gateway for APIs (chat-api subdomain)

Features:
- Dynamic origin switching based on DynamoDB control signal
- SigV4 request signing for S3 authentication
- Edge-aware DynamoDB routing (prefers nearest regional replica)
- Multi-region DynamoDB read with fallback
- In-memory caching (~15s TTL) to reduce DDB calls
- Last-known-good fallback if both DDB replicas fail
- DR-biased default (us-west-2) when no data available
"""

import json
import time
import logging
from datetime import datetime, timezone

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# =============================================================================
# Configuration
# =============================================================================

TABLE_NAME = 'macp-dr-prod-failover-state'
CACHE_TTL = 15  # seconds

# S3 Origins for static content
S3_ORIGINS = {
    'us-east-1': {
        'bucket': 'macp-dr-opt7-content-prod-us-east-1',
        'domainName': 'macp-dr-opt7-content-prod-us-east-1.s3.us-east-1.amazonaws.com',
        'region': 'us-east-1'
    },
    'us-west-2': {
        'bucket': 'macp-dr-opt7-content-prod-us-west-2',
        'domainName': 'macp-dr-opt7-content-prod-us-west-2.s3.us-west-2.amazonaws.com',
        'region': 'us-west-2'
    }
}

# Amazon Connect Admin URLs for server-side redirects
CONNECT_ADMIN_URLS = {
    'us-east-1': 'https://macp-dos-prod-connect-1.my.connect.aws',
    'us-west-2': 'https://macp-dos-prod-dr-connect-1.my.connect.aws'
}

# API Gateway Origins for APIs
API_ORIGINS = {
    'us-east-1': {
        'domainName': '8ct6cwf9ja.execute-api.us-east-1.amazonaws.com',
        'path': '/prod',
        'region': 'us-east-1'
    },
    'us-west-2': {
        'domainName': 'wcws15iu26.execute-api.us-west-2.amazonaws.com',
        'path': '/prod',
        'region': 'us-west-2'
    }
}

# Map CloudFront edge location prefixes to nearest DynamoDB region
WEST_EDGE_PREFIXES = (
    'SEA', 'SFO', 'LAX', 'PDX', 'HIO', 'SJC', 'OAK', 'SAN',  # US West Coast
    'PHX', 'DEN', 'SLC', 'LAS',  # US Mountain
    'YVR', 'YYC',  # Canada West
)

# =============================================================================
# Module-level caching (persists across Lambda invocations in same container)
# =============================================================================

# Region cache
CACHE = {'region': None, 'expires': 0, 'last_known': None}

# Pre-create DynamoDB clients for both regions (reduces cold start latency)
DDB_CLIENTS = {
    'us-east-1': boto3.client('dynamodb', region_name='us-east-1'),
    'us-west-2': boto3.client('dynamodb', region_name='us-west-2'),
}

# Cache boto3 session for SigV4 signing
BOTO_SESSION = boto3.Session()

# =============================================================================
# DynamoDB Functions
# =============================================================================

def get_nearest_ddb_regions(event):
    """
    Determine DynamoDB region order based on CloudFront edge location.
    Returns regions to try in order of proximity.
    """
    try:
        config = event['Records'][0]['cf'].get('config', {})
        request_id = config.get('requestId', '')
        
        if request_id and '.' in request_id:
            edge_code = request_id.split('.')[0].upper()
            if edge_code.startswith(WEST_EDGE_PREFIXES):
                logger.debug(f"Edge {edge_code} -> prefer us-west-2 DDB")
                return ['us-west-2', 'us-east-1']
    except Exception as e:
        logger.debug(f"Could not determine edge location: {e}")
    
    return ['us-east-1', 'us-west-2']


def read_ddb_item(event=None):
    """
    Read the failover state item from DynamoDB with multi-region fallback.
    Returns the full DynamoDB item dict, or None if all reads fail.
    
    This is the single source of truth for DynamoDB reads - consolidates
    logic previously duplicated in get_active_region and get_health_data.
    """
    ddb_regions = get_nearest_ddb_regions(event) if event else ['us-east-1', 'us-west-2']
    
    for ddb_region in ddb_regions:
        try:
            client = DDB_CLIENTS.get(ddb_region)
            if not client:
                client = boto3.client('dynamodb', region_name=ddb_region)
            
            resp = client.get_item(
                TableName=TABLE_NAME,
                Key={'config_key': {'S': 'active_region'}},
                ConsistentRead=False
            )
            
            if 'Item' in resp:
                logger.debug(f"DDB read from {ddb_region}: success")
                return resp['Item'], ddb_region
            else:
                logger.warning(f"No item found in DDB {ddb_region}")
        except Exception as e:
            logger.warning(f"DDB read failed for {ddb_region}: {e}")
            continue
    
    return None, None


def get_active_region(event=None):
    """
    Read active region from DynamoDB with caching and multi-region fallback.
    """
    global CACHE
    now = time.time()
    
    # Return cached value if fresh
    if now < CACHE['expires'] and CACHE['region']:
        logger.debug(f"Cache hit: {CACHE['region']}")
        return CACHE['region']
    
    # Read from DynamoDB
    item, ddb_region = read_ddb_item(event)
    
    if item:
        region = item.get('active_region', {}).get('S', 'us-east-1')
        CACHE = {
            'region': region,
            'expires': now + CACHE_TTL,
            'last_known': region
        }
        logger.info(f"DDB read from {ddb_region}: {region}")
        return region
    
    # Fallback: last-known-good or default to DR
    fallback = CACHE.get('last_known') or 'us-west-2'
    logger.warning(f"All DDB reads failed, using fallback: {fallback}")
    return fallback


def get_edge_location(event):
    """
    Extract CloudFront edge location info from the event.
    
    Uses CloudFront-Viewer-City and CloudFront-Viewer-Country headers
    if configured in the origin request policy, otherwise returns 'unknown'.
    """
    try:
        cf = event['Records'][0]['cf']
        request = cf.get('request', {})
        headers = request.get('headers', {})
        
        # Check for CloudFront viewer headers (must be configured in origin request policy)
        city = ''
        country = ''
        
        if 'cloudfront-viewer-city' in headers:
            city = headers['cloudfront-viewer-city'][0].get('value', '')
        if 'cloudfront-viewer-country' in headers:
            country = headers['cloudfront-viewer-country'][0].get('value', '')
        
        if city and country:
            return f"{city}, {country}"
        elif city:
            return city
        elif country:
            return country
        
        return 'unknown'
    except Exception as e:
        logger.debug(f"Could not extract edge location: {e}")
        return 'unknown'


def get_health_data(event):
    """
    Get full health data from DynamoDB for the health endpoint.
    Returns all row data plus metadata about the request.
    """
    # Extract edge location from request
    edge_location = get_edge_location(event)
    
    # Read from DynamoDB
    item, ddb_region = read_ddb_item(event)
    
    if item:
        return {
            'active_region': item.get('active_region', {}).get('S', 'unknown'),
            'updated_at': item.get('updated_at', {}).get('S', ''),
            'updated_by': item.get('updated_by', {}).get('S', ''),
            'reason': item.get('reason', {}).get('S', ''),
            'metadata': {
                'edge_location': edge_location,
                'ddb_replica_queried': ddb_region,
                'response_timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            }
        }
    
    # Fallback response
    return {
        'active_region': 'unknown',
        'error': 'Failed to read from DynamoDB',
        'metadata': {
            'edge_location': edge_location,
            'ddb_replica_queried': None,
            'response_timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        }
    }

# =============================================================================
# Response Generators
# =============================================================================

def generate_redirect_response(url, cache_seconds=300):
    """Generate a 302 redirect response with caching."""
    return {
        'status': '302',
        'statusDescription': 'Found',
        'headers': {
            'location': [{'key': 'Location', 'value': url}],
            'cache-control': [{'key': 'Cache-Control', 'value': f'public, max-age={cache_seconds}'}]
        }
    }


def generate_json_response(data, cache_seconds=5):
    """Generate a JSON response with CORS headers."""
    body = json.dumps(data, indent=2)
    return {
        'status': '200',
        'statusDescription': 'OK',
        'headers': {
            'content-type': [{'key': 'Content-Type', 'value': 'application/json'}],
            'cache-control': [{'key': 'Cache-Control', 'value': f'public, max-age={cache_seconds}'}],
            'access-control-allow-origin': [{'key': 'Access-Control-Allow-Origin', 'value': '*'}],
            'access-control-allow-methods': [{'key': 'Access-Control-Allow-Methods', 'value': 'GET, HEAD, OPTIONS'}]
        },
        'body': body
    }


def generate_error_response(message, status_code=500):
    """Generate an error response."""
    return {
        'status': str(status_code),
        'statusDescription': 'Error',
        'headers': {
            'content-type': [{'key': 'Content-Type', 'value': 'application/json'}],
            'cache-control': [{'key': 'Cache-Control', 'value': 'no-cache'}]
        },
        'body': json.dumps({'error': message})
    }

# =============================================================================
# Origin Routing Functions
# =============================================================================

def sign_s3_request(request, bucket, region, uri):
    """
    Sign the request with SigV4 for S3 authentication.
    Uses cached session for better performance.
    """
    credentials = BOTO_SESSION.get_credentials().get_frozen_credentials()
    
    host = f"{bucket}.s3.{region}.amazonaws.com"
    url = f"https://{host}{uri}"
    method = request.get('method', 'GET')
    
    headers = {
        'Host': host,
        'x-amz-content-sha256': 'UNSIGNED-PAYLOAD'
    }
    
    aws_request = AWSRequest(method=method, url=url, headers=headers)
    SigV4Auth(credentials, 's3', region).add_auth(aws_request)
    
    request['origin'] = {
        's3': {
            'domainName': host,
            'region': region,
            'authMethod': 'none',
            'path': '',
            'customHeaders': {}
        }
    }
    
    request['headers']['host'] = [{'key': 'Host', 'value': host}]
    
    for header_name, header_value in aws_request.headers.items():
        header_lower = header_name.lower()
        if header_lower in ['authorization', 'x-amz-date', 'x-amz-security-token', 'x-amz-content-sha256']:
            request['headers'][header_lower] = [{'key': header_name, 'value': header_value}]
    
    logger.debug(f"Signed S3 request for s3://{bucket}{uri}")
    return request


def route_to_api_gateway(request, api_origin, uri):
    """Route request to API Gateway origin."""
    host = api_origin['domainName']
    path = api_origin['path']
    
    request['origin'] = {
        'custom': {
            'domainName': host,
            'port': 443,
            'protocol': 'https',
            'path': path,
            'sslProtocols': ['TLSv1.2'],
            'readTimeout': 30,
            'keepaliveTimeout': 5,
            'customHeaders': {}
        }
    }
    
    request['headers']['host'] = [{'key': 'Host', 'value': host}]
    
    logger.debug(f"Routing to API Gateway: {host}{path}{uri}")
    return request

# =============================================================================
# Main Handler
# =============================================================================

def handler(event, context):
    """
    Lambda@Edge origin-request handler.
    
    Routes traffic based on subdomain:
    - admin → 302 redirect to Amazon Connect Admin
    - health → JSON response with active region and metadata
    - agent, chat, portal → S3 bucket (static content)
    - chat-api → API Gateway (APIs)
    """
    try:
        request = event['Records'][0]['cf']['request']
        
        # Extract subdomain from x-original-host header
        original_host_header = request['headers'].get('x-original-host', [])
        if original_host_header:
            if isinstance(original_host_header, list):
                original_host = original_host_header[0].get('value', '')
            else:
                original_host = original_host_header.get('value', '')
        else:
            original_host = ''
        
        subdomain = original_host.split('.')[0] if original_host else ''
        logger.info(f"Subdomain: {subdomain}, URI: {request['uri']}")
        
        # Route based on subdomain type
        if subdomain == 'admin':
            active_region = get_active_region(event)
            connect_url = CONNECT_ADMIN_URLS.get(active_region, CONNECT_ADMIN_URLS['us-east-1'])
            logger.info(f"Admin redirect to {active_region}: {connect_url}")
            return generate_redirect_response(connect_url, cache_seconds=60)
        
        elif subdomain == 'health':
            health_data = get_health_data(event)
            logger.info(f"Health check: active_region={health_data.get('active_region')}")
            return generate_json_response(health_data)
        
        elif subdomain == 'chat-api':
            active_region = get_active_region(event)
            api_origin = API_ORIGINS.get(active_region, API_ORIGINS['us-east-1'])
            request = route_to_api_gateway(request, api_origin, request['uri'])
            logger.info(f"API routing to {active_region}: {request['uri']}")
        
        else:
            # S3 static content routing (agent, chat, portal, etc.)
            active_region = get_active_region(event)
            folder_map = {'agent': '/agent', 'chat': '/chat', 'portal': '/portal'}
            folder_prefix = folder_map.get(subdomain, '')
            
            # Rewrite URI to include folder prefix
            original_uri = request['uri']
            if folder_prefix and not original_uri.startswith(folder_prefix):
                request['uri'] = folder_prefix + original_uri
                logger.debug(f"URI rewrite: {original_uri} → {request['uri']}")
            
            # Append index.html for directory requests
            if request['uri'].endswith('/'):
                request['uri'] += 'index.html'
            
            # Sign request and route to S3
            s3_origin = S3_ORIGINS.get(active_region, S3_ORIGINS['us-east-1'])
            request = sign_s3_request(
                request,
                s3_origin['bucket'],
                s3_origin['region'],
                request['uri']
            )
            logger.info(f"S3 routing to {active_region}: {request['uri']}")
        
        return request
    
    except Exception as e:
        logger.error(f"Handler error: {e}", exc_info=True)
        # Return error response for direct responses, or fallback for S3
        return generate_error_response(f"Internal error: {str(e)}")


# =============================================================================
# Local Testing
# =============================================================================

if __name__ == '__main__':
    test_event = {
        'Records': [{
            'cf': {
                'config': {'requestId': 'TEST.123.abc'},
                'request': {
                    'uri': '/',
                    'method': 'GET',
                    'headers': {
                        'x-original-host': [{'key': 'x-original-host', 'value': 'health.prod.gsa.dos.macp.cloud'}]
                    }
                }
            }
        }]
    }
    
    result = handler(test_event, None)
    print(json.dumps(result, indent=2))
