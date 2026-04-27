# Amazon Connect Disaster Recovery Plan
## Customer-Facing Resources Failover Strategy

### Overview

**Architecture**: Active-Passive with two Amazon Connect instances
- **Primary**: us-east-1 (`macp-dos-prod-connect-1`)
- **DR**: us-west-2 (`macp-dos-prod-connect-2`)
- **RTO Requirement**: 4 hours (designing for faster capability)
- **Failover Model**: Semi-automatic (one-click approval)
- **Base Domain**: `prod.gsa.dos.macp.cloud`

### Connect Instance URLs

| Region | Instance Alias | Admin Console URL |
|--------|---------------|-------------------|
| us-east-1 (Primary) | macp-dos-prod-connect-1 | https://macp-dos-prod-connect-1.my.connect.aws/ |
| us-west-2 (DR) | macp-dos-prod-connect-2 | https://macp-dos-prod-connect-2.my.connect.aws/ |

### Scope: Customer-Facing Resources

| Resource | Failover Mechanism | Notes |
|----------|-------------------|-------|
| Connect Admin Page | Route 53 + CloudFront + **Redirect** | Cannot proxy; uses JS redirect to active instance |
| CCP / Workspace | CloudFront + S3 | Region-aware configuration |
| Chat Widget | Embedded with custom domain | API endpoint switching |

---

## 1. DNS Architecture with Route 53

### 1.1 Domain Structure

```
Primary Domains (customer-facing):
├── admin.prod.gsa.dos.macp.cloud      → Connect Admin Redirect Portal
├── agent.prod.gsa.dos.macp.cloud      → CCP/Workspace
├── chat-api.prod.gsa.dos.macp.cloud   → Chat Widget Backend
└── chat.prod.gsa.dos.macp.cloud       → Chat Widget Assets

Internal/Control Domains:
├── health.prod.gsa.dos.macp.cloud     → Health check endpoints
└── config.prod.gsa.dos.macp.cloud     → Dynamic configuration (active region API)
```

### 1.2 Route 53 Health Checks

Create health checks for each Connect instance:

```yaml
HealthChecks:
  - Name: connect-east1-health
    Type: HTTPS
    Endpoint: https://macp-dos-prod-connect-1.my.connect.aws
    Port: 443
    Path: /
    FailureThreshold: 3
    RequestInterval: 30
    Regions: [us-east-1, us-west-2, eu-west-1]
    
  - Name: connect-west2-health
    Type: HTTPS  
    Endpoint: https://macp-dos-prod-connect-2.my.connect.aws
    Port: 443
    Path: /
    FailureThreshold: 3
    RequestInterval: 30
    Regions: [us-east-1, us-west-2, eu-west-1]
```

### 1.3 Failover DNS Records

```yaml
# Primary record - uses failover routing
admin.prod.gsa.dos.macp.cloud:
  Type: A (Alias)
  RoutingPolicy: Failover
  SetIdentifier: Primary
  Failover: PRIMARY
  Target: CloudFront Distribution (East)
  HealthCheckId: connect-east1-health

admin.prod.gsa.dos.macp.cloud:
  Type: A (Alias)
  RoutingPolicy: Failover
  SetIdentifier: Secondary
  Failover: SECONDARY
  Target: CloudFront Distribution (West)
  # No health check - accepts traffic when primary fails
```

---

## 2. CCP / Workspace Failover

### 2.1 Architecture

```
                    ┌─────────────────┐
                    │   Route 53      │
                    │  Failover DNS   │
                    └────────┬────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
              ▼                             ▼
    ┌─────────────────┐           ┌─────────────────┐
    │   CloudFront    │           │   CloudFront    │
    │   (Primary)     │           │   (DR)          │
    └────────┬────────┘           └────────┬────────┘
             │                             │
             ▼                             ▼
    ┌─────────────────┐           ┌─────────────────┐
    │  S3 - East      │           │  S3 - West      │
    │  CCP/Workspace  │           │  CCP/Workspace  │
    │  Assets         │           │  Assets         │
    └─────────────────┘           └─────────────────┘
```

