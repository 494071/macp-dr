# Implementation Plan: Option 7 - Lambda@Edge with DynamoDB Global Table Control Signal

## Problem Statement

Option 6 (CloudFront Origin Groups) provides automatic failover when the primary origin returns errors. However, it is **reactive** — failover only happens after requests fail. Option 7 adds **proactive** failover capability: operators can flip a DynamoDB flag to instantly route all traffic to DR before any user experiences errors.

## Proposed Approach

Build on the existing CloudFront + S3 infrastructure (Option 6) by adding:
1. DynamoDB Global Table as the control signal store
2. Lambda@Edge function on `origin-request` that reads the control signal and routes traffic accordingly
3. Operational tooling (failover script updates, monitoring)

The Origin Group remains as a safety net underneath Lambda@Edge.

## DNS Architecture: Subdomains

**Single CloudFront distribution with multiple subdomain aliases:**

| Subdomain | Path in S3 | Purpose |
|-----------|------------|---------|
| `admin.prod.gsa.dos.macp.cloud` | `/admin/` | Connect Admin redirect portal |
| `agent.prod.gsa.dos.macp.cloud` | `/agent/` | CCP / Workspace |
| `chat.prod.gsa.dos.macp.cloud` | `/chat/` | Chat widget assets |
| `chat-api.prod.gsa.dos.macp.cloud` | `/chat-api/` | Chat API (future) |

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
           │  Lambda@Edge ─────────┼──▶ DynamoDB
           │  Origin Group         │
           └───────────┬───────────┘
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
    S3 us-east-1              S3 us-west-2
```

All subdomains resolve to the **same CloudFront distribution**. Lambda@Edge routes to the active region's S3 bucket.

## Prerequisites (Verify Before Starting)

- [ ] Option 6 infrastructure deployed (CloudFront with Origin Group, CRR-enabled S3 buckets)
- [ ] ACM certificate available in us-east-1
- [ ] AWS CLI configured with appropriate permissions
- [ ] Python 3.12 available for Lambda@Edge development

---

## Implementation Phases

### Phase 1: DynamoDB Global Table

**Task:** Create DynamoDB Global Table replicated to us-east-1 and us-west-2

| Property | Value |
|----------|-------|
| Table name | `macp-dr-prod-failover-state` |
| Partition key | `config_key` (String) |
| Billing mode | PAY_PER_REQUEST |
| Point-in-time recovery | Enabled |
| Replicas | us-east-1, us-west-2 |

**Initial seed item:**
```json
{
  "config_key": "active_region",
  "active_region": "us-east-1",
  "updated_at": "2026-04-20T00:00:00Z",
  "updated_by": "initial-deploy",
  "reason": "Initial deployment"
}
```

---

### Phase 2: Lambda@Edge Function

#### 2.1 IAM Execution Role

Create IAM role with:
- **Trust policy:** `lambda.amazonaws.com` and `edgelambda.amazonaws.com`
- **Permissions:**
  - `dynamodb:GetItem` on both regional table ARNs
  - CloudWatch Logs (`logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`) in any region

#### 2.2 Function Code

Develop `origin-router` Lambda@Edge function with:

| Feature | Implementation |
|---------|----------------|
| Multi-region DDB read | Try us-east-1 first, fallback to us-west-2 |
| In-memory cache | ~15s TTL to reduce DDB calls |
| Last-known-good fallback | Use cached value if both replicas fail |
| DR-biased default | Default to us-west-2 if no cached value exists |
| Bundle size | ✅ No bundling needed - boto3 included in runtime |

**Implementation:**
```python
import boto3
import time

# Origins configuration
ORIGINS = {
    'us-east-1': {
        'domainName': 'test-macp-dos-prod-us-east-1-cloudfront-content.s3.us-east-1.amazonaws.com',
        'region': 'us-east-1'
    },
    'us-west-2': {
        'domainName': 'test-macp-dos-prod-us-west-2-cloudfront-content.s3.us-west-2.amazonaws.com',
        'region': 'us-west-2'
    }
}

TABLE_NAME = 'macp-dr-prod-failover-state'
CACHE_TTL = 15  # seconds

# Module-level cache (persists across warm invocations)
CACHE = {'region': None, 'expires': 0, 'last_known': None}


def get_active_region():
    """Read active region from DynamoDB with caching and multi-region fallback."""
    now = time.time()
    
    # Return cached value if fresh
    if now < CACHE['expires'] and CACHE['region']:
        return CACHE['region']
    
    # Try each DDB replica
    for ddb_region in ['us-east-1', 'us-west-2']:
        try:
            client = boto3.client('dynamodb', region_name=ddb_region)
            resp = client.get_item(
                TableName=TABLE_NAME,
                Key={'config_key': {'S': 'active_region'}},
                ConsistentRead=False
            )
            region = resp['Item']['active_region']['S']
            CACHE.update(region=region, expires=now + CACHE_TTL, last_known=region)
            return region
        except Exception:
            continue
    
    # Fallback: last-known-good or default to DR
    return CACHE['last_known'] or 'us-west-2'


