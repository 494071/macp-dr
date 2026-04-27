# MACP Connect DR - Architecture Options Analysis

## Overview

This document analyzes different approaches for implementing disaster recovery (DR) infrastructure for MACP Connect, focusing on the trade-off between failover speed and operational complexity.

**Requirements:**
- Serve static content (admin portal, agent CCP, chat widget) from S3
- Support future API endpoints (Lambda/API Gateway)
- US-only geo-restriction
- WAF protection
- Manual failover with minimal downtime
- Infrastructure in both us-east-1 (primary) and us-west-2 (DR)

---

## The Core Challenge: CloudFront Custom Domain Aliases

**AWS CloudFront has a global uniqueness constraint on custom domain aliases (CNAMEs).**

A domain like `portal.prod.gsa.dos.macp.cloud` can only be configured on **ONE** CloudFront distribution globally at any time. This means:

- ❌ Cannot have the same domain on both us-east-1 and us-west-2 distributions simultaneously
- ✅ Must swap aliases between distributions during failover
- ⏱️ CloudFront config changes take ~10-20 minutes to propagate

This constraint drives the architecture decisions below.

---

## Option 1: CloudFront with Manual Alias Swap

### Architecture

```
                         Route 53
                    (portal.prod.gsa.dos.macp.cloud)
                              │
                              ▼
              ┌───────────────────────────────┐
              │  CloudFront (Active Region)   │
              │  - Custom aliases configured  │
              │  - Edge caching               │
              │  - WAF at edge                │
              │  - Geo-restriction built-in   │
              └───────────────┬───────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │   S3 Bucket     │
                    │   (Regional)    │
                    └─────────────────┘

              ┌───────────────────────────────┐
              │  CloudFront (Standby Region)  │
              │  - NO custom aliases          │
              │  - Ready to activate          │
              │  - Same configuration         │
              └───────────────┬───────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │   S3 Bucket     │
                    │   (Regional)    │
                    └─────────────────┘
```

### Failover Process

1. **Remove aliases from primary CloudFront** (~10-15 min)
   ```bash
   aws cloudformation update-stack --stack-name macp-dr-infrastructure-east \
     --template-body file://admin-portal-infrastructure.yaml \
     --parameters ParameterKey=Region,ParameterValue=us-east-1 \
                  ParameterKey=EnableAliases,ParameterValue=false
   ```

2. **Wait for propagation** (~10-15 min)
   ```bash
   aws cloudformation wait stack-update-complete --stack-name macp-dr-infrastructure-east
   ```

3. **Add aliases to DR CloudFront** (~10-15 min)
   ```bash
   aws cloudformation update-stack --stack-name macp-dr-infrastructure-west \
     --template-body file://admin-portal-infrastructure.yaml \
     --parameters ParameterKey=Region,ParameterValue=us-west-2 \
                  ParameterKey=EnableAliases,ParameterValue=true
   ```

4. **Update Route 53** (optional, if not using CloudFront DNS)

**Total Failover Time: ~30-45 minutes**

### Pros
- ✅ Full CloudFront benefits (edge caching, ~10-30ms latency)
- ✅ WAF at edge (blocks before reaching origin)
- ✅ Built-in geo-restriction
- ✅ Simple S3 integration (OAC)
- ✅ Lower cost (~$10/month for typical usage)
- ✅ Uses existing WAF WebACL

### Cons
- ❌ Slow failover (~30-45 minutes)
- ❌ Potential service disruption during alias swap
- ❌ Cannot pre-validate DR distribution with custom domain

### When to Use
- Static content is primary use case
- RTO (Recovery Time Objective) of 30-45 minutes is acceptable
- Edge caching benefits outweigh failover speed

---

## Option 2: CloudFront with Path-Based Routing (Single Domain)

### Architecture

Same as Option 1, but uses path-based routing instead of subdomains:

| Current (Subdomains) | Proposed (Paths) |
|---------------------|------------------|
| admin.prod.gsa.dos.macp.cloud | portal.prod.gsa.dos.macp.cloud/admin/ |
| agent.prod.gsa.dos.macp.cloud | portal.prod.gsa.dos.macp.cloud/agent/ |
| chat.prod.gsa.dos.macp.cloud | portal.prod.gsa.dos.macp.cloud/chat/ |

### Failover Process

Same as Option 1 - still requires alias swap.

### Additional Consideration

Path-based routing simplifies from 6 domains to 1, but **does not solve the alias swap problem**.

---

## Option 3: API Gateway for Everything (Skip CloudFront)

### Architecture

