#!/bin/bash
# MACP Connect DR - Manual Failover Script
# This script performs instant failover between primary and DR regions

set -e

# Configuration
STACK_NAME="macp-dr-dns"
PRIMARY_CF="d1vhple0dnr5gl.cloudfront.net"
DR_CF="d3nh2q65al3cia.cloudfront.net"
TEMPLATE_FILE="cloudformation/route53-manual-failover.yaml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

usage() {
    echo "Usage: $0 {status|failover|failback|test}"
    echo ""
    echo "Commands:"
    echo "  status    - Show current active region and health check status"
    echo "  failover  - Switch traffic to DR region (us-west-2)"
    echo "  failback  - Switch traffic back to primary region (us-east-1)"
    echo "  test      - Test connectivity to both CloudFront distributions"
    echo ""
    exit 1
}

check_status() {
    echo -e "${YELLOW}=== MACP Connect DR Status ===${NC}"
    echo ""
    
    # Get current active distribution from stack
    CURRENT=$(aws cloudformation describe-stacks \
        --stack-name $STACK_NAME \
        --query "Stacks[0].Parameters[?ParameterKey=='ActiveCloudFrontDomain'].ParameterValue" \
        --output text 2>/dev/null || echo "Stack not found")
    
    if [ "$CURRENT" == "$PRIMARY_CF" ]; then
        echo -e "Active Region: ${GREEN}PRIMARY (us-east-1)${NC}"
        echo "CloudFront: $PRIMARY_CF"
    elif [ "$CURRENT" == "$DR_CF" ]; then
        echo -e "Active Region: ${RED}DR (us-west-2)${NC}"
        echo "CloudFront: $DR_CF"
    else
        echo -e "Active Region: ${RED}UNKNOWN${NC}"
        echo "Current value: $CURRENT"
    fi
    
    echo ""
    echo "Health Checks:"
    
    # Get health check status
    aws route53 list-health-checks \
        --query "HealthChecks[?HealthCheckConfig.FullyQualifiedDomainName!=null].{Name:HealthCheckConfig.FullyQualifiedDomainName,Id:Id}" \
        --output table 2>/dev/null | grep -E "(macp|connect)" || echo "No health checks found"
}

do_failover() {
    echo -e "${YELLOW}=== Initiating Failover to DR Region ===${NC}"
    echo ""
    echo "This will switch all traffic from:"
    echo "  PRIMARY (us-east-1): $PRIMARY_CF"
    echo "  DR (us-west-2):      $DR_CF"
    echo ""
    read -p "Are you sure you want to failover to DR? (yes/no): " confirm
    
    if [ "$confirm" != "yes" ]; then
        echo "Failover cancelled."
        exit 0
    fi
    
    echo ""
    echo "Updating DNS to point to DR region..."
    
    aws cloudformation update-stack \
        --stack-name $STACK_NAME \
        --use-previous-template \
        --parameters ParameterKey=ActiveCloudFrontDomain,ParameterValue=$DR_CF \
                     ParameterKey=Environment,UsePreviousValue=true \
                     ParameterKey=PortalDomain,UsePreviousValue=true \
                     ParameterKey=HostedZoneId,UsePreviousValue=true \
                     ParameterKey=EnableHealthChecks,UsePreviousValue=true \
                     ParameterKey=PrimaryConnectInstance,UsePreviousValue=true \
                     ParameterKey=DRConnectInstance,UsePreviousValue=true
    
    echo ""
    echo -e "${GREEN}Failover initiated!${NC}"
    echo ""
    echo "Waiting for stack update to complete..."
    aws cloudformation wait stack-update-complete --stack-name $STACK_NAME
    
    echo ""
    echo -e "${GREEN}Failover complete!${NC}"
    echo "Traffic is now routing to DR region (us-west-2)"
    echo ""
    echo "DNS propagation typically takes 60 seconds within AWS."
    echo "Run '$0 test' to verify connectivity."
}

do_failback() {
    echo -e "${YELLOW}=== Initiating Failback to Primary Region ===${NC}"
    echo ""
    echo "This will switch all traffic from:"
    echo "  DR (us-west-2):      $DR_CF"
    echo "  PRIMARY (us-east-1): $PRIMARY_CF"
    echo ""
    read -p "Are you sure you want to failback to PRIMARY? (yes/no): " confirm
    
    if [ "$confirm" != "yes" ]; then
        echo "Failback cancelled."
        exit 0
    fi
    
    echo ""
    echo "Updating DNS to point to primary region..."
    
    aws cloudformation update-stack \
        --stack-name $STACK_NAME \
        --use-previous-template \
        --parameters ParameterKey=ActiveCloudFrontDomain,ParameterValue=$PRIMARY_CF \
                     ParameterKey=Environment,UsePreviousValue=true \
                     ParameterKey=PortalDomain,UsePreviousValue=true \
                     ParameterKey=HostedZoneId,UsePreviousValue=true \
                     ParameterKey=EnableHealthChecks,UsePreviousValue=true \
                     ParameterKey=PrimaryConnectInstance,UsePreviousValue=true \
                     ParameterKey=DRConnectInstance,UsePreviousValue=true
    
    echo ""
    echo -e "${GREEN}Failback initiated!${NC}"
    echo ""
    echo "Waiting for stack update to complete..."
    aws cloudformation wait stack-update-complete --stack-name $STACK_NAME
    
    echo ""
    echo -e "${GREEN}Failback complete!${NC}"
    echo "Traffic is now routing to PRIMARY region (us-east-1)"
    echo ""
    echo "DNS propagation typically takes 60 seconds within AWS."
    echo "Run '$0 test' to verify connectivity."
}

test_connectivity() {
    echo -e "${YELLOW}=== Testing CloudFront Connectivity ===${NC}"
    echo ""
    
    echo "Testing PRIMARY (us-east-1): $PRIMARY_CF"
    PRIMARY_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://$PRIMARY_CF/admin/index.html" 2>/dev/null || echo "000")
    if [ "$PRIMARY_STATUS" == "200" ]; then
        echo -e "  Status: ${GREEN}$PRIMARY_STATUS OK${NC}"
    else
        echo -e "  Status: ${RED}$PRIMARY_STATUS${NC}"
    fi
    
    echo ""
    echo "Testing DR (us-west-2): $DR_CF"
    DR_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://$DR_CF/admin/index.html" 2>/dev/null || echo "000")
    if [ "$DR_STATUS" == "200" ]; then
        echo -e "  Status: ${GREEN}$DR_STATUS OK${NC}"
    else
        echo -e "  Status: ${RED}$DR_STATUS${NC}"
    fi
    
    echo ""
    echo "Testing Portal Domain: portal.prod.gsa.dos.macp.cloud"
    PORTAL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://portal.prod.gsa.dos.macp.cloud/admin/index.html" 2>/dev/null || echo "000")
    PORTAL_CF=$(curl -s -I "https://portal.prod.gsa.dos.macp.cloud/admin/index.html" 2>/dev/null | grep -i "x-amz-cf-pop" | head -1 || echo "unknown")
    if [ "$PORTAL_STATUS" == "200" ]; then
        echo -e "  Status: ${GREEN}$PORTAL_STATUS OK${NC}"
    else
        echo -e "  Status: ${RED}$PORTAL_STATUS${NC}"
    fi
    echo "  Edge: $PORTAL_CF"
}

# Main
case "$1" in
    status)
        check_status
        ;;
    failover)
        do_failover
        ;;
    failback)
        do_failback
        ;;
    test)
        test_connectivity
        ;;
    *)
        usage
        ;;
esac
