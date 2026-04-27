# CLAUDE.md - Project Context for AI Assistants

## Project Purpose

MACP Connect DR infrastructure - disaster recovery for an AWS Connect contact center. Enables failover (~30-60 seconds) between us-east-1 (primary) and us-west-2 (DR) regions.

## Architecture

**Single CloudFront distribution** with Lambda@Edge reading a DynamoDB Global Table to route requests to the active region's S3 bucket. Manual failover via Portal UI or API.

```
User Request → CloudFront → Lambda@Edge → checks DynamoDB → routes to active S3/redirect
                              ↓
                    DynamoDB Global Table
                    (replicated us-east-1 ↔ us-west-2)
```

**Subdomains:**
- `portal.prod...` - DR dashboard with failover toggle
- `health.prod...` - JSON status (active_region, metadata)
- `admin.prod...` - 302 redirect to Connect Admin (60s cache)
- `agent.prod...` - Dynamic redirect via health endpoint
- `chat.prod...` - Static chat widget assets
- `failover-api.prod...` - API Gateway for failover/invalidation

## Key Files

| File | Purpose |
|------|---------|
| `cdk/lib/macp-dr-stack.ts` | Main CDK stack (CloudFront, Lambda@Edge, DDB) |
| `cdk/lib/dr-bucket-stack.ts` | DR bucket stack (us-west-2) |
| `cdk/lib/failover-api-stack.ts` | Failover API (API Gateway + Lambda) |
| `cdk/lambda/origin_router.py` | Lambda@Edge - routes all traffic |
| `cdk/lambda/failover-api/index.py` | Failover/invalidation Lambda |
| `content/*/portal/index.html` | Portal dashboard |
| `content/*/agent/index.html` | Dynamic agent redirect |
| `sync-content.sh` | Sync content to both S3 buckets |

## Deployment

```bash
cd cdk && npm install && npm run build

# Deploy stacks (in order)
npx cdk deploy Option7DrBucketStack  # us-west-2 bucket
npx cdk deploy Option7Stack          # Main stack
npx cdk deploy FailoverApiStack      # Failover API

# Sync static content
cd .. && ./sync-content.sh
```

## Failover

**Recommended: Use Portal UI** at https://portal.prod.gsa.dos.macp.cloud

**Or via API:**
```bash
curl -X POST https://failover-api.prod.gsa.dos.macp.cloud/failover \
  -H "x-api-key: API_KEY" \
  -d '{"region":"us-west-2","reason":"DR test"}'
```

**Or via CLI (both steps required):**
```bash
# 1. Update DynamoDB
aws dynamodb put-item --table-name macp-dr-prod-failover-state \
  --item '{"config_key":{"S":"active_region"},"active_region":{"S":"us-west-2"}}'

# 2. Invalidate CloudFront (REQUIRED - 24hr cache)
aws cloudfront create-invalidation --distribution-id E1KLVY7Q1RG0RK --paths "/*"
```

## Key Resources

- CloudFront: `E1KLVY7Q1RG0RK`
- DynamoDB: `macp-dr-prod-failover-state`
- S3 East: `macp-dr-opt7-content-prod-us-east-1`
- S3 West: `macp-dr-opt7-content-prod-us-west-2`
- Account: `417886991978`

## Conventions

- Environment: `prod`
- Domain pattern: `{service}.prod.gsa.dos.macp.cloud`
- Primary region: us-east-1, DR region: us-west-2
- CDK is authoritative; CloudFormation templates in `research/` are reference only
- Stack IDs use `Option7*` to match deployed stacks (historical naming)

## Connect URLs

- East Admin: `https://macp-dos-prod-connect-1.my.connect.aws`
- West Admin: `https://macp-dos-prod-dr-connect-1.my.connect.aws`
- Agent path: `/agent-app-v2/`