```
                         Route 53
                    (portal.prod.gsa.dos.macp.cloud)
                              │
              ┌───────────────┴───────────────┐
              │ Failover or Weighted Routing  │
              └───────────────┬───────────────┘
                              │
         ┌────────────────────┴────────────────────┐
         ▼                                         ▼
┌─────────────────────┐                 ┌─────────────────────┐
│  us-east-1          │                 │  us-west-2          │
│  ┌───────────────┐  │                 │  ┌───────────────┐  │
│  │     WAF       │  │                 │  │     WAF       │  │
│  │  (Regional)   │  │                 │  │  (Regional)   │  │
│  └───────┬───────┘  │                 │  └───────┬───────┘  │
│          ▼          │                 │          ▼          │
│  ┌───────────────┐  │                 │  ┌───────────────┐  │
│  │ API Gateway   │  │                 │  │ API Gateway   │  │
│  │  (Regional)   │  │                 │  │  (Regional)   │  │
│  │ Custom Domain:│  │                 │  │ Custom Domain:│  │
│  │ portal.prod...│  │                 │  │ portal.prod...│  │
│  └───────┬───────┘  │                 │  └───────┬───────┘  │
│          ▼          │                 │          ▼          │
│  ┌───────────────┐  │                 │  ┌───────────────┐  │
│  │    Lambda     │  │                 │  │    Lambda     │  │
│  │  (S3 Proxy)   │  │                 │  │  (S3 Proxy)   │  │
│  └───────┬───────┘  │                 │  └───────┬───────┘  │
│          ▼          │                 │          ▼          │
│  ┌───────────────┐  │                 │  ┌───────────────┐  │
│  │      S3       │  │                 │  │      S3       │  │
│  └───────────────┘  │                 │  └───────────────┘  │
└─────────────────────┘                 └─────────────────────┘
```

### Key Advantage

**API Gateway allows the same custom domain in multiple regions simultaneously!**

Each regional API Gateway can have `portal.prod.gsa.dos.macp.cloud` configured. Route 53 determines which region receives traffic based on failover or weighted routing.

### Failover Process

```bash
# Instant failover via Route 53 (~60 seconds)
aws route53 change-resource-record-sets \
  --hosted-zone-id Z10445293PTGB9ZOBN0G8 \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "portal.prod.gsa.dos.macp.cloud",
        "Type": "A",
        "AliasTarget": {
          "HostedZoneId": "<dr-api-gateway-hosted-zone>",
          "DNSName": "<dr-api-gateway-domain>",
          "EvaluateTargetHealth": true
        }
      }
    }]
  }'
```

**Total Failover Time: ~60 seconds**

### S3 Integration Options

#### Direct S3 Integration (REST API only)
```yaml
AdminProxyMethod:
  Type: AWS::ApiGateway::Method
  Properties:
    Integration:
      Type: AWS
      IntegrationHttpMethod: GET
      Uri: !Sub 'arn:aws:apigateway:${AWS::Region}:s3:path/${Bucket}/admin/{proxy}'
      Credentials: !GetAtt ApiGatewayS3Role.Arn
```

**Complexity:** Each path pattern needs explicit configuration, status code handling, CORS headers.

#### Lambda Proxy (Recommended)
```python
def lambda_handler(event, context):
    path = event['pathParameters'].get('proxy', 'index.html')
    s3_key = f"admin/{path}"
    
    response = s3.get_object(Bucket=BUCKET, Key=s3_key)
    return {
        'statusCode': 200,
        'headers': {'Content-Type': response['ContentType']},
        'body': response['Body'].read().decode('utf-8')
    }
```

**Complexity:** More flexible but adds Lambda cold start latency and costs.

### Geo-Restriction Implementation

