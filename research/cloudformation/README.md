# CloudFormation Templates for MACP Connect DR

This directory contains CloudFormation templates for deploying the MACP Connect Disaster Recovery infrastructure.

## Templates

### `admin-portal-infrastructure.yaml`
Creates CloudFront + S3 infrastructure for the Admin Portal redirect page.

**Resources Created:**
- S3 bucket with versioning and encryption
- S3 bucket policy (HTTPS-only + CloudFront OAC access)
- CloudFront Origin Access Control (OAC)
- CloudFront distribution with:
  - Custom domain support (ACM certificate)
  - Geo-restriction (US only)
  - WAF integration
  - Access logging
  - TLS 1.3

**Usage:**

```bash
# Deploy to us-east-1 (Primary)
aws cloudformation create-stack \
  --stack-name macp-dr-admin-portal-east \
  --template-body file://admin-portal-infrastructure.yaml \
  --parameters \
    ParameterKey=Environment,ParameterValue=prod \
    ParameterKey=Region,ParameterValue=us-east-1 \
    ParameterKey=ConnectInstanceAlias,ParameterValue=macp-dos-prod-connect-1 \
  --region us-east-1

# Deploy to us-west-2 (DR)
aws cloudformation create-stack \
  --stack-name macp-dr-admin-portal-west \
  --template-body file://admin-portal-infrastructure.yaml \
  --parameters \
    ParameterKey=Environment,ParameterValue=prod \
    ParameterKey=Region,ParameterValue=us-west-2 \
    ParameterKey=ConnectInstanceAlias,ParameterValue=macp-dos-prod-connect-2 \
  --region us-west-2
```

**Update Stack:**
```bash
aws cloudformation update-stack \
  --stack-name macp-dr-admin-portal-east \
  --template-body file://admin-portal-infrastructure.yaml \
  --parameters \
    ParameterKey=Environment,ParameterValue=prod \
    ParameterKey=Region,ParameterValue=us-east-1 \
  --region us-east-1
```

**Upload Content to S3:**
```bash
# Get bucket name from stack output
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name macp-dr-admin-portal-east \
  --query 'Stacks[0].Outputs[?OutputKey==`ContentBucketName`].OutputValue' \
  --output text \
  --region us-east-1)

# Upload admin redirect page
aws s3 cp ../admin-redirect-test.html s3://${BUCKET}/admin/index.html
```

## Template Structure

All templates follow these conventions:

1. **Parameters**: Configurable values (environment, region, domains)
2. **Resources**: AWS resources to create
3. **Outputs**: Exported values for cross-stack references

## Next Templates to Create

Based on the DR plan, additional templates needed:

- [ ] `route53-health-checks.yaml` - Health checks and failover DNS records
- [ ] `chat-config-lambda.yaml` - Lambda + API Gateway for chat configuration
- [ ] `admin-config-api.yaml` - Lambda + API Gateway + DynamoDB for admin redirect config
- [ ] `failover-automation.yaml` - Step Functions workflow for semi-automated failover
- [ ] `ccp-workspace-infrastructure.yaml` - CloudFront + S3 for CCP/Workspace

## Parameters to Customize

Before deploying, review and update these parameters:

- `ACMCertificateArn`: Must be in us-east-1 (CloudFront requirement)
- `WebACLArn`: WAF Web ACL for security
- `CloudFrontLoggingBucket`: Existing bucket for access logs
- `BaseDomain`: Your custom domain
- `ConnectInstanceAlias`: Your Connect instance alias per region

## Security Features

All templates include:

- ✅ S3 public access blocked
- ✅ Encryption at rest (AES256)
- ✅ Encryption in transit (HTTPS only)
- ✅ CloudFront Origin Access Control (OAC)
- ✅ Geo-restriction (US only)
- ✅ WAF integration
- ✅ Access logging enabled

## Monitoring

After deployment, monitor:
- CloudFront distribution status
- S3 bucket access logs
- CloudWatch metrics for CloudFront
- WAF logs and metrics
