# Option 7 CDK - Lambda@Edge with DynamoDB Global Table

AWS CDK implementation of Option 7 DR architecture for MACP Connect.

## Architecture

```
                    Route 53
                       │
     ┌─────────────────┼─────────────────┐
     │                 │                 │
     ▼                 ▼                 ▼
  admin.prod...    agent.prod...    chat.prod...
     │                 │                 │
     └─────────────────┼─────────────────┘
                       │
                       ▼
           ┌───────────────────────┐
           │  CloudFront (Single)  │
           │  + CloudFront Function │◄── Copies Host → x-original-host
           │  + Lambda@Edge ────────┼──▶ DynamoDB Global Table
           └───────────┬───────────┘
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
    S3 us-east-1              S3 us-west-2
     (Primary)                   (DR)
```

## How It Works

1. **Route53** resolves `{admin,agent,chat}.prod.gsa.dos.macp.cloud` to CloudFront
2. **CloudFront Function** (viewer-request) copies the `Host` header to `x-original-host`
3. **Lambda@Edge** (origin-request):
   - Reads `active_region` from DynamoDB Global Table
   - Extracts subdomain from `x-original-host` header
   - Rewrites URI: `/` → `/{subdomain}/index.html`
   - Signs request with SigV4 and routes to active region's S3 bucket
4. **S3** serves the content from the appropriate folder

## Stacks

| Stack | Region | Description |
|-------|--------|-------------|
| `Option7DrBucketStack` | us-west-2 | DR S3 bucket (deploy first) |
| `Option7Stack` | us-east-1 | Main stack: DynamoDB, Lambda@Edge, CloudFront, Primary S3, Route53 |

## Resources Created

- **DynamoDB Global Table** - `macp-dr-prod-failover-state` (replicated to us-west-2)
- **S3 Buckets** - `macp-dr-opt7-content-prod-us-east-1` and `macp-dr-opt7-content-prod-us-west-2`
- **Lambda@Edge** - `macp-dr-prod-origin-router` (Python 3.12)
- **CloudFront Function** - `macp-dr-prod-host-passthrough`
- **CloudFront Distribution** - Single distribution with 3 subdomain aliases
- **Route53 A Records** - For admin, agent, chat subdomains

## Prerequisites

- AWS CLI configured
- Node.js 18+
- CDK CLI: `npm install -g aws-cdk`

## Deployment

```bash
# Install dependencies
npm install

# Build
npm run build

# Bootstrap CDK (if first time)
npx cdk bootstrap aws://ACCOUNT_ID/us-east-1
npx cdk bootstrap aws://ACCOUNT_ID/us-west-2

# Deploy DR bucket first (us-west-2)
npx cdk deploy Option7DrBucketStack

# Deploy main stack (us-east-1)
npx cdk deploy Option7Stack

# Seed DynamoDB with initial state
aws dynamodb put-item --table-name macp-dr-prod-failover-state \
  --item '{"config_key":{"S":"active_region"},"active_region":{"S":"us-east-1"}}'
```

## Failover Commands

### Quick One-Liners

```bash
# Failover to DR (us-west-2)
aws dynamodb put-item --table-name macp-dr-prod-failover-state \
  --item '{"config_key":{"S":"active_region"},"active_region":{"S":"us-west-2"}}'

# Failback to Primary (us-east-1)
aws dynamodb put-item --table-name macp-dr-prod-failover-state \
  --item '{"config_key":{"S":"active_region"},"active_region":{"S":"us-east-1"}}'

# Check current state
aws dynamodb get-item --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}' --query 'Item.active_region.S' --output text

# Optional: Invalidate cache for immediate effect
aws cloudfront create-invalidation --distribution-id E1KLVY7Q1RG0RK --paths "/*"
```

### With Metadata

```bash
# Failover to DR (us-west-2) with audit trail
aws dynamodb put-item --table-name macp-dr-prod-failover-state \
  --item '{"config_key":{"S":"active_region"},"active_region":{"S":"us-west-2"},"updated_at":{"S":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"},"updated_by":{"S":"'$(whoami)'"}}'
```

## Timing

- **Lambda Cache TTL**: 15 seconds - failover takes effect within this window
- **Cache Invalidation**: Optional, for immediate effect (~30s to propagate)

## Testing

Open `dr-dashboard.html` in a browser to monitor all 3 subdomains with auto-refresh.

```bash
# Test all subdomains
curl https://admin.prod.gsa.dos.macp.cloud/
curl https://agent.prod.gsa.dos.macp.cloud/
curl https://chat.prod.gsa.dos.macp.cloud/
```

## S3 Content Structure

Both buckets should have identical folder structure:
```
/admin/index.html
/agent/index.html
/chat/index.html
```

## Useful CDK Commands

* `npm run build`   - Compile TypeScript
* `npm run watch`   - Watch for changes
* `npm run test`    - Run tests
* `npx cdk synth`   - Synthesize CloudFormation
* `npx cdk diff`    - Compare with deployed
* `npx cdk deploy`  - Deploy stack
