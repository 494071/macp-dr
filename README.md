# MACP Connect DR Infrastructure

## Overview

This project implements a Disaster Recovery solution for MACP Connect using Lambda@Edge with DynamoDB Global Table control signals.

**Key Features:**
- **Single CloudFront distribution** with multiple subdomain aliases
- **Lambda@Edge** for dynamic routing based on DynamoDB control signal
- **DynamoDB Global Table** replicated to us-east-1 and us-west-2
- **Manual failover** via Portal UI or DynamoDB update (~30-60 second RTO)
- **Health endpoint** for real-time status monitoring
- **Failover API** for programmatic failover and cache invalidation

## Architecture

```
                    Route 53
                       │
     ┌─────────────────┼─────────────────┐
     │                 │                 │
     ▼                 ▼                 ▼
  admin.prod...    agent.prod...    portal.prod...
     │                 │                 │
     └─────────────────┼─────────────────┘
                       │
                       ▼
           ┌───────────────────────┐
           │  CloudFront (Single)  │
           │  Lambda@Edge ─────────┼──▶ DynamoDB Global Table
           └───────────┬───────────┘    (us-east-1 + us-west-2)
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
    S3 us-east-1              S3 us-west-2
    (Primary)                 (DR)
```

## Subdomains

| Subdomain | Type | Purpose | Cache |
|-----------|------|---------|-------|
| `portal.prod.gsa.dos.macp.cloud` | S3 | DR management dashboard | 24hr |
| `health.prod.gsa.dos.macp.cloud` | Lambda@Edge | JSON status endpoint | 5 sec |
| `admin.prod.gsa.dos.macp.cloud` | Lambda@Edge redirect | Connect Admin console | 60 sec |
| `agent.prod.gsa.dos.macp.cloud` | S3 (dynamic) | Agent workspace redirect | 24hr* |
| `chat.prod.gsa.dos.macp.cloud` | S3 | Chat widget assets | 24hr |
| `failover-api.prod.gsa.dos.macp.cloud` | API Gateway | Failover/invalidation API | none |

*Agent pages fetch health endpoint for dynamic routing, so updates take ~5 seconds.

## Failover Methods

### Method 1: Portal UI (Recommended)

1. Go to https://portal.prod.gsa.dos.macp.cloud
2. Select target region in the Region Failover section
3. Optionally add a reason
4. Click "Initiate Failover" and confirm
5. Portal automatically invalidates CloudFront cache

### Method 2: Failover API

```bash
# Failover to DR region
curl -X POST https://failover-api.prod.gsa.dos.macp.cloud/failover \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_API_KEY" \
  -d '{"region": "us-west-2", "reason": "DR test"}'

# Manual cache invalidation only
curl -X POST https://failover-api.prod.gsa.dos.macp.cloud/invalidate \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_API_KEY" \
  -d '{}'
```

### Method 3: CLI (Manual)

```bash
# Step 1: Update DynamoDB
aws dynamodb put-item --region us-east-1 \
  --table-name macp-dr-prod-failover-state \
  --item '{
    "config_key":{"S":"active_region"},
    "active_region":{"S":"us-west-2"},
    "updated_at":{"S":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"},
    "updated_by":{"S":"operator"},
    "reason":{"S":"Manual failover"}
  }'

# Step 2: Invalidate CloudFront cache (REQUIRED)
aws cloudfront create-invalidation \
  --distribution-id E1KLVY7Q1RG0RK \
  --paths "/*"
```

## Check Current Status

```bash
# Via health endpoint
curl https://health.prod.gsa.dos.macp.cloud/

# Via CLI
aws dynamodb get-item --region us-east-1 \
  --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}'
```

## Deployment

```bash
cd cdk
npm install
npm run build

# Deploy in order:
npx cdk deploy Option7DrBucketStack  # us-west-2 bucket first
npx cdk deploy Option7Stack          # Main stack (us-east-1)
npx cdk deploy FailoverApiStack      # Failover API

# Sync static content
cd .. && ./sync-content.sh
```

## Project Structure

```
├── cdk/                          # AWS CDK implementation (authoritative)
│   ├── bin/macp-dr.ts           # CDK app entry point
│   ├── lib/
│   │   ├── macp-dr-stack.ts     # Main stack (CloudFront, Lambda@Edge, DDB)
│   │   ├── dr-bucket-stack.ts   # DR bucket (us-west-2)
│   │   ├── failover-api-stack.ts # Failover API (API Gateway + Lambda)
│   │   └── chat-api-stack.ts    # Chat API
│   └── lambda/
│       ├── origin_router.py     # Lambda@Edge origin router
│       └── failover-api/        # Failover API Lambda
├── content/                      # Static content for S3
│   ├── us-east-1/               # Primary region content
│   │   ├── agent/index.html     # Dynamic agent redirect
│   │   └── portal/index.html    # DR portal dashboard
│   └── us-west-2/               # DR region content (mirrors east)
├── sync-content.sh              # Sync content to both S3 buckets
├── dr-dashboard.html            # Legacy dashboard (reference)
└── research/                    # Previous architecture explorations
```

## Key Resources

| Resource | ID/ARN |
|----------|--------|
| CloudFront Distribution | E1KLVY7Q1RG0RK |
| DynamoDB Table | macp-dr-prod-failover-state |
| Primary S3 Bucket | macp-dr-opt7-content-prod-us-east-1 |
| DR S3 Bucket | macp-dr-opt7-content-prod-us-west-2 |
| Hosted Zone | Z10445293PTGB9ZOBN0G8 |
| AWS Account | 417886991978 |

## Cost Estimate

| Component | Monthly Cost |
|-----------|-------------|
| CloudFront distribution | ~$5-10 |
| S3 buckets (2 regions) | ~$2-5 |
| DynamoDB Global Table | ~$1-3 |
| Lambda@Edge | ~$1-5 |
| API Gateway (failover API) | ~$1 |
| Route 53 | ~$1 |
| **Total** | **~$12-25/month** |