### 2.2 Region-Aware CCP Configuration

The CCP application must dynamically determine which Connect instance to use:

```javascript
// config.js - Served from S3, region-specific
const CONFIG = {
  region: 'us-east-1', // or 'us-west-2' for DR bucket
  connectInstanceAlias: 'macp-dos-prod-connect-1', // or 'macp-dos-prod-connect-2' for DR
  connectInstanceUrl: 'https://macp-dos-prod-connect-1.my.connect.aws',
  ccpUrl: 'https://macp-dos-prod-connect-1.my.connect.aws/ccp-v2',
  
  // Feature flags
  isFailoverMode: false,
  failoverTimestamp: null
};
```

**Option A: Static Config per Region (Recommended for simplicity)**
- Each S3 bucket contains region-specific `config.js`
- Failover = DNS points to DR CloudFront = DR config served automatically

**Option B: Dynamic Config via API**
- Single config endpoint that returns region based on Route 53 determination
- More complex but allows runtime switching without cache invalidation

### 2.3 S3 Bucket Replication

```yaml
ReplicationConfiguration:
  Role: arn:aws:iam::ACCOUNT:role/s3-replication-role
  Rules:
    - Id: ccp-assets-replication
      Status: Enabled
      Priority: 1
      DeleteMarkerReplication:
        Status: Disabled
      Filter:
        Prefix: ""
      Destination:
        Bucket: arn:aws:s3:::ccp-assets-west2
        ReplicationTime:
          Status: Enabled
          Time:
            Minutes: 15
        Metrics:
          Status: Enabled
```

### 2.4 CloudFront Configuration

Both distributions should have identical configurations:

```yaml
CloudFrontDistribution:
  Origins:
    - Id: S3Origin
      DomainName: ccp-assets-{region}.s3.amazonaws.com
      S3OriginConfig:
        OriginAccessIdentity: origin-access-identity/cloudfront/XXXXX
  
  DefaultCacheBehavior:
    TargetOriginId: S3Origin
    ViewerProtocolPolicy: redirect-to-https
    CachePolicyId: 658327ea-f89d-4fab-a63d-7e88639e58f6  # CachingOptimized
    
  # Custom error pages for graceful degradation
  CustomErrorResponses:
    - ErrorCode: 503
      ResponsePagePath: /maintenance.html
      ResponseCode: 503
      ErrorCachingMinTTL: 60
```

---

## 3. Chat Widget Failover

### 3.1 Architecture

```
Customer Website
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│  Embedded Chat Widget (JavaScript)                          │
│  - Loads config from: chat-config.prod.gsa.dos.macp.cloud   │
│  - Connects to: chat-api.prod.gsa.dos.macp.cloud            │
└─────────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────┐
│   Route 53      │
│  Failover DNS   │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
 East API   West API
(Primary)    (DR)
```

### 3.2 Chat Widget Configuration Endpoint

Create a lightweight API (Lambda + API Gateway) that returns region-appropriate configuration:

```javascript
// Lambda: get-chat-config
exports.handler = async (event) => {
  // This Lambda runs in both regions
  // Returns config pointing to local Connect instance
  
  const region = process.env.AWS_REGION;
  const config = {
    region: region,
    instanceId: process.env.CONNECT_INSTANCE_ID,
    contactFlowId: process.env.CONTACT_FLOW_ID,
    apiEndpoint: `https://${process.env.CONNECT_INSTANCE_ID}.execute-api.${region}.amazonaws.com`,
    
    // Participant service endpoint
    participantServiceEndpoint: `https://participant.connect.${region}.amazonaws.com`
  };
  
  return {
    statusCode: 200,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'max-age=60' // Short cache for failover responsiveness
    },
    body: JSON.stringify(config)
  };
};
```

### 3.3 Chat Widget Initialization (Customer Website)

```javascript
// Embedded in customer website
async function initializeChatWidget() {
  try {
    // Fetch region-aware configuration
    const response = await fetch('https://chat-config.prod.gsa.dos.macp.cloud/config');
    const config = await response.json();
    
    // Initialize Amazon Connect Chat
    amazon_connect('init', {
      instanceId: config.instanceId,
      contactFlowId: config.contactFlowId,
      region: config.region,
      // ... other options
    });
    
  } catch (error) {
    console.error('Failed to initialize chat:', error);
    // Show fallback UI (phone number, email, etc.)
    showFallbackContactOptions();
  }
}
```

### 3.4 API Gateway Regional Endpoints

```yaml
# East Region API Gateway
EastChatAPI:
  Type: AWS::ApiGateway::RestApi
  Properties:
    Name: connect-chat-api
    EndpointConfiguration:
      Types: [REGIONAL]

