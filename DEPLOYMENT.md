# Deployment Guide - MACP Connect DR Infrastructure

## Pre-Deployment Checklist

- [ ] AWS CLI configured with appropriate credentials
- [ ] Access to account 417886991978
- [ ] ACM certificate exists: arn:aws:acm:us-east-1:417886991978:certificate/e925eda1-90b5-4607-8ad2-c2e924a3bb71
- [ ] WAF Web ACL exists and ARN is correct
- [ ] CloudFront logging bucket exists: maximus-cloudfront-logs-417886991978-us-east-1
- [ ] Route 53 hosted zone exists: Z10445293PTGB9ZOBN0G8

## Deployment Order

### Step 1: Deploy Infrastructure to Primary Region (us-east-1)

```bash
cd /home/eric/macp-dr/cloudformation

aws cloudformation create-stack \
  --stack-name macp-dr-infrastructure-east \
  --template-body file://admin-portal-infrastructure.yaml \
  --parameters \
    ParameterKey=Environment,ParameterValue=prod \
    ParameterKey=Region,ParameterValue=us-east-1 \
    ParameterKey=ConnectInstanceAlias,ParameterValue=macp-dos-prod-connect-1 \
  --region us-east-1 \
  --tags Key=Environment,Value=prod Key=Purpose,Value=DR-Primary

# Monitor stack creation (takes 15-20 minutes for CloudFront)
aws cloudformation wait stack-create-complete \
  --stack-name macp-dr-infrastructure-east \
  --region us-east-1

# Get outputs
aws cloudformation describe-stacks \
  --stack-name macp-dr-infrastructure-east \
  --region us-east-1 \
  --query 'Stacks[0].Outputs'
```

**Save the DistributionDomainName output - you'll need it for Step 3!**

### Step 2: Deploy Infrastructure to DR Region (us-west-2)

```bash
aws cloudformation create-stack \
  --stack-name macp-dr-infrastructure-west \
  --template-body file://admin-portal-infrastructure.yaml \
  --parameters \
    ParameterKey=Environment,ParameterValue=prod \
    ParameterKey=Region,ParameterValue=us-west-2 \
    ParameterKey=ConnectInstanceAlias,ParameterValue=macp-dos-prod-connect-2 \
  --region us-west-2 \
  --tags Key=Environment,Value=prod Key=Purpose,Value=DR-Secondary

# Monitor stack creation
aws cloudformation wait stack-create-complete \
  --stack-name macp-dr-infrastructure-west \
  --region us-west-2

# Get outputs
aws cloudformation describe-stacks \
  --stack-name macp-dr-infrastructure-west \
  --region us-west-2 \
  --query 'Stacks[0].Outputs'
```

**Save the DistributionDomainName output for Step 3!**

### Step 3: Upload Content to S3 Buckets

```bash
# Get bucket names from stack outputs
BUCKET_EAST=$(aws cloudformation describe-stacks \
  --stack-name macp-dr-infrastructure-east \
  --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`ContentBucketName`].OutputValue' \
  --output text)

BUCKET_WEST=$(aws cloudformation describe-stacks \
  --stack-name macp-dr-infrastructure-west \
  --region us-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`ContentBucketName`].OutputValue' \
  --output text)

# Upload admin redirect page to both buckets
aws s3 cp /home/eric/macp-dr/admin-redirect-test.html \
  s3://${BUCKET_EAST}/admin/index.html \
  --region us-east-1

aws s3 cp /home/eric/macp-dr/admin-redirect-test.html \
  s3://${BUCKET_WEST}/admin/index.html \
  --region us-west-2

# Verify uploads
aws s3 ls s3://${BUCKET_EAST}/admin/ --region us-east-1
aws s3 ls s3://${BUCKET_WEST}/admin/ --region us-west-2
```

### Step 4: Deploy Route 53 Failover DNS (OPTIONAL - READ WARNING)

⚠️ **WARNING**: This will MODIFY your existing DNS records. Current traffic will be affected.

**Before deploying:**
1. Review the existing Route 53 records
2. Understand that this will replace simple alias records with failover routing
3. Consider deploying to a test subdomain first

