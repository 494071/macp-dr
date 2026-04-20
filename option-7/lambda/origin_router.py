"""
Lambda@Edge Origin Router for Option 7 DR Architecture

This function runs on CloudFront origin-request events and routes traffic
to the active region's S3 bucket based on a DynamoDB control signal.

Features:
- Multi-region DynamoDB read with fallback (us-east-1 → us-west-2)
- In-memory caching (~15s TTL) to reduce DDB calls
- Last-known-good fallback if both DDB replicas fail
- DR-biased default (us-west-2) when no data available
"""

import boto3
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration
# Note: Lambda@Edge doesn't support environment variables, so these are hardcoded
TABLE_NAME = 'macp-dr-prod-failover-state'
CACHE_TTL = 15  # seconds

ORIGINS = {
    'us-east-1': {
        'domainName': 'macp-dr-opt7-content-prod-us-east-1.s3.us-east-1.amazonaws.com',
        'region': 'us-east-1'
    },
    'us-west-2': {
        'domainName': 'macp-dr-opt7-content-prod-us-west-2.s3.us-west-2.amazonaws.com',
        'region': 'us-west-2'
    }
}

# Module-level cache (persists across warm Lambda invocations)
CACHE = {'region': None, 'expires': 0, 'last_known': None}


def get_active_region():
    """
    Read active region from DynamoDB with caching and multi-region fallback.
    
    Read strategy:
    1. Return cached value if fresh (< CACHE_TTL seconds old)
    2. Try reading from us-east-1 DDB replica
    3. Fallback to us-west-2 DDB replica
    4. Use last-known-good value from cache
    5. Default to us-west-2 (DR region) - safer during catastrophic failure
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
                ConsistentRead=False  # Eventually consistent is fine, faster
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


def handler(event, context):
    """
    Lambda@Edge origin-request handler.
    
    Rewrites the request.origin to point to the active region's S3 bucket
    based on the DynamoDB control signal.
    
    The Origin Group in CloudFront acts as a safety net - if the bucket
    we route to returns 5xx/403, CloudFront will automatically retry
    with the other origin in the group.
    """
    request = event['Records'][0]['cf']['request']
    
    # Get active region from DynamoDB (with caching)
    active_region = get_active_region()
    origin_config = ORIGINS.get(active_region, ORIGINS['us-west-2'])
    
    # Rewrite origin to active region's S3 bucket
    request['origin'] = {
        's3': {
            'domainName': origin_config['domainName'],
            'region': origin_config['region'],
            'authMethod': 'origin-access-identity',
            'path': '',
            'customHeaders': {}
        }
    }
    
    # Update Host header to match origin (required for S3)
    request['headers']['host'] = [{
        'key': 'host',
        'value': origin_config['domainName']
    }]
    
    logger.info(f"Routing to {active_region}: {origin_config['domainName']}")
    return request


# For local testing
if __name__ == '__main__':
    # Simulate a CloudFront origin-request event
    test_event = {
        'Records': [{
            'cf': {
                'request': {
                    'uri': '/admin/index.html',
                    'headers': {
                        'host': [{'key': 'host', 'value': 'admin.prod.gsa.dos.macp.cloud'}]
                    }
                }
            }
        }]
    }
    
    result = handler(test_event, None)
    print(f"Result: {result}")
