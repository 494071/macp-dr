"""
Lambda@Edge Origin Router for Option 7 DR Architecture

This function runs on CloudFront origin-request events and routes traffic
to the active region based on a DynamoDB control signal.

Supports:
- S3 buckets for static content (admin, agent, chat subdomains)
- API Gateway for APIs (chat-api subdomain)

Features:
- Dynamic origin switching based on DynamoDB control signal
- SigV4 request signing for S3 authentication
- Multi-region DynamoDB read with fallback (us-east-1 → us-west-2)
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


def get_active_region():
    """
    Read active region from DynamoDB with caching and multi-region fallback.
    """
    global CACHE
    now = time.time()
    
    # Return cached value if fresh
    if now < CACHE['expires'] and CACHE['region']:
        logger.info(f"Cache hit: {CACHE['region']}")
        return CACHE['region']
    
    # Try each DDB replica in order
    for ddb_region in ['us-east-1', 'us-west-2']:
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
    - admin, agent, chat → S3 bucket (static content)
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
    
    # Get active region from DynamoDB
    active_region = get_active_region()
    
    # Route based on subdomain type
    if subdomain == 'chat-api':
        # API Gateway routing
        api_origin = API_ORIGINS.get(active_region, API_ORIGINS['us-east-1'])
        request = route_to_api_gateway(request, api_origin, request['uri'])
        logger.info(f"API routing to {active_region}: {request['uri']}")
    else:
        # S3 static content routing
        folder_map = {'admin': '/admin', 'agent': '/agent', 'chat': '/chat'}
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
