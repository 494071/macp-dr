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

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
import time
import logging
from datetime import datetime
from urllib.parse import quote

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration
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

# Module-level cache
CACHE = {'region': None, 'expires': 0, 'last_known': None}

# Map CloudFront edge location prefixes to nearest DynamoDB region
# Using PriceClass_100: North America and Europe only
# DynamoDB replicas: us-east-1 and us-west-2
#
# US West Coast edges -> prefer us-west-2
# US East/Central, Canada East, Europe -> prefer us-east-1
WEST_EDGE_PREFIXES = (
    # US West Coast
    'SEA',  # Seattle
    'SFO',  # San Francisco
    'LAX',  # Los Angeles
    'PDX',  # Portland
    'HIO',  # Hillsboro, OR
    'SJC',  # San Jose
    'OAK',  # Oakland
    'SAN',  # San Diego
    # US Mountain (closer to us-west-2)
    'PHX',  # Phoenix
    'DEN',  # Denver
    'SLC',  # Salt Lake City
    'LAS',  # Las Vegas
    # Canada West
    'YVR',  # Vancouver
    'YYC',  # Calgary
)


def get_nearest_ddb_regions(event):
    """
    Determine DynamoDB region order based on CloudFront edge location.
    Returns regions to try in order of proximity.
    """
    try:
        # Edge location is in the config section of the CloudFront event
        config = event['Records'][0]['cf'].get('config', {})
        edge_location = config.get('distributionDomainName', '')
        
        # Try to get from request ID which contains edge location code
        request_id = config.get('requestId', '')
        
        # CloudFront edge location codes are typically in headers or can be derived
        # The distributionId doesn't help, but we can check the request origin
        request = event['Records'][0]['cf']['request']
        
        # Check for CloudFront-Viewer-Country or similar headers
        viewer_country_header = request['headers'].get('cloudfront-viewer-country', [])
        
        # Better approach: use the 'x-edge-location' if available, or derive from request
        # For Lambda@Edge, we can inspect the invoked function ARN region
        # But actually, the most reliable is to check context.invoked_function_arn
        
        # Fallback: check if there's edge location info we can use
        # The request ID format includes edge location: <edge>.<timestamp>.<id>
        if request_id and '.' in request_id:
            edge_code = request_id.split('.')[0].upper()
            if edge_code.startswith(WEST_EDGE_PREFIXES):
                logger.info(f"Edge location {edge_code} - preferring us-west-2 DDB")
                return ['us-west-2', 'us-east-1']
    except Exception as e:
        logger.debug(f"Could not determine edge location: {e}")
    
    # Default: prefer us-east-1 (US East, US Central, Canada East, and all European edges)
    return ['us-east-1', 'us-west-2']


def get_active_region(event=None):
    """
    Read active region from DynamoDB with caching and multi-region fallback.
    Tries nearest DynamoDB replica first based on edge location.
    """
    global CACHE
    now = time.time()
    
    # Return cached value if fresh
    if now < CACHE['expires'] and CACHE['region']:
        logger.info(f"Cache hit: {CACHE['region']}")
        return CACHE['region']
    
    # Determine region order based on edge location
    ddb_regions = get_nearest_ddb_regions(event) if event else ['us-east-1', 'us-west-2']
    
    # Try each DDB replica in order
    for ddb_region in ddb_regions:
        try:
            client = boto3.client('dynamodb', region_name=ddb_region)
            resp = client.get_item(
                TableName=TABLE_NAME,
                Key={'config_key': {'S': 'active_region'}},
                ConsistentRead=False
            )
            
            if 'Item' in resp:
                region = resp['Item']['active_region']['S']
                CACHE = {
                    'region': region,
                    'expires': now + CACHE_TTL,
                    'last_known': region
                }
                logger.info(f"DDB read from {ddb_region}: {region}")
                return region
            else:
                logger.warning(f"No item found in DDB {ddb_region}")
        except Exception as e:
            logger.warning(f"DDB read failed for {ddb_region}: {e}")
            continue
    
    # Fallback: last-known-good or default to DR
    fallback = CACHE.get('last_known') or 'us-west-2'
    logger.warning(f"All DDB reads failed, using fallback: {fallback}")
    return fallback