def handler(event, context):
    """Lambda@Edge origin-request handler."""
    request = event['Records'][0]['cf']['request']
    
    active_region = get_active_region()
    origin_config = ORIGINS[active_region]
    
    # Rewrite origin to active region's S3 bucket
    request['origin'] = {
        's3': {
            'domainName': origin_config['domainName'],
            'region': origin_config['region'],
            'authMethod': 'origin-access-identity',
            'path': ''
        }
    }
    request['headers']['host'] = [{'key': 'host', 'value': origin_config['domainName']}]
    
    return request
```

#### 2.3 Deploy Lambda@Edge

- Deploy from `us-east-1` (Lambda@Edge constraint)
- Runtime: Python 3.12
- No bundling required (boto3 included in runtime)
- Publish numbered version (required for Lambda@Edge — cannot use `$LATEST`)

---

### Phase 3: CloudFront Integration

Associate Lambda@Edge with CloudFront distribution:

```yaml
CacheBehaviors:
  - PathPattern: /*
    LambdaFunctionAssociations:
      - EventType: origin-request
        LambdaFunctionARN: !Ref OriginRouterLambdaVersion
        IncludeBody: false
```

**Note:** Distribution deployment takes ~5-10 minutes to propagate to all edges.

---

### Phase 4: Operational Tooling

#### 4.1 Update failover.sh

Add new commands:

| Command | Action |
|---------|--------|
| `failover ddb us-west-2` | Proactive failover via DDB flag flip |
| `failover status` | Read current DDB state |
| `failover revert` | Revert to us-east-1 |

**Example failover command:**
```bash
aws dynamodb update-item \
  --region us-west-2 \
  --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}' \
  --update-expression "SET active_region = :r, updated_at = :t, updated_by = :u, reason = :reason" \
  --expression-attribute-values '{
    ":r":{"S":"us-west-2"},
    ":t":{"S":"2026-04-20T10:00:00Z"},
    ":u":{"S":"operator"},
    ":reason":{"S":"Planned maintenance"}
  }'
```

#### 4.2 Runbook Documentation

Document:
- Proactive failover steps
- Verification commands
- Revert procedure
- Lambda@Edge log troubleshooting (logs written to ~15 regions)

---

### Phase 5: Testing

| Test | Description | Expected Result |
|------|-------------|-----------------|
| Happy path | DDB returns us-east-1 | Traffic routes to primary |
| Proactive failover | Flip DDB to us-west-2 | Traffic routes to DR within ~15s |
| DDB unavailable | Both replicas unreachable | Uses last-known-good or defaults to DR |
| Origin Group fallback | Lambda's chosen origin returns 5xx | Origin Group auto-fails to secondary |

---

## Files to Create

| File | Purpose |
|------|---------|
| `cloudformation/dynamodb-global-table.yaml` | DynamoDB Global Table + initial seed |
| `cloudformation/lambda-edge.yaml` | Lambda@Edge function + IAM role + CloudFront association |
| `lambda/origin-router/origin_router.py` | Lambda@Edge handler code (single file, no dependencies) |

---

## Cost Impact

| Component | Monthly Cost |
|-----------|-------------|
| DynamoDB Global Table (PAY_PER_REQUEST) | ~$1-3 |
| Lambda@Edge invocations | ~$1-5 |
| **Additional over Option 6** | **~$2-8/month** |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Lambda@Edge cold starts add latency | In-memory caching reduces DDB calls; origin group as fallback |
| Cannot deploy code fixes during us-east-1 outage | Write defensive code upfront; DDB reads can fallback to us-west-2 |
| Multi-region log debugging | Document which regions to search; consider centralized logging |

---

## Failover RTO

| Scenario | RTO |
|----------|-----|
| Proactive (DDB flip) | ~15 seconds (cache TTL) |
| Automatic (Origin Group) | ~seconds (per-request) |

---

## Relationship to Option 6

Option 7 is a **strict superset** of Option 6. The Origin Group automatic failover remains intact as a safety net. This plan adds the proactive DDB-based routing layer on top.

You can deploy Option 6 first and add Option 7 later without re-architecting.

---

## Deployment Order

1. Deploy DynamoDB Global Table (Phase 1)
2. Create Lambda IAM role (Phase 2.1)
3. Build and deploy Lambda@Edge (Phase 2.2, 2.3)
4. Associate Lambda with CloudFront (Phase 3)
5. Update operational scripts (Phase 4)
6. Run test suite (Phase 5)

---

*Document created: 2026-04-20*
