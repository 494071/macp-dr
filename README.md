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
| **Manual (DDB update)** | ~15 sec | Operator writes `active_region` to DynamoDB |

Lambda@Edge caches the active region for ~15 seconds, so failover propagates quickly
across all edge locations.

## Failover Commands

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

## Project Structure

```
├── 01-dynamodb-global-table.yaml   # DynamoDB Global Table for failover state
├── 02-s3-buckets.yaml              # S3 buckets with cross-region replication
├── 03-lambda-edge.yaml             # Lambda@Edge routing function
├── 04-cloudfront-distribution.yaml # CloudFront with origin groups
├── lambda/                         # Lambda function source code
├── dr-dashboard.html               # DR operations dashboard
├── cdk/                            # AWS CDK implementation (alternative)
└── research/                       # Previous architecture options and docs
```

## Research & Alternative Approaches

The `research/` directory contains documentation and templates from earlier architecture explorations, including dual-CloudFront approaches and Route 53 failover patterns.
