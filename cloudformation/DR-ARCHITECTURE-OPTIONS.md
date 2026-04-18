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

## Comparison Matrix

| Criteria | Option 1: CloudFront Alias Swap | Option 3: API Gateway |
|----------|--------------------------------|----------------------|
| **Failover Time** | 30-45 minutes | ~60 seconds |
| **Latency (cached)** | 10-30ms | 50-150ms |
| **Edge Caching** | ✅ Yes | ❌ No |
| **Edge WAF** | ✅ Yes | ❌ Regional only |
| **Geo-Restriction** | ✅ Built-in | ⚠️ Regional WAF rules |
| **S3 Integration** | ✅ Simple (OAC) | ⚠️ Complex |
| **Setup Complexity** | Low | High |
| **Monthly Cost** | ~$10-20 | ~$40-60 |
| **Uses Existing WAF** | ✅ Yes | ❌ Need regional copies |
| **Same domain both regions** | ❌ No | ✅ Yes |

---

## Recommendation

### For Most Use Cases: Option 1 (CloudFront with Alias Swap)

**Choose this if:**
- Your RTO can accommodate 30-45 minute failover
- You're serving primarily static content
- Edge caching significantly benefits your users
- You want to leverage existing CloudFront/WAF configuration
- Operational simplicity is valued

### For Critical RTO Requirements: Option 3 (API Gateway)

**Choose this if:**
- You absolutely need sub-5-minute failover
- Content is mostly dynamic anyway
- You're already heavily invested in API Gateway
- Additional complexity and cost are justified

### Hybrid Approach (Future)

Consider a hybrid where:
- Static content: CloudFront (accept longer failover)
- APIs: API Gateway with Route 53 failover (instant)

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
*Last updated: 2026-04-17*