# Route 53 Health Check for API
ChatAPIHealthCheck:
  Type: AWS::Route53::HealthCheck
  Properties:
    HealthCheckConfig:
      Type: HTTPS
      FullyQualifiedDomainName: !Sub "${EastChatAPI}.execute-api.us-east-1.amazonaws.com"
      Port: 443
      ResourcePath: /health
      FailureThreshold: 3
      RequestInterval: 30
```

---

## 4. Connect Admin Page Access

### 4.1 Challenge

Amazon Connect admin console URLs are instance-specific and **cannot be proxied**:
- Primary: `https://macp-dos-prod-connect-1.my.connect.aws/`
- DR: `https://macp-dos-prod-connect-2.my.connect.aws/`

CloudFront cannot act as a reverse proxy to these URLs because:
1. Connect uses its own authentication/session management
2. The `.my.connect.aws` domain has strict CORS and cookie policies
3. AWS does not support custom domains for Connect admin console

### 4.2 Solution: Admin Portal Redirect Page

**Architecture Overview:**

```
┌─────────────────────────────────────────────────────────────────────┐
│  User navigates to: https://admin.prod.gsa.dos.macp.cloud           │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Route 53 (Alias A Record)                                          │
│  Points to: CloudFront Distribution                                 │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  CloudFront Distribution                                            │
│  Origin: S3 bucket containing static redirect HTML page             │
│  (NOT proxying to Connect - just serving static HTML)               │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  S3 Bucket: admin-portal-prod-gsa-dos-macp                          │
│  Contents: index.html (redirect page)                               │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Browser receives index.html, JavaScript executes:                  │
│  1. Fetches active region from config API                           │
│  2. Performs client-side redirect (302-equivalent via JS)           │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
                    ▼                               ▼
    ┌───────────────────────────┐   ┌───────────────────────────┐
    │  If activeRegion =        │   │  If activeRegion =        │
    │  "us-east-1":             │   │  "us-west-2":             │
    │                           │   │                           │
    │  Redirect to:             │   │  Redirect to:             │
    │  macp-dos-prod-connect-1  │   │  macp-dos-prod-connect-2  │
    │  .my.connect.aws          │   │  .my.connect.aws          │
    └───────────────────────────┘   └───────────────────────────┘
```

**Key Point:** CloudFront serves a static HTML file that performs a client-side redirect. 
The user's browser URL will change from `admin.prod.gsa.dos.macp.cloud` to `macp-dos-prod-connect-X.my.connect.aws` after redirect.

### 4.3 Redirect Page Implementation

