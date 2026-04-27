# MACP Connect DR Solution: Option 7

## Lambda@Edge with DynamoDB Global Table

### Executive Summary

Option 7 provides **manual, controlled failover** for MACP Connect's static web applications (Admin, Agent, Chat portals) using AWS Lambda@Edge and DynamoDB Global Tables. This solution enables on-demand failover between us-east-1 (Primary) and us-west-2 (DR) regions with sub-minute switching time.

**Key Design Decision**: No automatic failover. All failovers are explicitly triggered by operators via DynamoDB, ensuring full control over when and why traffic switches regions.

---

## Architecture Overview

```
                         ┌─────────────────────────────────────┐
                         │            Route 53                 │
                         │  admin.prod.gsa.dos.macp.cloud     │
                         │  agent.prod.gsa.dos.macp.cloud     │
                         │  chat.prod.gsa.dos.macp.cloud      │
                         └─────────────────┬───────────────────┘
                                           │
                                           ▼
                         ┌─────────────────────────────────────┐
                         │      CloudFront Distribution        │
                         │  ┌─────────────────────────────┐   │
                         │  │  CloudFront Function        │   │
                         │  │  (viewer-request)           │   │
                         │  │  Preserves Host header      │   │
                         │  └──────────────┬──────────────┘   │
                         │                 │                   │
                         │  ┌──────────────▼──────────────┐   │
                         │  │  Lambda@Edge                │   │     ┌──────────────────┐
                         │  │  (origin-request)           │◄──┼────►│ DynamoDB Global  │
                         │  │  • Reads active region      │   │     │ Table            │
                         │  │  • Routes to correct S3     │   │     │ (us-east-1 +     │
                         │  │  • Signs requests (SigV4)   │   │     │  us-west-2)      │
                         │  └──────────────┬──────────────┘   │     └──────────────────┘
                         │                 │                   │
                         └─────────────────┼───────────────────┘
                                           │
                          ┌────────────────┴────────────────┐
                          │                                 │
                          ▼                                 ▼
              ┌───────────────────────┐       ┌───────────────────────┐
              │   S3 us-east-1        │       │   S3 us-west-2        │
              │   (Primary)           │       │   (DR)                │
              │                       │       │                       │
              │   /admin/index.html   │       │   /admin/index.html   │
              │   /agent/index.html   │       │   /agent/index.html   │
              │   /chat/index.html    │       │   /chat/index.html    │
              └───────────────────────┘       └───────────────────────┘
```

---

## Key Components

### 1. CloudFront Distribution
- **Single distribution** serving all three subdomains
- Custom domain aliases: `admin.prod.gsa.dos.macp.cloud`, `agent.prod.gsa.dos.macp.cloud`, `chat.prod.gsa.dos.macp.cloud`
- HTTPS only with TLS 1.2+
- US-only geo-restriction
- WAF integration (Fortinet rules)
- Price Class 100 (US, Canada, Europe)

### 2. CloudFront Function (Viewer Request)
- Copies original `Host` header to `x-original-host`
- Required because CloudFront overwrites Host with origin domain before Lambda@Edge runs
- Extremely fast (~1ms) and cost-effective ($0.10/1M requests)

### 3. Lambda@Edge (Origin Request)
- **Runtime**: Python 3.12, x86_64 architecture
- **Memory**: 128 MB
- **Timeout**: 5 seconds
- **Functions**:
  - Reads `active_region` from DynamoDB (with 15-second cache)
  - Extracts subdomain from `x-original-host` header
  - Rewrites URI: `/` → `/{subdomain}/index.html`
  - Signs request with SigV4 for S3 authentication
  - **Routes to active region's S3 bucket** (no automatic failover)

### 4. DynamoDB Global Table
- **Table Name**: `macp-dr-prod-failover-state`
- **Replicated**: us-east-1 ↔ us-west-2
- **Schema**:
  ```json
  {
    "config_key": "active_region",
    "active_region": "us-east-1",
    "updated_at": "2026-04-20T19:00:00Z",
    "updated_by": "operator-name"
  }
  ```
- Point-in-time recovery enabled
- On-demand billing

### 5. S3 Buckets
- **Primary**: `macp-dr-opt7-content-prod-us-east-1`
- **DR**: `macp-dr-opt7-content-prod-us-west-2`
- Both buckets:
  - Versioning enabled
  - Server-side encryption (AES-256)
  - Block all public access
  - Server access logging enabled

### 6. Route 53
- A (Alias) records pointing to CloudFront distribution
- Records managed via CDK

---

## Failover Process

### Triggering Failover

Use the provided failover script:

```bash
# Failover to DR (us-west-2)
./failover.sh west

# Failback to Primary (us-east-1)
./failover.sh east

# Check current status
./failover.sh status
```

Or manually:

