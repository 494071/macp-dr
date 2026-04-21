"""
Lambda@Edge Origin Router for Option 7 DR Architecture

This function runs on CloudFront origin-request events and routes traffic
to the active region's S3 bucket based on a DynamoDB control signal.

Features:
- Dynamic origin switching based on DynamoDB control signal
- SigV4 request signing for S3 authentication (replaces OAC)
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

ORIGINS = {
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
    This replaces OAC - Lambda signs requests using its IAM role credentials.
    """
    session = boto3.Session()
    credentials = session.get_credentials().get_frozen_credentials()
    
    # Build the S3 URL
    host = f"{bucket}.s3.{region}.amazonaws.com"
    url = f"https://{host}{uri}"
    
    # S3 requires x-amz-content-sha256 header (UNSIGNED-PAYLOAD for GET requests)
    headers = {
        'Host': host,
        'x-amz-content-sha256': 'UNSIGNED-PAYLOAD'
    }
    
    # Create AWS request for signing
    aws_request = AWSRequest(method='GET', url=url, headers=headers)
    
    # Sign the request
    SigV4Auth(credentials, 's3', region).add_auth(aws_request)
    
    # Update CloudFront request with signed headers
    request['origin'] = {
        's3': {
            'domainName': host,
            'region': region,
            'authMethod': 'none',  # We're handling auth ourselves
            'path': '',
            'customHeaders': {}
        }
    }
    
    # Set the Host header
    request['headers']['host'] = [{'key': 'Host', 'value': host}]
    
    # Add SigV4 auth headers
    for header_name, header_value in aws_request.headers.items():
        header_lower = header_name.lower()
        if header_lower in ['authorization', 'x-amz-date', 'x-amz-security-token', 'x-amz-content-sha256']:
            request['headers'][header_lower] = [{'key': header_name, 'value': header_value}]
    
    logger.info(f"Signed request for s3://{bucket}{uri} in {region}")
    return request


def handler(event, context):
    """
    Lambda@Edge origin-request handler.
    
    Routes traffic to the active region's S3 bucket based on DynamoDB control signal.
    Signs requests with SigV4 for S3 authentication.
    
    Also rewrites the URI path based on subdomain:
    - admin.prod.gsa.dos.macp.cloud/foo → /admin/foo
    - agent.prod.gsa.dos.macp.cloud/foo → /agent/foo
    - chat.prod.gsa.dos.macp.cloud/foo  → /chat/foo
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
    
    # Map subdomain to folder path
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
    
    # Get active region from DynamoDB
    active_region = get_active_region()
    origin_config = ORIGINS.get(active_region, ORIGINS['us-east-1'])
    
    # Sign request and switch origin
    request = sign_s3_request(
        request,
        origin_config['bucket'],
        origin_config['region'],
        request['uri']
    )
    
    logger.info(f"Routing to {active_region}: {request['uri']}")
    return request


# For local testing
if __name__ == '__main__':
    test_event = {
        'Records': [{
            'cf': {
                'request': {
                    'uri': '/',
                    'headers': {
                        'x-original-host': [{'key': 'x-original-host', 'value': 'admin.prod.gsa.dos.macp.cloud'}]
                    }
                }
            }
        }]
    }
    
    result = handler(test_event, None)
    print(f"Result: {result}")