```html
<!-- S3: s3://admin-portal-prod-gsa-dos-macp/index.html -->
<!-- Served via CloudFront at: https://admin.prod.gsa.dos.macp.cloud -->
<!DOCTYPE html>
<html>
<head>
  <title>MACP Contact Center Admin</title>
  <style>
    body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }
    .spinner { border: 4px solid #f3f3f3; border-top: 4px solid #3498db; 
               border-radius: 50%; width: 40px; height: 40px; 
               animation: spin 1s linear infinite; margin: 20px auto; }
    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    .error { color: #c0392b; display: none; }
    .manual-links { margin-top: 20px; }
    .manual-links a { margin: 0 10px; color: #2980b9; }
  </style>
  <script>
    const CONNECT_INSTANCES = {
      'us-east-1': 'https://macp-dos-prod-connect-1.my.connect.aws/',
      'us-west-2': 'https://macp-dos-prod-connect-2.my.connect.aws/'
    };
    
    const CONFIG_ENDPOINT = 'https://config.prod.gsa.dos.macp.cloud/active-region';
    const DEFAULT_REGION = 'us-east-1';
    
    async function redirectToAdmin() {
      try {
        const response = await fetch(CONFIG_ENDPOINT, { 
          cache: 'no-store',
          headers: { 'Accept': 'application/json' }
        });
        
        if (!response.ok) throw new Error('Config fetch failed');
        
        const config = await response.json();
        const targetUrl = CONNECT_INSTANCES[config.activeRegion] || CONNECT_INSTANCES[DEFAULT_REGION];
        
        // Redirect to the active Connect instance
        window.location.href = targetUrl;
        
      } catch (error) {
        console.error('Failed to fetch config:', error);
        // Show error message and manual links
        document.getElementById('loading').style.display = 'none';
        document.getElementById('error').style.display = 'block';
        document.getElementById('manual-links').style.display = 'block';
      }
    }
    
    // Start redirect on page load
    window.onload = redirectToAdmin;
  </script>
</head>
<body>
  <h1>MACP Contact Center Admin</h1>
  
  <div id="loading">
    <div class="spinner"></div>
    <p>Redirecting to Contact Center Admin Console...</p>
  </div>
  
  <div id="error" class="error">
    <p>Unable to determine active region. Please select manually:</p>
  </div>
  
  <div id="manual-links" class="manual-links" style="display: none;">
    <a href="https://macp-dos-prod-connect-1.my.connect.aws/">US-East-1 (Primary)</a>
    <a href="https://macp-dos-prod-connect-2.my.connect.aws/">US-West-2 (DR)</a>
  </div>
</body>
</html>
```

### 4.4 Config API (Active Region Endpoint)

The redirect page calls `https://config.prod.gsa.dos.macp.cloud/active-region` to determine which Connect instance is active.

**Lambda Function:**
```javascript
// Lambda: get-active-region (deployed in both regions)
const AWS = require('aws-sdk');
const dynamodb = new AWS.DynamoDB.DocumentClient();

exports.handler = async (event) => {
  try {
    const result = await dynamodb.get({
      TableName: 'connect-dr-config',
      Key: { configKey: 'active-region' }
    }).promise();
    
    return {
      statusCode: 200,
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control': 'no-cache, no-store, must-revalidate'
      },
      body: JSON.stringify({
        activeRegion: result.Item?.value || 'us-east-1',
        updatedAt: result.Item?.updatedAt || null
      })
    };
  } catch (error) {
    // Default to primary on error
    return {
      statusCode: 200,
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*'
      },
      body: JSON.stringify({ activeRegion: 'us-east-1', error: true })
    };
  }
};
```

### 4.5 Active Region Configuration Store

DynamoDB Global Table stores the active region (replicated across both regions):

```yaml
ActiveRegionTable:
  Type: AWS::DynamoDB::GlobalTable
  Properties:
    TableName: connect-dr-config
    AttributeDefinitions:
      - AttributeName: configKey
        AttributeType: S
    KeySchema:
      - AttributeName: configKey
        KeyType: HASH
    Replicas:
      - Region: us-east-1
      - Region: us-west-2
    BillingMode: PAY_PER_REQUEST

# Initial data:
# { configKey: "active-region", value: "us-east-1", updatedAt: "2026-04-16T12:00:00Z" }
```

### 4.6 CloudFront Distribution for Admin Portal

