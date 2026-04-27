# CLAUDE.md - Project Context for AI Assistants

## Project Purpose

MACP Connect DR infrastructure - disaster recovery for an AWS Connect contact center. Enables rapid failover (~15 seconds) between us-east-1 (primary) and us-west-2 (DR) regions.

## Architecture (Option 7)

**Single CloudFront distribution** with Lambda@Edge reading a DynamoDB Global Table to route requests to the active region's S3 bucket. Manual failover only.

```
User Request → CloudFront → Lambda@Edge → checks DynamoDB → routes to active S3 bucket
                              ↓
                    DynamoDB Global Table
                    (replicated us-east-1 ↔ us-west-2)
```

**Failover mechanism:**
- **Manual**: Operator updates DynamoDB `active_region` value → Lambda@Edge routes to new region (~15 sec RTO)
- CloudFront requires an origin defined, but Lambda@Edge overrides it dynamically

## Key Files

| File | Purpose |
|------|---------|
| `01-dynamodb-global-table.yaml` | Failover state table (Global Table) |
| `02-s3-buckets.yaml` | S3 buckets with cross-region replication |
| `03-lambda-edge.yaml` | Lambda@Edge origin router |
| `04-cloudfront-distribution.yaml` | CloudFront with origin groups |
| `lambda/origin_router.py` | Lambda@Edge code that reads DynamoDB |
| `dr-dashboard.html` | Operations dashboard for failover control |
| `cdk/` | AWS CDK implementation (TypeScript) |

## Deployment

Deploy CloudFormation stacks in order (1→4), all in us-east-1 except S3 DR bucket in us-west-2.

## Failover Commands

```bash
# Check current state
aws dynamodb get-item --region us-east-1 \
  --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}'

# Failover to DR
aws dynamodb update-item --region us-west-2 \
  --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}' \
  --update-expression "SET active_region = :r" \
  --expression-attribute-values '{":r":{"S":"us-west-2"}}'
```

## Conventions

- Environment: `prod`
- Domain pattern: `{service}.prod.gsa.dos.macp.cloud` (admin, agent, chat)
- AWS Account: 417886991978
- Primary region: us-east-1, DR region: us-west-2

## Research

The `research/` folder contains earlier architecture explorations (dual-CloudFront, Route 53 failover patterns) preserved for reference.