```bash
# Get CloudFront distribution domain names
DIST_EAST=$(aws cloudformation describe-stacks \
  --stack-name macp-dr-infrastructure-east \
  --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
  --output text)

DIST_WEST=$(aws cloudformation describe-stacks \
  --stack-name macp-dr-infrastructure-west \
  --region us-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
  --output text)

echo "Primary CloudFront: $DIST_EAST"
echo "DR CloudFront: $DIST_WEST"

# Deploy Route 53 stack
aws cloudformation create-stack \
  --stack-name macp-dr-route53-failover \
  --template-body file://route53-failover.yaml \
  --parameters \
    ParameterKey=Environment,ParameterValue=prod \
    ParameterKey=PrimaryCloudFrontDistribution,ParameterValue=$DIST_EAST \
    ParameterKey=DRCloudFrontDistribution,ParameterValue=$DIST_WEST \
  --region us-east-1

# Monitor
aws cloudformation wait stack-create-complete \
  --stack-name macp-dr-route53-failover \
  --region us-east-1
```

## Post-Deployment Validation

### Verify CloudFront Distributions

```bash
# Test primary distribution
curl -I https://admin.prod.gsa.dos.macp.cloud

# Check CloudFront distribution status
aws cloudfront get-distribution \
  --id $(aws cloudformation describe-stacks \
    --stack-name macp-dr-infrastructure-east \
    --region us-east-1 \
    --query 'Stacks[0].Outputs[?OutputKey==`DistributionId`].OutputValue' \
    --output text) \
  --query 'Distribution.Status'
```

### Verify Health Checks

```bash
# Get health check IDs
aws cloudformation describe-stacks \
  --stack-name macp-dr-route53-failover \
  --region us-east-1 \
  --query 'Stacks[0].Outputs'

# Check health status
aws route53 get-health-check-status \
  --health-check-id <primary-health-check-id>
```

### Verify DNS Resolution

```bash
# Check DNS records
dig admin.prod.gsa.dos.macp.cloud
dig agent.prod.gsa.dos.macp.cloud

# Verify failover records exist
aws route53 list-resource-record-sets \
  --hosted-zone-id Z10445293PTGB9ZOBN0G8 \
  --query "ResourceRecordSets[?Name=='admin.prod.gsa.dos.macp.cloud.']"
```

## Rollback Procedures

### If Infrastructure Stack Fails

```bash
# Delete stack (CloudFront takes 15-20 min to delete)
aws cloudformation delete-stack \
  --stack-name macp-dr-infrastructure-east \
  --region us-east-1

# Monitor deletion
aws cloudformation wait stack-delete-complete \
  --stack-name macp-dr-infrastructure-east \
  --region us-east-1
```

### If Route 53 Stack Needs Rollback

```bash
# This will restore original DNS records
aws cloudformation delete-stack \
  --stack-name macp-dr-route53-failover \
  --region us-east-1

# You may need to manually re-create simple alias records
```

## Estimated Deployment Times

| Step | Time |
|------|------|
| CloudFront creation | 15-20 minutes |
| S3 bucket creation | 1-2 minutes |
| Route 53 changes | 1-2 minutes |
| DNS propagation | 60 seconds (TTL) |
| **Total per region** | **~20 minutes** |

## Troubleshooting

### CloudFront Returns 403 Forbidden

- Check S3 bucket policy includes CloudFront OAC
- Verify content exists in S3 at correct path (/admin/index.html)
- Check CloudFront distribution is deployed

### Health Check Failing

- Verify Connect instance URLs are accessible
- Check health check configuration (HTTPS, port 443)
- Review CloudWatch metrics for health check

### DNS Not Resolving

- Check Route 53 hosted zone delegation
- Verify CloudFront aliases match DNS records
- Confirm ACM certificate includes all domain names

## Cost Estimate

Per region, per month:
- CloudFront: ~$10-50 (traffic dependent)
- S3: ~$1-5 (storage + requests)
- Route 53 health checks: ~$1 each
- **Total: ~$25-100/month for both regions**

## Next Steps After Validation

Once infrastructure is validated:
1. Upload actual CCP/Agent content to /agent path
2. Upload chat widget to /chat path  
3. Deploy Lambda/API Gateway for config/health/chat-api endpoints
4. Create Step Functions workflow for controlled failover
5. Set up CloudWatch dashboards and alarms