```yaml
AdminPortalCloudFront:
  Type: AWS::CloudFront::Distribution
  Properties:
    DistributionConfig:
      Aliases:
        - admin.prod.gsa.dos.macp.cloud
      Origins:
        - Id: S3Origin
          DomainName: admin-portal-prod-gsa-dos-macp.s3.amazonaws.com
          S3OriginConfig:
            OriginAccessIdentity: !Sub "origin-access-identity/cloudfront/${OAI}"
      DefaultRootObject: index.html
      DefaultCacheBehavior:
        TargetOriginId: S3Origin
        ViewerProtocolPolicy: redirect-to-https
        # Short TTL since this is a redirect page
        CachePolicyId: 4135ea2d-6df8-44a3-9df3-4b5a84be39ad  # CachingDisabled
        AllowedMethods: [GET, HEAD]
      ViewerCertificate:
        AcmCertificateArn: arn:aws:acm:us-east-1:ACCOUNT:certificate/xxxxx
        SslSupportMethod: sni-only
        MinimumProtocolVersion: TLSv1.2_2021
```

### 4.7 User Experience Flow

1. **Normal operation**: User visits `admin.prod.gsa.dos.macp.cloud` → sees spinner for ~500ms → redirected to `macp-dos-prod-connect-1.my.connect.aws`

2. **After failover**: User visits same URL → config returns `us-west-2` → redirected to `macp-dos-prod-connect-2.my.connect.aws`

3. **Config API failure**: User sees error message with manual links to both instances

---

## 5. Semi-Automatic Failover Workflow

### 5.1 Failover Decision Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                     MONITORING PHASE                             │
├─────────────────────────────────────────────────────────────────┤
│  Route 53 Health Checks → CloudWatch Alarms → SNS Notification  │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                     NOTIFICATION PHASE                           │
├─────────────────────────────────────────────────────────────────┤
│  1. SNS sends alert to:                                         │
│     - PagerDuty/OpsGenie                                        │
│     - Slack channel                                              │
│     - Email distribution list                                   │
│  2. Alert contains one-click approval link                      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                     APPROVAL PHASE                               │
├─────────────────────────────────────────────────────────────────┤
│  Operator clicks approval link → API Gateway → Step Functions   │
│                                                                  │
│  Approval UI shows:                                             │
│  - Current health status                                        │
│  - Impact assessment                                            │
│  - [APPROVE FAILOVER] [DISMISS - FALSE ALARM]                   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                     EXECUTION PHASE                              │
├─────────────────────────────────────────────────────────────────┤
│  Step Functions executes:                                       │
│  1. Update DynamoDB active-region config                        │
│  2. Invalidate CloudFront caches                                │
│  3. Update Route 53 records (if using weighted routing)         │
│  4. Send confirmation notifications                              │
│  5. Log audit trail                                             │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 Step Functions State Machine

```yaml
FailoverStateMachine:
  StartAt: ValidateFailoverRequest
  States:
    ValidateFailoverRequest:
      Type: Task
      Resource: arn:aws:lambda:REGION:ACCOUNT:function:validate-failover
      Next: UpdateActiveRegion
      
    UpdateActiveRegion:
      Type: Task
      Resource: arn:aws:lambda:REGION:ACCOUNT:function:update-active-region
      Parameters:
        targetRegion.$: $.targetRegion
      Next: InvalidateCaches
      
    InvalidateCaches:
      Type: Parallel
      Branches:
        - StartAt: InvalidateCCPCache
          States:
            InvalidateCCPCache:
              Type: Task
              Resource: arn:aws:lambda:REGION:ACCOUNT:function:invalidate-cloudfront
              Parameters:
                distributionId: ${CCPDistributionId}
              End: true
        - StartAt: InvalidateAdminCache
          States:
            InvalidateAdminCache:
              Type: Task
              Resource: arn:aws:lambda:REGION:ACCOUNT:function:invalidate-cloudfront
              Parameters:
                distributionId: ${AdminDistributionId}
              End: true
      Next: SendNotifications
      
    SendNotifications:
      Type: Task
      Resource: arn:aws:lambda:REGION:ACCOUNT:function:send-failover-notification
      Next: AuditLog
      
    AuditLog:
      Type: Task
      Resource: arn:aws:lambda:REGION:ACCOUNT:function:audit-log-failover
      End: true
```

