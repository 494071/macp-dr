"""
Failover API Lambda - Handles region failover and cache invalidation requests

This Lambda function:
1. POST /failover - Updates DynamoDB and triggers CloudFront invalidation
2. POST /invalidate - Triggers CloudFront cache invalidation only
"""

import json
import boto3
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration
TABLE_NAME = os.environ.get('TABLE_NAME', 'macp-dr-prod-failover-state')
DISTRIBUTION_ID = os.environ.get('DISTRIBUTION_ID', 'E1KLVY7Q1RG0RK')
VALID_REGIONS = ['us-east-1', 'us-west-2']

# Initialize clients
dynamodb = boto3.client('dynamodb')
cloudfront = boto3.client('cloudfront')


def handler(event, context):
    """
    Route requests to appropriate handler based on path.
    """
    logger.info(f"Event: {json.dumps(event)}")
    
    # Handle CORS preflight
    if event.get('httpMethod') == 'OPTIONS':
        return cors_response(200, {'message': 'OK'})
    
    # Route based on path
    path = event.get('path', '')
    
    if path == '/failover':
        return handle_failover(event)
    elif path == '/invalidate':
        return handle_invalidate(event)
    else:
        return cors_response(404, {'error': f'Unknown path: {path}'})


def handle_failover(event):
    """
    Handle failover requests.
    
    Expected request body:
    {
        "region": "us-east-1" | "us-west-2",
        "reason": "Optional reason for failover"
    }
    """
    # Only allow POST
    if event.get('httpMethod') != 'POST':
        return cors_response(405, {'error': 'Method not allowed'})
    
    # Parse request body
    try:
        body = json.loads(event.get('body', '{}'))
    except json.JSONDecodeError:
        return cors_response(400, {'error': 'Invalid JSON body'})
    
    # Validate region
    target_region = body.get('region')
    if not target_region:
        return cors_response(400, {'error': 'Missing required field: region'})
    
    if target_region not in VALID_REGIONS:
        return cors_response(400, {
            'error': f'Invalid region. Must be one of: {VALID_REGIONS}'
        })
    
    # Get optional fields
    reason = body.get('reason', '')
    
    # Extract API key name for audit (if available)
    api_key_id = event.get('requestContext', {}).get('identity', {}).get('apiKeyId', 'unknown')
    
    try:
        # Get current state first
        current = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={'config_key': {'S': 'active_region'}}
        )
        current_region = current.get('Item', {}).get('active_region', {}).get('S', 'unknown')
        
        # Check if already in target region
        if current_region == target_region:
            return cors_response(200, {
                'status': 'no_change',
                'message': f'Already active in {target_region}',
                'active_region': target_region
            })
        
        # Update DynamoDB
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                'config_key': {'S': 'active_region'},
                'active_region': {'S': target_region},
                'updated_at': {'S': timestamp},
                'updated_by': {'S': f'portal-api:{api_key_id}'},
                'reason': {'S': reason},
                'previous_region': {'S': current_region}
            }
        )
        logger.info(f"DynamoDB updated: {current_region} -> {target_region}")
        
        # Trigger CloudFront invalidation
        invalidation_id = None
        try:
            invalidation = cloudfront.create_invalidation(
                DistributionId=DISTRIBUTION_ID,
                InvalidationBatch={
                    'Paths': {
                        'Quantity': 1,
                        'Items': ['/*']
                    },
                    'CallerReference': f'failover-{timestamp}-{target_region}'
                }
            )
            invalidation_id = invalidation['Invalidation']['Id']
            logger.info(f"CloudFront invalidation created: {invalidation_id}")
        except Exception as e:
            logger.error(f"CloudFront invalidation failed: {e}")
            # Continue - DDB update succeeded, invalidation can be done manually
        
        return cors_response(200, {
            'status': 'success',
            'message': f'Failover initiated from {current_region} to {target_region}',
            'active_region': target_region,
            'previous_region': current_region,
            'updated_at': timestamp,
            'invalidation_id': invalidation_id,
            'note': 'CloudFront invalidation may take 30-60 seconds to propagate globally'
        })
        
    except Exception as e:
        logger.error(f"Failover failed: {e}")
        return cors_response(500, {
            'error': 'Failover failed',
            'details': str(e)
        })


def handle_invalidate(event):
    """
    Handle cache invalidation requests.
    
    Optional request body:
    {
        "paths": ["/path1", "/path2"]  // defaults to ["/*"] if not provided
    }
    """
    # Only allow POST
    if event.get('httpMethod') != 'POST':
        return cors_response(405, {'error': 'Method not allowed'})
    
    # Parse request body
    try:
        body = json.loads(event.get('body', '{}')) if event.get('body') else {}
    except json.JSONDecodeError:
        return cors_response(400, {'error': 'Invalid JSON body'})
    
    # Get paths to invalidate (default to all)
    paths = body.get('paths', ['/*'])
    if not isinstance(paths, list):
        paths = [paths]
    
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    try:
        invalidation = cloudfront.create_invalidation(
            DistributionId=DISTRIBUTION_ID,
            InvalidationBatch={
                'Paths': {
                    'Quantity': len(paths),
                    'Items': paths
                },
                'CallerReference': f'manual-{timestamp}'
            }
        )
        invalidation_id = invalidation['Invalidation']['Id']
        logger.info(f"CloudFront invalidation created: {invalidation_id} for paths: {paths}")
        
        return cors_response(200, {
            'status': 'success',
            'message': 'Cache invalidation initiated',
            'invalidation_id': invalidation_id,
            'paths': paths,
            'timestamp': timestamp,
            'note': 'Invalidation may take 30-60 seconds to propagate globally'
        })
        
    except Exception as e:
        logger.error(f"Invalidation failed: {e}")
        return cors_response(500, {
            'error': 'Invalidation failed',
            'details': str(e)
        })


def cors_response(status_code, body):
    """Return response with CORS headers."""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, x-api-key'
        },
        'body': json.dumps(body)
    }