Must use regional WAF (CloudFront's built-in geo-restriction not available):

```yaml
GeoRestrictWebACL:
  Type: AWS::WAFv2::WebACL
  Properties:
    Scope: REGIONAL  # Not CLOUDFRONT
    DefaultAction:
      Block: {}
    Rules:
      - Name: AllowUSOnly
        Priority: 1
        Action:
          Allow: {}
        Statement:
          GeoMatchStatement:
            CountryCodes:
              - US
```

### Pros
- ✅ **Instant failover** (~60 seconds)
- ✅ Same domain works in both regions simultaneously
- ✅ Route 53 health checks for automatic failover option
- ✅ Native Lambda integration for APIs
- ✅ Consistent architecture for static and dynamic content

### Cons
- ❌ **No edge caching** (every request hits regional origin)
- ❌ Higher latency (~50-150ms vs ~10-30ms for cached content)
- ❌ More complex S3 integration
- ❌ Higher cost (~$3.50/million requests vs ~$1/million)
- ❌ Regional WAF only (not at edge)
- ❌ Must replicate WAF rules to each region
- ❌ Lambda cold starts (~100-500ms)

### When to Use
- RTO must be under 5 minutes
- Content is mostly dynamic (caching less beneficial)
- Already heavily invested in API Gateway/Lambda

---

## Option 4: AWS Global Accelerator

### Overview

AWS Global Accelerator provides static anycast IP addresses and routes traffic to optimal endpoints based on health and geography.

### Limitation for This Use Case

**Global Accelerator does NOT work with CloudFront** (both are edge services).

It works with:
- Application Load Balancer (ALB)
- Network Load Balancer (NLB)
- EC2 instances
- Elastic IPs

### Architecture (If Using)

```
                    Global Accelerator
                   (Static Anycast IPs)
                          │
         ┌────────────────┴────────────────┐
         ▼                                 ▼
   ┌───────────┐                    ┌───────────┐
   │ us-east-1 │                    │ us-west-2 │
   │    ALB    │                    │    ALB    │
   └─────┬─────┘                    └─────┬─────┘
         │                                 │
    ┌────┴────┐                       ┌────┴────┐
    ▼         ▼                       ▼         ▼
┌───────┐ ┌───────┐               ┌───────┐ ┌───────┐
│Lambda │ │  S3   │               │Lambda │ │  S3   │
│ (API) │ │(static)│               │ (API) │ │(static)│
└───────┘ └───────┘               └───────┘ └───────┘
```

### Pros
- ✅ Static IPs (no DNS propagation)
- ✅ ~30 second failover with health checks
- ✅ Automatic failover option

### Cons
- ❌ **Bypasses CloudFront entirely** (no edge caching)
- ❌ Requires ALB in each region (~$20/month each)
- ❌ Additional Global Accelerator cost (~$18/month)
- ❌ More infrastructure to manage
- ❌ Higher overall complexity

### When to Use
- Need sub-minute automatic failover
- Serving primarily dynamic content
- Can justify ALB costs and management

---

## Option 5: CloudFront Functions (Accept Any Host Header)

### Concept

Use CloudFront Functions to modify the Host header at the edge, potentially allowing CloudFront to accept requests without matching aliases.

### Reality

**This does NOT work.**

CloudFront validates the Host header **before** CloudFront Functions execute. If the Host doesn't match a configured alias, the request is rejected with 403.

```
Request → CloudFront Alias Check → CloudFront Functions → Origin
                    ▲
                    │
              403 if no match
```

### Conclusion

Not a viable option.

---

## Option 6: Single CloudFront with S3 Origin Group (Automatic Origin Failover)

### Architecture

```
                         Route 53
                    (portal.prod.gsa.dos.macp.cloud)
                              │
                              ▼
              ┌───────────────────────────────┐
              │  CloudFront (Global, Single)  │
              │  - Custom alias configured    │
              │  - WAF at edge (global)       │
              │  - Geo-restriction built-in   │
              │  - Origin Group w/ failover   │
              └───────────────┬───────────────┘
                              │
                  ┌───────────┴───────────┐
                  ▼                       ▼
           ┌─────────────┐         ┌─────────────┐
           │ S3 Primary  │  CRR ─▶ │   S3 DR     │
           │ us-east-1   │         │  us-west-2  │
           └─────────────┘         └─────────────┘
              (Primary)              (Secondary)

        On 5xx/403 from primary → CloudFront automatically
        serves subsequent requests from DR bucket.
```

### Key Insight

CloudFront is a **global** service — a regional outage in `us-east-1` does not affect CloudFront itself, only the origins behind it. Origin Groups are purpose-built for this: CloudFront automatically retries failed origin requests against a secondary origin, per-request, in seconds.

Because there is only one CloudFront distribution, the global alias-uniqueness constraint (see Option 1) never comes into play.

### Failover Process

**Automatic (regional S3 outage):**

No operator action. Origin group fails over on configured status codes (`500`, `502`, `503`, `504`, `403`). RTO measured in seconds per edge.

**Manual (drill, partial failure, Connect-only outage):**

Two independent flip mechanics, both single-call and reversible:

1. **Force S3 origin failover** — attach a Deny statement to the primary bucket policy:
   ```bash
   aws s3api put-bucket-policy --bucket $PRIMARY_BUCKET \
     --policy file://bucket-policy-with-deny.json
   ```
   CloudFront OAC requests return 403 → origin group fails over to DR bucket.

2. **Force Connect-only failover** — update the runtime config served from the primary bucket:
   ```bash
   aws s3 cp config-dr.json s3://$PRIMARY_BUCKET/config/active-region.json
   aws cloudfront create-invalidation --distribution-id $DIST_ID \
     --paths "/config/active-region.json"
   ```
   UI fetches new config, redirects to DR Connect instance.

**Total Failover Time: ~5-60 seconds** (origin flip: seconds; config flip: invalidation TTL-bound).

### Extending to API Gateway

Add API Gateway origins to the same distribution with a second origin group:

```yaml
OriginGroups:
  Items:
    - Id: S3OriginGroup
      Members: [S3OriginPrimary, S3OriginDR]
      FailoverCriteria:
        StatusCodes: [403, 500, 502, 503, 504]
    - Id: APIOriginGroup
      Members: [APIOriginPrimary, APIOriginDR]
      FailoverCriteria:
        StatusCodes: [403, 500, 502, 503, 504]

CacheBehaviors:
  - PathPattern: /api/*
    TargetOriginId: APIOriginGroup
```

Manual API Gateway failover is less elegant than S3 — API Gateway has no bucket-policy equivalent. The cleanest equivalent is attaching a deny-all **regional WAF ACL** to the primary API Gateway stage:

```bash
aws wafv2 associate-web-acl \
  --web-acl-arn $DENY_ALL_REGIONAL_WEBACL_ARN \
  --resource-arn $PRIMARY_API_STAGE_ARN
```

Returns 403 → API origin group fails over. One call, reversible via `disassociate-web-acl`.

### Runtime Region Awareness via config.json

Each bucket serves a different `/config/active-region.json`:

```json
// us-east-1 bucket
{ "connectInstance": "macp-dos-prod-connect-1.my.connect.aws", "region": "us-east-1" }

// us-west-2 bucket
{ "connectInstance": "macp-dos-prod-connect-2.my.connect.aws", "region": "us-west-2" }
```

When origin group fails over to the DR bucket, the UI naturally receives DR Connect config — no separate coordination needed.

### Required Infrastructure Changes

1. **Split templates:**
   - `buckets.yaml` — deployed per-region (both primary and DR buckets + CRR)
   - `distribution.yaml` — deployed once in `us-east-1`, references both bucket ARNs as origins
2. **S3 Cross-Region Replication (CRR)** on primary bucket → DR bucket
3. **Bucket policy on DR bucket** must trust the same CloudFront distribution ARN (OAC)
4. **Origin group block** in CloudFront distribution replacing single `TargetOriginId`
5. **Route 53** simplifies to a single alias → CloudFront (no `PRIMARY`/`SECONDARY` records)
6. **`ErrorCachingMinTTL: 0`** on failover status codes (prevents 10s cached 5xx delaying flip)
7. **Deny-all regional WebACLs** pre-provisioned in each region if API Gateway is in scope

### Pros

- ✅ **Near-instant automatic failover** (~seconds, per-request)
- ✅ Single CloudFront distribution — no alias-uniqueness problem
- ✅ CloudFront remains global; unaffected by regional outages
- ✅ Edge caching preserved (unlike Options 3/4)
- ✅ Edge WAF + built-in geo-restriction preserved
- ✅ Single domain, single cert, single WAF config to manage
- ✅ Manual failover is a single AWS CLI call (bucket policy / WAF association)
- ✅ Idempotent and reversible flips (good for drills)
- ✅ Covers both full-region and partial (Connect-only) failures via distinct flip mechanics

### Cons

- ❌ Requires S3 Cross-Region Replication (async; RPO = replication lag, seconds to minutes)
- ❌ Origin group only triggers on HTTP errors — a regional issue where S3 returns 200s with stale/bad content is not detected
- ❌ CloudFront caches error responses by default (mitigated by `ErrorCachingMinTTL: 0`)
- ❌ Non-idempotent API writes may retry on DR during failover (requires idempotency keys)
- ❌ DR bucket is hot standby, not truly idle (minor cost)
- ❌ Manual API Gateway flip requires pre-provisioned deny-all WebACL (not as clean as S3)
- ❌ Operator must maintain exact "normal" bucket policy JSON for clean revert

### When to Use

- Full-region failover is the design goal
- RTO target is seconds to low minutes
- Edge caching and edge WAF are valued
- Operational simplicity of a single distribution is preferred
- Static + API content share the same failover posture

---

## Option 7: Option 6 + Lambda@Edge with DynamoDB Global Table Control Signal

### Architecture

Builds on Option 6. Adds Lambda@Edge on the `origin-request` event to make **proactive** routing decisions driven by a shared control-signal stored in a DynamoDB Global Table.

```
                          Route 53
                     (portal.prod.gsa.dos.macp.cloud)
                               │
                               ▼
               ┌────────────────────────────────┐
               │  CloudFront (Global, Single)   │
               │                                │
               │  origin-request Lambda@Edge ───┼──── reads ───┐
               │         │                      │              │
               │         ▼                      │              │
               │  Rewrites request.origin       │              ▼
               │  to active region              │   ┌──────────────────────┐
               │                                │   │ DynamoDB Global      │
               │  Origin Group (safety net)     │   │ Table                │
               │  ├─ Primary  (selected)        │   │ ┌──────────────────┐ │
               │  └─ Secondary (failover)       │   │ │ config_key:      │ │
               └────────────┬───────────────────┘   │ │   active_region  │ │
                            │                       │ │ value: us-east-1 │ │
                            ▼                       │ └──────────────────┘ │
                  ┌───────────────────┐             │  Replicas:           │
                  │  S3 (chosen region)│             │  - us-east-1        │
                  └───────────────────┘             │  - us-west-2        │
                                                    └──────────────────────┘
```

### Key Concept

Option 6 relies on **reactive** failover — CloudFront only knows to fail over *after* the primary origin has already returned a 5xx/403. Option 7 adds a **proactive** layer: operators flip a single DynamoDB item and all CloudFront edges worldwide start routing to the new region within ~15 seconds (cache TTL), before any request has to fail.

Origin Group is kept as the safety net underneath. The two layers compose:

- Lambda@Edge decides the **preferred** origin based on current failover state
- Origin Group catches the case where the preferred origin is itself unavailable

### DynamoDB Global Table Schema

Single table, minimal schema, PAY_PER_REQUEST billing (reads/writes are rare):

| Attribute | Type | Notes |
|---|---|---|
| `config_key` (PK) | String | `active_region`, future keys for API/Connect overrides |
| `active_region` | String | `us-east-1` or `us-west-2` |
| `updated_at` | String | ISO 8601 timestamp |
| `updated_by` | String | Operator or automation identifier |
| `reason` | String | Free text — audit trail |

**Replicas:** `us-east-1` and `us-west-2`. Global Tables replicate writes bidirectionally in seconds.

**Point-in-Time Recovery:** enabled — gives full history of failover state changes, essential for post-incident review.

### Lambda@Edge Function Design

Runs on every CloudFront `origin-request` event (cache misses only). Core logic:

```javascript
// Pseudocode — see lambda/origin-router/ for full implementation

let cached = { region: null, expires: 0, lastKnown: null };

async function getActiveRegion() {
  // 1. Short-circuit if in-memory cache is fresh
  if (Date.now() < cached.expires) return cached.region;

  // 2. Try primary region's DDB replica
  try {
    const region = await readDdb('us-east-1');
    cached = { region, expires: Date.now() + 15_000, lastKnown: region };
    return region;
  } catch {}

  // 3. Fall back to DR region's DDB replica
  try {
    const region = await readDdb('us-west-2');
    cached = { region, expires: Date.now() + 15_000, lastKnown: region };
    return region;
  } catch {}

  // 4. Last-known-good from prior successful read
  if (cached.lastKnown) return cached.lastKnown;

  // 5. Conservative default — assume primary is dead if nothing else known
  return 'us-west-2';
}

exports.handler = async (event) => {
  const request = event.Records[0].cf.request;
  const region = await getActiveRegion();
  request.origin = ORIGINS[region];
  request.headers['host'] = [{ key: 'host', value: ORIGINS[region].s3.domainName }];
  return request;
};
```

**Critical design properties:**

1. **Multi-region read with fallback.** `us-east-1` DDB failure does not incapacitate the function — falls through to `us-west-2` replica.
2. **In-memory cache (~15s TTL).** Prevents per-request DDB calls; amortizes latency/cost.
3. **Last-known-good fallback.** If both DDB replicas are unreachable, serve from the last successful read.
4. **DR-biased default.** If no cached value exists either (cold Lambda + both regions down), default to DR rather than primary. Safer assumption during catastrophic failure.

### Failover Process

**Operator-initiated proactive failover (Option 7's primary value):**

```bash
# Single write — replicates to both regions via Global Table
aws dynamodb update-item \
  --region us-west-2 \
  --table-name macp-dr-prod-failover-state \
  --key '{"config_key":{"S":"active_region"}}' \
  --update-expression "SET active_region = :r, updated_at = :t, updated_by = :u, reason = :reason" \
  --expression-attribute-values '{
    ":r":{"S":"us-west-2"},
    ":t":{"S":"2026-04-18T10:00:00Z"},
    ":u":{"S":"failover.sh"},
    ":reason":{"S":"Primary region outage"}
  }'
```

Writing to the `us-west-2` replica works even when `us-east-1` is fully offline — this is the critical property.

**RTO:** ~15 seconds (cache TTL) on warm edges; ~cold-start latency + 15s on cold edges.

**Automatic failover** (origin group safety net) remains unchanged from Option 6: fires when the Lambda's chosen origin returns 5xx/403.

**Revert:** same command, value `us-east-1`.

### Additional Use Cases Unlocked

Because the control signal is operator-settable via a single DDB write, Option 7 enables patterns that pure origin groups cannot:

| Pattern | Mechanism |
|---|---|
| **Canary rollback** | Schema extension: add `canary_percent` attribute, Lambda routes by request hash |
| **Session pinning** | Lambda reads region from request cookie, writes sticky cookie on first request |
| **Geographic routing** | Lambda inspects `CloudFront-Viewer-Country` header, routes per-region |
| **Planned maintenance** | Shift traffic to DR, perform primary-region work, shift back |
| **Drill mode** | `?failover=dr` query param forces DR routing for specific testers only |
| **API-only failover** | Add `active_api_region` key; static content stays primary, API routes to DR |

### Required Infrastructure

Building on Option 6:

1. **DynamoDB Global Table** — replicated to `us-east-1` and `us-west-2`, PITR enabled
2. **Lambda@Edge function** — Node.js 20.x, deployed from `us-east-1` (Lambda@Edge constraint)
3. **IAM execution role** — assumable by both `lambda.amazonaws.com` and `edgelambda.amazonaws.com`; `dynamodb:GetItem` on both regional replicas
4. **Published Lambda version** — Lambda@Edge requires numbered version, not `$LATEST`
5. **CloudFront association** — `LambdaFunctionAssociations` on cache behaviors with `EventType: origin-request` and `IncludeBody: false`
6. **Initial DDB seed** — item with `config_key=active_region`, `active_region=us-east-1`
7. **Bundle size discipline** — Lambda@Edge origin-request functions are capped at 1 MB uncompressed; use esbuild/webpack tree-shaking on `@aws-sdk/client-dynamodb` (fits under 1 MB with only `GetItem` included)

### Pros

- ✅ **Proactive failover** — flip a flag, all edges route to DR within ~15s
- ✅ **No need to force 5xx** to trigger failover (cleaner ops than bucket policy flip for planned events)
- ✅ **Works during `us-east-1` control-plane outage** — writes to `us-west-2` DDB replica
- ✅ **Origin Group remains safety net** — two independent failover mechanisms compose
- ✅ **Unlocks canary, geo, session pinning, partial failover** — not achievable with origin groups alone
- ✅ **Audit trail via DDB PITR + CloudTrail** — every flip is logged with actor and reason
- ✅ **Single source of truth** — one DDB item drives routing at all edges globally

### Cons

- ❌ **Lambda@Edge cold starts** (~100-500ms on first hit per edge location)
- ❌ **Deploy latency** — new Lambda versions take ~5-10 min to propagate to all edges
- ❌ **Bundle size constraint** (1 MB uncompressed for origin-request) — requires build tooling
- ❌ **Debugging is harder** — CloudWatch logs written to the region nearest the edge that served the request; typically ~15 regions to search
- ❌ **Cost per origin request** — ~$0.60/M invocations + GB-seconds; negligible for cached content, meaningful for uncached API paths
- ❌ **More moving parts** — DDB table, Lambda function, IAM role, CloudFront association, Lambda version lifecycle
- ❌ **Deployment region lock-in** — Lambda@Edge can only be created/updated from `us-east-1`, making code rollouts brittle during `us-east-1` control-plane issues (though existing versions keep running)
- ❌ **No environment variables** — config values must be hardcoded or read from external sources

### The `us-east-1` Control Plane Caveat

Lambda@Edge has an asymmetric relationship with `us-east-1`:

| Phase | Affected by `us-east-1` outage? |
|---|---|
| **Execution** at edges | ❌ No — already-deployed code runs globally, independent of `us-east-1` |
| **Deployment / version publishing** | ✅ Yes — cannot create or update functions |
| **CloudFront association changes** | ✅ Yes — CloudFront control plane is in `us-east-1` |

Existing deployed functions continue executing during a `us-east-1` outage. This is why multi-region DDB reads in the function matter: the *code* keeps running, so it needs to be able to reach a control signal that isn't dependent on `us-east-1`. DynamoDB Global Tables deliver exactly this — each regional replica is autonomous.

What you cannot do during a `us-east-1` outage is **push a code fix** to the Lambda. This is why the function must be written defensively upfront: you can't hot-patch during the incident.

### Failover Mechanism Matrix (Option 7 Stack)

With Option 7 fully deployed, you have **four** orthogonal failover mechanisms:

| Mechanism | RTO | Trigger | Best for |
|---|---|---|---|
| Origin Group auto-failover | ~seconds | Primary origin returns 5xx/403 | Real regional outages |
| **DDB flag flip (Lambda@Edge)** | **~15s** | **Operator write to DDB** | **Planned / proactive failover** |
| Bucket policy Deny flip | ~5s | Operator policy change | Drills, forced 5xx for testing |
| `config.json` + invalidation | ~30-60s | Operator updates config | Connect-only failures |

They stack without conflict — if one fails, the next layer catches.

### When to Use

- You need **proactive** operator-driven failover without waiting for origin errors
- You foresee canary rollouts, geographic routing, or session pinning
- You have the operational capacity to manage Lambda@Edge (bundling, versioning, logs)
- You want the richest failover toolkit and are comfortable with more moving parts

### When to Skip

- Option 6 already meets your RTO (it does for MACP Connect's 4h target)
- You want minimum infrastructure surface area
- Team lacks bandwidth to own Lambda@Edge operations
- You don't foresee needing canary / geographic / session-based routing

### Relationship to Option 6

Option 7 is a **strict superset** of Option 6. You can deploy Option 6 first and add Option 7 later without re-architecting — the Lambda@Edge association is an additive change to the existing CloudFront distribution. This makes "start with 6, upgrade to 7 if needed" a reasonable path.

---

## Comparison Matrix

### Failover Method, Time, and Cost Summary

Unified view of all options. Cost assumes low-to-moderate traffic (≤10M requests/month typical for admin portal + CCP).

| # | Option | Failover Method | Failover Time | Monthly Cost | Viable? |
|---|---|---|---|---|---|
| 1 | CloudFront Alias Swap | Manual CFN update: remove aliases from primary distribution, add to DR distribution; wait for propagation | 30–45 min | ~$10–20 | ✅ |
| 2 | CloudFront Path-Based Routing | Same as Option 1 — path-based routing is orthogonal to failover mechanism | 30–45 min | ~$10–20 | ✅ (variant of 1) |
| 3 | API Gateway + Route 53 Failover | Route 53 health check flips record from PRIMARY to SECONDARY regional API Gateway | ~60 sec (DNS TTL) | ~$40–60 | ✅ |
| 4 | Global Accelerator + ALB | Global Accelerator endpoint health check auto-flips; or manual endpoint weight change | ~30 sec | ~$60–80+ | ✅ |
| 5 | CloudFront Functions (Host header rewrite) | N/A — CloudFront validates Host before Functions execute | N/A | N/A | ❌ Not viable |
| 6 | **CloudFront Origin Group (Recommended)** | **Automatic:** CloudFront auto-fails over on primary origin 5xx/403<br>**Manual:** bucket policy Deny flip (forces 403) or `config.json` invalidation | **~seconds (auto)**<br>**~5–60 sec (manual)** | **~$15–25 (incl. CRR)** | ✅ |
| 7 | **Option 6 + Lambda@Edge + DDB Global Table** | All Option 6 mechanisms **plus**:<br>**Proactive:** operator writes DDB item; Lambda@Edge reads flag at all edges | **~15 sec (proactive)**<br>Plus all Option 6 paths | **~$20–35** | ✅ |

### Cost Breakdown Detail

| Cost Component | Option 1 | Option 3 | Option 4 | Option 6 | Option 7 |
|---|---|---|---|---|---|
| CloudFront distribution(s) | $5–10 (×2) | — | — | $5–10 (×1) | $5–10 (×1) |
| S3 storage (primary + DR) | $2–5 | $2–5 | $2–5 | $2–5 | $2–5 |
| S3 Cross-Region Replication | — | — | — | $2–5 | $2–5 |
| API Gateway (requests) | — | $20–30 | — | — | — |
| Regional WAF (×2 regions) | — | $10–15 | $10–15 | — | — |
| ALB (×2 regions) | — | — | $40–45 | — | — |
| Global Accelerator | — | — | $18 | — | — |
| Lambda/Lambda@Edge | — | $2–5 | — | — | $1–5 |
| DynamoDB Global Table | — | — | — | — | $1–3 |
| Route 53 hosted zone + health checks | $1–2 | $1–2 | $1–2 | $1 | $1 |
| CloudWatch logs / alarms | $1–2 | $1–2 | $1–2 | $1 | $1–2 |
| **Total (typical)** | **~$10–20** | **~$40–60** | **~$60–80** | **~$15–25** | **~$20–35** |

Costs exclude: data transfer (~$0.085/GB out), request charges beyond free tier, ACM (free for CloudFront), CloudTrail. Scale linearly with traffic for request-priced components.

### Detailed Criteria Comparison

| Criteria | Option 1: Alias Swap | Option 3: API Gateway | Option 4: Global Accelerator | Option 6: Origin Group | Option 7: Option 6 + L@E |
|----------|----------------------|-----------------------|------------------------------|------------------------|--------------------------|
| **Failover Time (automatic)** | N/A | ~60 sec | ~30 sec | ~seconds | ~seconds (L@E) + safety net |
| **Failover Time (manual)** | 30–45 min | ~60 sec | ~30 sec | ~5–60 sec | ~15 sec (DDB flip) |
| **Latency (cached)** | 10–30ms | 50–150ms | 20–50ms | 10–30ms | 10–30ms |
| **Edge Caching** | ✅ Yes | ❌ No | ❌ No | ✅ Yes | ✅ Yes |
| **Edge WAF** | ✅ Yes | ❌ Regional | ❌ Regional | ✅ Yes | ✅ Yes |
| **Geo-Restriction** | ✅ Built-in | ⚠️ Regional WAF | ⚠️ Regional WAF | ✅ Built-in | ✅ Built-in |
| **S3 Integration** | ✅ Simple (OAC) | ⚠️ Complex | ⚠️ Complex | ✅ Simple (OAC) | ✅ Simple (OAC) |
| **Setup Complexity** | Low | High | High | Medium (CRR) | Medium-High |
| **Same domain both regions** | ❌ No | ✅ Yes | ✅ Yes (anycast IPs) | ✅ Yes | ✅ Yes |
| **Handles full region outage** | ⚠️ Manual, slow | ✅ Route 53 | ✅ GA health | ✅ Auto origin group | ✅ Auto + proactive |
| **Handles Connect-only failure** | ⚠️ Manual | ✅ Via health check | ⚠️ Requires ALB health | ✅ `config.json` flip | ✅ DDB flip (cleanest) |
| **Supports canary/weighted** | ❌ No | ⚠️ Route 53 weighted | ✅ GA weights | ❌ No | ✅ Via L@E logic |
| **Supports session pinning** | ❌ No | ⚠️ Sticky via Lambda | ✅ Client affinity | ❌ No | ✅ Via L@E cookie |
| **`us-east-1` control-plane dependency** | ✅ CloudFront config change required | ⚠️ Route 53 write needed | ⚠️ GA config change | ❌ S3 regional API only | ⚠️ DDB write (can use `us-west-2` replica) |
| **Uses Existing WAF** | ✅ Yes | ❌ Regional copies | ❌ Regional copies | ✅ Yes | ✅ Yes |
| **Pre-provisioning required for DR** | Both distributions | Both API stacks | Both ALBs | Both buckets + CRR | All of 6 + DDB + L@E |
| **Audit trail of failover events** | CloudTrail (CFN update) | CloudTrail (R53 change) | CloudTrail (GA change) | CloudTrail (S3 policy) | DDB PITR + CloudTrail |

---

## Recommendation

### For Full-Region Failover: Option 6 (Single CloudFront with Origin Group)

**Choose this if:**
- Full-region failover is the design goal (this project)
- You want near-instant automatic failover without losing edge caching/WAF
- You want a single domain, single distribution to manage
- RPO tolerates S3 CRR replication lag (seconds to minutes)

This is the recommended baseline for MACP Connect DR given the full-region failover objective.

### For Maximum Control and Flexibility: Option 7 (Option 6 + Lambda@Edge + DDB Global Table)

**Choose this if:**
- You need proactive operator-driven failover without forcing origin errors
- You foresee canary rollouts, geographic routing, or session pinning
- You want a single-call (DDB write) flip mechanism that works during a `us-east-1` outage
- You can own the additional operational surface (Lambda bundling, versioning, edge logs)

Option 7 is a strict superset of Option 6 — deploy Option 6 first, layer Option 7 on when the need for proactive routing emerges.

### For Most Use Cases: Option 1 (CloudFront with Alias Swap)

**Choose this if:**
- Your RTO can accommodate 30-45 minute failover
- You're serving primarily static content
- Edge caching significantly benefits your users
- You want to leverage existing CloudFront/WAF configuration
- Operational simplicity is valued and CRR is undesirable

### For Critical RTO Requirements: Option 3 (API Gateway)

**Choose this if:**
- You absolutely need sub-5-minute failover
- Content is mostly dynamic anyway
- You're already heavily invested in API Gateway
- Additional complexity and cost are justified
- Edge caching is not a priority

### Hybrid Approach (Legacy)

Consider a hybrid where:
- Static content: CloudFront (accept longer failover)
- APIs: API Gateway with Route 53 failover (instant)

Option 6 supersedes this hybrid by placing API Gateway origins into the same CloudFront with a second origin group, giving both layers automatic failover behind one distribution.

---

## Current Implementation Status

### Deployed (CloudFront Approach)

| Region | Component | Status | ID |
|--------|-----------|--------|-----|
| us-east-1 | S3 Bucket | ✅ Deployed | test-macp-dos-prod-us-east-1-cloudfront-content |
| us-east-1 | CloudFront | ✅ Deployed | E2GLW8V93PVKFQ (d1vhple0dnr5gl.cloudfront.net) |
| us-east-1 | Custom Aliases | ✅ Configured | 6 subdomains |
| us-west-2 | S3 Bucket | ✅ Deployed | test-macp-dos-prod-us-west-2-cloudfront-content |
| us-west-2 | CloudFront | ✅ Deployed | E2AUNDW1F8HW11 (d3nh2q65al3cia.cloudfront.net) |
| us-west-2 | Custom Aliases | ❌ None | Ready for failover |

### Test URLs

- **Primary (us-east-1):** https://d1vhple0dnr5gl.cloudfront.net/index.html
- **DR (us-west-2):** https://d3nh2q65al3cia.cloudfront.net/index.html

---

## Files Reference

| File | Purpose |
|------|---------|
| `cloudformation/admin-portal-infrastructure.yaml` | Main infrastructure template (S3 + CloudFront) |
| `cloudformation/route53-failover.yaml` | Original Route 53 with health checks (subdomain approach) |
| `cloudformation/route53-manual-failover.yaml` | Simplified Route 53 for manual failover |
| `failover.sh` | Shell script for manual failover operations |
| `DEPLOYMENT.md` | Deployment guide |

---

## Appendix: CloudFront Alias Validation

### Why CloudFront Rejects Mismatched Host Headers

When a request arrives at CloudFront:

1. CloudFront extracts the `Host` header
2. Compares against configured `Aliases`
3. If no match found → **403 Forbidden**

```bash
# This fails (Host doesn't match any alias)
curl -sI https://d3nh2q65al3cia.cloudfront.net/index.html \
  -H "Host: portal.prod.gsa.dos.macp.cloud"
# HTTP/2 403

# This works (Host matches CloudFront's own domain)
curl -sI https://d3nh2q65al3cia.cloudfront.net/index.html
# HTTP/2 200
```

This behavior is by design to prevent domain fronting attacks and ensure SSL certificate validation integrity.

---

## Appendix: API Gateway Custom Domain Configuration

### Regional API Gateway Custom Domain

Unlike CloudFront, API Gateway allows the same custom domain in multiple regions:

```yaml
# us-east-1
PrimaryCustomDomain:
  Type: AWS::ApiGateway::DomainName
  Properties:
    DomainName: portal.prod.gsa.dos.macp.cloud
    RegionalCertificateArn: arn:aws:acm:us-east-1:...  # Regional cert
    EndpointConfiguration:
      Types:
        - REGIONAL

# us-west-2 (SAME DOMAIN!)
DRCustomDomain:
  Type: AWS::ApiGateway::DomainName
  Properties:
    DomainName: portal.prod.gsa.dos.macp.cloud
    RegionalCertificateArn: arn:aws:acm:us-west-2:...  # Regional cert
    EndpointConfiguration:
      Types:
        - REGIONAL
```

Route 53 then uses failover or weighted routing to direct traffic to the appropriate regional endpoint.

---

*Document created: 2026-04-17*
*Last updated: 2026-04-18*