def sign_s3_request(request, bucket, region, uri):
    """
    Sign the request with SigV4 for S3 authentication.
    """
    session = boto3.Session()
    credentials = session.get_credentials().get_frozen_credentials()
    
    host = f"{bucket}.s3.{region}.amazonaws.com"
    url = f"https://{host}{uri}"
    
    headers = {
        'Host': host,
        'x-amz-content-sha256': 'UNSIGNED-PAYLOAD'
    }
    
    aws_request = AWSRequest(method='GET', url=url, headers=headers)
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
    
    logger.info(f"Signed S3 request for s3://{bucket}{uri} in {region}")
    return request


def generate_redirect_response(url, cache_seconds=300):
    """
    Generate a 302 redirect response with caching.
    """
    return {
        'status': '302',
        'statusDescription': 'Found',
        'headers': {
            'location': [{'key': 'Location', 'value': url}],
            'cache-control': [{'key': 'Cache-Control', 'value': f'public, max-age={cache_seconds}'}]
        }
    }


def route_to_api_gateway(request, api_origin, uri):
    """
    Route request to API Gateway origin.
    API Gateway handles its own authentication, no signing needed.
    """
    host = api_origin['domainName']
    path = api_origin['path']
    
    # Set the origin to API Gateway
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
    
    # Update Host header for API Gateway
    request['headers']['host'] = [{'key': 'Host', 'value': host}]
    
    logger.info(f"Routing to API Gateway: {host}{path}{uri}")
    return request


def handler(event, context):
    """
    Lambda@Edge origin-request handler.
    
    Routes traffic based on subdomain:
    - admin → 302 redirect to Amazon Connect Admin
    - agent, chat → S3 bucket (static content)
    - chat-api → API Gateway (APIs)
    """
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
    
    # Get active region from DynamoDB (pass event for edge-aware routing)
    active_region = get_active_region(event)
    
    # Route based on subdomain type
    if subdomain == 'admin':
        # Server-side redirect to Amazon Connect Admin
        connect_url = CONNECT_ADMIN_URLS.get(active_region, CONNECT_ADMIN_URLS['us-east-1'])
        logger.info(f"Admin redirect to {active_region}: {connect_url}")
        return generate_redirect_response(connect_url)
    elif subdomain == 'chat-api':
        # API Gateway routing
        api_origin = API_ORIGINS.get(active_region, API_ORIGINS['us-east-1'])
        request = route_to_api_gateway(request, api_origin, request['uri'])
        logger.info(f"API routing to {active_region}: {request['uri']}")
    else:
        # S3 static content routing (agent, chat, etc.)
        folder_map = {'agent': '/agent', 'chat': '/chat'}
        folder_prefix = folder_map.get(subdomain, '')
        
        # Rewrite URI to include folder prefix
        original_uri = request['uri']
        if folder_prefix and not original_uri.startswith(folder_prefix):
            request['uri'] = folder_prefix + original_uri
            logger.info(f"URI rewrite: {original_uri} → {request['uri']}")
        
        # Append index.html for directory requests
        if request['uri'].endswith('/'):
            request['uri'] += 'index.html'
            logger.info(f"Added index.html: {request['uri']}")
        
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


# For local testing
if __name__ == '__main__':
    test_event = {
        'Records': [{
            'cf': {
                'request': {
                    'uri': '/',
                    'headers': {
                        'x-original-host': [{'key': 'x-original-host', 'value': 'chat-api.prod.gsa.dos.macp.cloud'}]
                    }
                }
            }
        }]
    }
    
    result = handler(test_event, None)
    print(f"Result: {result}")
