# MACP Connect DR Infrastructure

## Overview

This project implements a Disaster Recovery solution for MACP Connect using Lambda@Edge with DynamoDB Global Table control signals.

**Key Features:**
- **Single CloudFront distribution** with multiple subdomain aliases
- **Lambda@Edge** for dynamic routing based on DynamoDB control signal
- **DynamoDB Global Table** replicated to us-east-1 and us-west-2
- **Manual failover** via DynamoDB update (~15 second RTO)

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
           │  Aliases: all above   │
           │  Lambda@Edge ─────────┼──▶ DynamoDB Global Table
           └───────────┬───────────┘    (us-east-1 + us-west-2)
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
    S3 us-east-1              S3 us-west-2
    (Primary)                 (DR)
```

**Note:** CloudFront requires at least one origin defined, but Lambda@Edge dynamically
overrides it based on the DynamoDB control signal.

## Deployment Order

Deploy stacks in this order:

| Order | Template | Region | Description |
|-------|----------|--------|-------------|
| 1 | `01-dynamodb-global-table.yaml` | us-east-1 | Creates Global Table with us-west-2 replica |
| 2 | `02-s3-buckets.yaml` | us-east-1 | Primary bucket |
| 3 | `02-s3-buckets.yaml` | us-west-2 | DR bucket |
| 4 | `03-lambda-edge.yaml` | us-east-1 | Lambda@Edge function (must be us-east-1) |
| 5 | `04-cloudfront-distribution.yaml` | us-east-1 | CloudFront with Lambda@Edge |

## Subdomains

| Subdomain | Path in S3 | Purpose |
|-----------|------------|---------|
| `admin.prod.gsa.dos.macp.cloud` | `/admin/` | Connect Admin redirect portal |
| `agent.prod.gsa.dos.macp.cloud` | `/agent/` | CCP / Workspace |
| `chat.prod.gsa.dos.macp.cloud` | `/chat/` | Chat widget assets |

## Failover

| Method | RTO | Trigger |
|--------|-----|---------|
| **Manual (DDB + invalidation)** | ~30-60 sec | Operator updates DynamoDB + invalidates CloudFront cache |

**Important:** CloudFront caching is set to 24 hours. After updating DynamoDB, you **must** 
invalidate the CloudFront cache for failover to take effect immediately.

## Failover Procedure

### Step 1: Update DynamoDB

```bash
# Failover to DR (us-west-2)
aws dynamodb update-item \
  --region us-west-2 \
  --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}' \
  --update-expression "SET active_region = :r, updated_at = :t, updated_by = :u, reason = :m" \
  --expression-attribute-values '{
    ":r":{"S":"us-west-2"},
    ":t":{"S":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"},
    ":u":{"S":"operator"},
    ":m":{"S":"Manual failover"}
  }'
```

### Step 2: Invalidate CloudFront Cache (Required)

```bash
# Get distribution ID
DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Aliases.Items, 'admin.prod.gsa.dos.macp.cloud')].Id" \
  --output text)

# Invalidate all cached content
aws cloudfront create-invalidation --distribution-id $DIST_ID --paths "/*"
```

### Failback to Primary

```bash
# Step 1: Update DynamoDB
aws dynamodb update-item \
  --region us-east-1 \
  --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}' \
  --update-expression "SET active_region = :r, updated_at = :t, updated_by = :u, reason = :m" \
  --expression-attribute-values '{
    ":r":{"S":"us-east-1"},
    ":t":{"S":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"},
    ":u":{"S":"operator"},
    ":m":{"S":"Revert to primary"}
  }'

# Step 2: Invalidate CloudFront cache
aws cloudfront create-invalidation --distribution-id $DIST_ID --paths "/*"
```

## Check Current State

```bash
aws dynamodb get-item \
  --region us-east-1 \
  --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}'
```

## Cost Estimate

| Component | Monthly Cost |
|-----------|-------------|
| CloudFront distribution | ~$5-10 |
| S3 buckets (2 regions) | ~$2-5 |
| S3 Cross-Region Replication | ~$2-5 |
| DynamoDB Global Table | ~$1-3 |
| Lambda@Edge | ~$1-5 |
| Route 53 | ~$1 |
| **Total** | **~$15-30/month** |

## Project Structure

```
├── cdk/                    # AWS CDK implementation (authoritative)
│   ├── bin/                # CDK app entry point
│   ├── lib/                # Stack definitions
│   ├── lambda/             # Lambda function source
│   └── failover.sh         # Failover helper script
├── lambda/                 # Lambda source (reference copy)
├── dr-dashboard.html       # DR operations dashboard
└── research/               # Previous architecture options and docs
```

## Research & Alternative Approaches

The `research/` directory contains documentation and templates from earlier architecture explorations, including dual-CloudFront approaches and Route 53 failover patterns.
