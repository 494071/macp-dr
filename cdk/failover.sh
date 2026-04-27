#!/bin/bash
#
# MACP DR Failover Script
# Switches active region and invalidates CloudFront cache
#

set -e

# Configuration
TABLE_NAME="macp-dr-prod-failover-state"
DISTRIBUTION_ID="E1KLVY7Q1RG0RK"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

usage() {
    echo "Usage: $0 <east|west|status|invalidate>"
    echo ""
    echo "Commands:"
    echo "  east        - Failover to us-east-1 (Primary)"
    echo "  west        - Failover to us-west-2 (DR)"
    echo "  status      - Check current active region"
    echo "  invalidate  - Invalidate CloudFront cache only"
    echo ""
    exit 1
}

get_status() {
    # Try us-east-1 first, fallback to us-west-2
    aws dynamodb get-item \
        --table-name "$TABLE_NAME" \
        --key '{"config_key":{"S":"active_region"}}' \
        --query 'Item.active_region.S' \
        --output text 2>/dev/null || \
    aws dynamodb get-item \
        --region us-west-2 \
        --table-name "$TABLE_NAME" \
        --key '{"config_key":{"S":"active_region"}}' \
        --query 'Item.active_region.S' \
        --output text
}

invalidate_cache() {
    echo -e "${YELLOW}Invalidating CloudFront cache...${NC}"
    local invalidation_id=$(aws cloudfront create-invalidation \
        --distribution-id "$DISTRIBUTION_ID" \
        --paths "/*" \
        --query 'Invalidation.Id' \
        --output text)
    
    echo -e "${GREEN}✓ Cache invalidation started: $invalidation_id${NC}"
    echo ""
    echo "Note: Invalidation takes ~30-60 seconds to propagate globally."
}

failover() {
    local target_region=$1
    local timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local user=$(whoami)
    
    echo -e "${YELLOW}Current state:${NC}"
    # Try us-east-1 first, fallback to us-west-2 for status check
    local current=$(aws dynamodb get-item \
        --table-name "$TABLE_NAME" \
        --key '{"config_key":{"S":"active_region"}}' \
        --query 'Item.active_region.S' \
        --output text 2>/dev/null || \
        aws dynamodb get-item \
        --region us-west-2 \
        --table-name "$TABLE_NAME" \
        --key '{"config_key":{"S":"active_region"}}' \
        --query 'Item.active_region.S' \
        --output text)
    echo "  Active region: $current"
    echo ""
    
    if [ "$current" == "$target_region" ]; then
        echo -e "${GREEN}Already pointing to $target_region. No action needed.${NC}"
        exit 0
    fi
    
    echo -e "${YELLOW}Switching to $target_region...${NC}"
    
    # Write to us-west-2 replica (works even if us-east-1 is down)
    aws dynamodb put-item \
        --region us-west-2 \
        --table-name "$TABLE_NAME" \
        --item "{\"config_key\":{\"S\":\"active_region\"},\"active_region\":{\"S\":\"$target_region\"},\"updated_at\":{\"S\":\"$timestamp\"},\"updated_by\":{\"S\":\"$user\"}}"
    
    echo -e "${GREEN}✓ DynamoDB updated (us-west-2 replica)${NC}"
    
    # Invalidate CloudFront cache
    echo -e "${YELLOW}Invalidating CloudFront cache...${NC}"
    local invalidation_id=$(aws cloudfront create-invalidation \
        --distribution-id "$DISTRIBUTION_ID" \
        --paths "/*" \
        --query 'Invalidation.Id' \
        --output text)
    
    echo -e "${GREEN}✓ Cache invalidation started: $invalidation_id${NC}"
    
    # Verify (read from us-west-2 to confirm write)
    echo ""
    echo -e "${YELLOW}Verifying...${NC}"
    sleep 2
    local new_state=$(aws dynamodb get-item \
        --region us-west-2 \
        --table-name "$TABLE_NAME" \
        --key '{"config_key":{"S":"active_region"}}' \
        --query 'Item.active_region.S' \
        --output text)
    echo "  Active region: $new_state"
    
    if [ "$new_state" == "$target_region" ]; then
        echo ""
        echo -e "${GREEN}✓ Failover to $target_region complete!${NC}"
        echo ""
        echo "Note: Cache invalidation takes ~30-60 seconds to propagate globally."
        echo "Lambda cache TTL is 15 seconds - new requests will route to $target_region shortly."
    else
        echo -e "${RED}✗ Verification failed. Expected $target_region but got $new_state${NC}"
        exit 1
    fi
}

# Main
case "${1:-}" in
    east)
        failover "us-east-1"
        ;;
    west)
        failover "us-west-2"
        ;;
    status)
        echo "Active region: $(get_status)"
        ;;
    invalidate)
        invalidate_cache
        ;;
    *)
        usage
        ;;
esac
