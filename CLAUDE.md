# CLAUDE.md - Project Context for AI Assistants

## Project Purpose

MACP Connect DR infrastructure - disaster recovery for an AWS Connect contact center. Enables failover (~30-60 seconds) between us-east-1 (primary) and us-west-2 (DR) regions.

## Architecture

**Single CloudFront distribution** with Lambda@Edge reading a DynamoDB Global Table to route requests to the active region's S3 bucket. Manual failover only.

```
User Request → CloudFront → Lambda@Edge → checks DynamoDB → routes to active S3 bucket
                              ↓
                    DynamoDB Global Table
                    (replicated us-east-1 ↔ us-west-2)
```

**Failover mechanism:**
- **Manual**: Operator updates DynamoDB `active_region` + invalidates CloudFront cache
- CloudFront caching is 24 hours, so cache invalidation is **required** for failover
- CloudFront requires an origin defined, but Lambda@Edge overrides it dynamically

## Key Files

| File | Purpose |
|------|---------|
| `cdk/lib/macp-dr-stack.ts` | Main CDK stack (authoritative) |
| `cdk/lib/dr-bucket-stack.ts` | DR bucket stack (us-west-2) |
| `cdk/lambda/origin_router.py` | Lambda@Edge code that reads DynamoDB |
| `cdk/failover.sh` | Failover helper script |
| `dr-dashboard.html` | Operations dashboard for failover control |

## Deployment

```bash
cd cdk && npm install && npm run build
npx cdk deploy MacpDrBucketStack   # us-west-2 first
npx cdk deploy MacpDrStack         # us-east-1
```

## Failover Procedure

**Both steps are required:**

```bash
# Step 1: Update DynamoDB
aws dynamodb put-item --table-name macp-dr-prod-failover-state \
  --item '{"config_key":{"S":"active_region"},"active_region":{"S":"us-west-2"}}'

# Step 2: Invalidate CloudFront cache (REQUIRED - 24hr cache TTL)
aws cloudfront create-invalidation --distribution-id DIST_ID --paths "/*"
```

## Conventions

- Environment: `prod`
- Domain pattern: `{service}.prod.gsa.dos.macp.cloud` (admin, agent, chat)
- AWS Account: 417886991978
- Primary region: us-east-1, DR region: us-west-2
- CDK is authoritative; CloudFormation templates in `research/` are reference only

## Research

The `research/` folder contains earlier architecture explorations (dual-CloudFront, Route 53 failover patterns) preserved for reference.