```bash
# Failover to us-west-2
aws dynamodb put-item --table-name macp-dr-prod-failover-state \
  --item '{"config_key":{"S":"active_region"},"active_region":{"S":"us-west-2"}}'

# Invalidate cache (required for immediate effect)
aws cloudfront create-invalidation --distribution-id E1KLVY7Q1RG0RK --paths "/*"
```

### Failover Timeline

| Step | Time | Description |
|------|------|-------------|
| 1 | 0s | DynamoDB updated |
| 2 | 0-15s | Lambda cache expires, new requests route to DR |
| 3 | 30-60s | CloudFront cache invalidation propagates globally |
| 4 | ~60s | **Full failover complete** |

---

## Cost Estimate

### Per 1 Million Requests

| Component | Cost | Percentage |
|-----------|------|------------|
| Lambda@Edge | $1.80 | 55% |
| CloudFront Requests | $0.75 | 23% |
| S3 GET Requests | $0.40 | 12% |
| CloudFront Function | $0.10 | 3% |
| DynamoDB Reads | $0.02 | <1% |
| **Total** | **~$3.07** | 100% |

*Note: Data transfer costs vary based on content size.*

### Monthly Fixed Costs

| Resource | Cost |
|----------|------|
| DynamoDB (on-demand, minimal) | ~$1-5 |
| S3 Storage (per GB) | $0.023 |
| CloudFront (no minimum) | $0 |

---

## Security Features

- ✅ **HTTPS Only** - All traffic encrypted in transit
- ✅ **S3 Private** - Buckets not publicly accessible
- ✅ **SigV4 Signing** - Lambda signs all S3 requests
- ✅ **WAF Protection** - Fortinet rules applied
- ✅ **Geo-Restriction** - US-only access
- ✅ **IAM Least Privilege** - Lambda has minimal required permissions
- ✅ **Encryption at Rest** - S3 buckets use AES-256

---

## Monitoring & Observability

### CloudWatch Metrics
- CloudFront: Requests, Bytes Downloaded, Error Rate
- Lambda@Edge: Invocations, Duration, Errors
- DynamoDB: Read Capacity, Latency

### Logs
- CloudFront access logs → S3
- Lambda@Edge logs → CloudWatch (in edge region)
- S3 server access logs → S3

### Recommended Alarms
- Lambda@Edge error rate > 1%
- CloudFront 5xx error rate > 0.5%
- DynamoDB throttling events

---

## Deployment

### Prerequisites
- AWS CLI configured
- Node.js 18+
- CDK CLI (`npm install -g aws-cdk`)

### Deploy Steps

```bash
cd cdk/option-7

# Install dependencies
npm install

# Bootstrap CDK (first time only)
npx cdk bootstrap aws://ACCOUNT_ID/us-east-1
npx cdk bootstrap aws://ACCOUNT_ID/us-west-2

# Deploy DR bucket (us-west-2)
npx cdk deploy Option7DrBucketStack

# Deploy main stack (us-east-1)
npx cdk deploy Option7Stack

# Seed DynamoDB
aws dynamodb put-item --table-name macp-dr-prod-failover-state \
  --item '{"config_key":{"S":"active_region"},"active_region":{"S":"us-east-1"}}'

# Upload content to both buckets
aws s3 sync ./content/ s3://macp-dr-opt7-content-prod-us-east-1/
aws s3 sync ./content/ s3://macp-dr-opt7-content-prod-us-west-2/
```

---

## Advantages of Option 7

| Feature | Benefit |
|---------|---------|
| **Manual Control Only** | No automatic failover - operators decide when to switch |
| **Single Distribution** | No DNS propagation delays |
| **Sub-minute Failover** | ~60 seconds for full transition |
| **Cost Effective** | ~$3/1M requests |
| **No Code Changes** | Same URLs, transparent to users |
| **Audit Trail** | DynamoDB records who/when for failovers |

---

## Limitations

- Lambda@Edge adds latency (~50ms per request)
- Requires cache invalidation for immediate failover
- Lambda@Edge doesn't support ARM64 (must use x86_64)
- Content must be synced to both S3 buckets
- **No automatic failover** - if primary region fails, manual intervention required

---

## Files & Resources

| File | Description |
|------|-------------|
| `cdk/option-7/` | CDK infrastructure code |
| `cdk/option-7/lambda/origin_router.py` | Lambda@Edge function |
| `cdk/option-7/failover.sh` | Failover script |
| `option-7/dr-dashboard.html` | Local monitoring dashboard |

### AWS Resources

| Resource | Identifier |
|----------|------------|
| CloudFront Distribution | E1KLVY7Q1RG0RK |
| Primary S3 Bucket | macp-dr-opt7-content-prod-us-east-1 |
| DR S3 Bucket | macp-dr-opt7-content-prod-us-west-2 |
| DynamoDB Table | macp-dr-prod-failover-state |
| Lambda Function | macp-dr-prod-origin-router |

---

## Support & Contacts

For failover procedures, refer to the DR Runbook or contact the platform team.
