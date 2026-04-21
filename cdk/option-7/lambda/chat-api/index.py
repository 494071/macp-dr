"""
Chat API Lambda - Hello World

Simple Lambda function that returns a greeting with the AWS region.
Used to verify API Gateway DR routing.
"""

import json
import os


def handler(event, context):
    """
    Lambda handler for Chat API.
    Returns a JSON response with region information.
    """
    region = os.environ.get('AWS_REGION', 'unknown')
    
    response = {
        'message': f'Hello from {region}!',
        'region': region,
        'service': 'chat-api'
    }
    
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        },
        'body': json.dumps(response)
    }
