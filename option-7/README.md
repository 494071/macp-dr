# Option 7: Lambda@Edge with DynamoDB Global Table Control Signal

## Overview

This directory contains CloudFormation templates for implementing Option 7 DR architecture:
- **Single CloudFront distribution** with multiple subdomain aliases
- **Origin Group** for automatic failover (safety net)
- **Lambda@Edge** for proactive routing based on DynamoDB control signal
- **DynamoDB Global Table** replicated to us-east-1 and us-west-2

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
           │  Origin Group         │    (us-east-1 + us-west-2)
           └───────────┬───────────┘
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
    S3 us-east-1              S3 us-west-2
    (Primary)                 (DR - via CRR)
```

## Deployment Order

Deploy stacks in this order:

| Order | Template | Region | Description |
|-------|----------|--------|-------------|
| 1 | `01-dynamodb-global-table.yaml` | us-east-1 | Creates Global Table with us-west-2 replica |
| 2 | `02-s3-buckets.yaml` | us-east-1 | Primary bucket + CRR to us-west-2 |
| 3 | `02-s3-buckets.yaml` | us-west-2 | DR bucket (CRR destination) |
| 4 | `03-lambda-edge.yaml` | us-east-1 | Lambda@Edge function (must be us-east-1) |
| 5 | `04-cloudfront-distribution.yaml` | us-east-1 | CloudFront with origin group + Lambda |

## Subdomains

| Subdomain | Path in S3 | Purpose |
|-----------|------------|---------|
| `admin.prod.gsa.dos.macp.cloud` | `/admin/` | Connect Admin redirect portal |
| `agent.prod.gsa.dos.macp.cloud` | `/agent/` | CCP / Workspace |
| `chat.prod.gsa.dos.macp.cloud` | `/chat/` | Chat widget assets |

## Failover Methods

| Method | RTO | Trigger |
|--------|-----|---------|
| **Proactive (DDB flip)** | ~15 sec | Operator writes to DynamoDB |
| **Automatic (Origin Group)** | ~seconds | Primary origin returns 5xx/403 |

## Proactive Failover Command

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

# Revert to Primary (us-east-1)
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