### 5.3 One-Click Approval Interface

```javascript
// Lambda: generate-approval-link
exports.handler = async (event) => {
  const approvalToken = crypto.randomUUID();
  const expiresAt = Date.now() + (60 * 60 * 1000); // 1 hour
  
  // Store token in DynamoDB
  await dynamodb.put({
    TableName: 'failover-approval-tokens',
    Item: {
      token: approvalToken,
      expiresAt: expiresAt,
      sourceRegion: event.failedRegion,
      targetRegion: event.targetRegion,
      status: 'pending'
    }
  }).promise();
  
  // Generate approval URL
  const approvalUrl = `https://failover.prod.gsa.dos.macp.cloud/approve?token=${approvalToken}`;
  
  return {
    approvalUrl,
    expiresAt: new Date(expiresAt).toISOString()
  };
};
```

---

## 6. Implementation Todos

### Phase 1: DNS & Health Monitoring
- [ ] Create Route 53 hosted zone for custom domains
- [ ] Configure health checks for both Connect instances
- [ ] Set up CloudWatch alarms for health check failures
- [ ] Configure SNS topics for alerting

### Phase 2: CCP/Workspace Infrastructure
- [ ] Create S3 buckets in both regions with versioning
- [ ] Configure cross-region replication
- [ ] Deploy CloudFront distributions
- [ ] Set up Route 53 failover records for CCP domain
- [ ] Deploy region-specific configuration files

### Phase 3: Chat Widget Infrastructure
- [ ] Create Lambda functions for chat config endpoint
- [ ] Deploy API Gateway in both regions
- [ ] Configure Route 53 failover for chat API
- [ ] Update chat widget initialization code on customer website

### Phase 4: Admin Portal
- [ ] Create admin redirect page
- [ ] Deploy DynamoDB Global Table for config
- [ ] Create config API endpoint
- [ ] Deploy CloudFront distribution for admin portal

### Phase 5: Failover Automation
- [ ] Build Step Functions state machine
- [ ] Create approval token Lambda
- [ ] Build approval UI
- [ ] Configure SNS → Lambda for automated alert processing
- [ ] Test end-to-end failover workflow

### Phase 6: Testing & Documentation
- [ ] Conduct tabletop failover exercise
- [ ] Execute actual failover test in non-production
- [ ] Document runbooks for manual fallback procedures
- [ ] Train operations team on approval workflow

---

## 7. Considerations & Trade-offs

### What This Plan Covers
✅ Customer-facing resources (CCP, Workspace, Chat Widget)
✅ Admin console access redirection
✅ Automated monitoring with human-in-the-loop approval
✅ Sub-minute DNS propagation via Route 53 health checks

### What This Plan Does NOT Cover (Future Phases)
- Phone number porting / carrier-level failover
- Contact flow synchronization between regions
- Agent state replication
- Historical metrics / reporting continuity
- Lambda function failover (Connect integrations)
- Lex bot replication
- Quick Connect / Queue / Routing Profile sync

### Known Limitations
1. **Active calls will be dropped** during failover - there is no live call migration between Connect instances
2. **Agent sessions will be terminated** - agents must re-login after failover
3. **Chat sessions in progress** may be lost depending on timing
4. **Phone numbers** are region-specific - requires carrier-level DR planning

---

## 8. Cost Estimate (Monthly)

| Component | Estimated Cost |
|-----------|---------------|
| Route 53 Health Checks (4 checks × 3 regions) | ~$6 |
| CloudFront Distributions (2) | ~$10-50 (traffic dependent) |
| S3 Cross-Region Replication | ~$5 (storage dependent) |
| DynamoDB Global Tables | ~$5 |
| Lambda + API Gateway | ~$10 |
| Step Functions | ~$1 |
| **Total Additional DR Cost** | **~$40-80/month** |

---

## Next Steps

1. Review and approve this plan
2. Prioritize implementation phases based on criticality
3. Begin Phase 1 (DNS & Health Monitoring) as foundation
4. Consider future phases for phone number and agent-side DR
