# S3 Bucket Configuration - Complete Property Documentation

## Overview
CloudFormation template will create **2 S3 buckets** (one per region) with identical configuration except for regional parameters.

---

## Bucket Names

### Primary Region (us-east-1)
**Bucket Name:** `macp-prod-us-east-1-cloudfront-content`

### DR Region (us-west-2)
**Bucket Name:** `macp-prod-us-west-2-cloudfront-content`

**Naming Pattern:** `macp-${Environment}-${Region}-cloudfront-content`
- Environment: prod (parameter)
- Region: us-east-1 or us-west-2 (parameter)

---

## Core Configuration Properties

### 1. BucketName
```yaml
BucketName: !Sub 'macp-${Environment}-${Region}-cloudfront-content'
```
- **us-east-1:** macp-prod-us-east-1-cloudfront-content
- **us-west-2:** macp-prod-us-west-2-cloudfront-content

### 2. BucketEncryption
```yaml
ServerSideEncryptionConfiguration:
  - ServerSideEncryptionByDefault:
      SSEAlgorithm: AES256
    BucketKeyEnabled: false
```
- **Encryption Type:** AES256 (SSE-S3, AWS-managed keys)
- **Bucket Key:** Disabled (matches existing bucket)
- **Effect:** All objects encrypted at rest automatically
- **Key Management:** AWS handles key rotation

### 3. VersioningConfiguration
```yaml
VersioningConfiguration:
  Status: Enabled
```
- **Status:** Enabled
- **Effect:** Every object modification creates a new version
- **Allows:** Recovery from accidental deletions/overwrites
- **Cost Impact:** Stores all versions until explicitly deleted

### 4. PublicAccessBlockConfiguration
```yaml
PublicAccessBlockConfiguration:
  BlockPublicAcls: true
  BlockPublicPolicy: true
  IgnorePublicAcls: true
  RestrictPublicBuckets: true
```
- **BlockPublicAcls:** Prevents new public ACLs from being applied
- **IgnorePublicAcls:** Ignores any existing public ACLs
- **BlockPublicPolicy:** Prevents bucket policies that grant public access
- **RestrictPublicBuckets:** Only AWS services and authorized users can access
- **Result:** Bucket is completely private, no public access possible

### 5. LoggingConfiguration (Server Access Logs)
```yaml
LoggingConfiguration:
  DestinationBucketName: !FindInMap [RegionalConfig, !Ref Region, S3LogBucket]
  LogFilePrefix: macp-dos-prod/cloudfront-content
```

**us-east-1:**
- **Destination:** maximus-federal-s3-logs-417886991978-us-east-1
- **Prefix:** macp-dos-prod/cloudfront-content
- **Log Format:** macp-dos-prod/cloudfront-content[YYYY]-[MM]-[DD]-[hh]-[mm]-[ss]-[UniqueString]

**us-west-2:**
- **Destination:** maximus-federal-s3-logs-417886991978-us-west-2
- **Prefix:** macp-dos-prod/cloudfront-content
- **Log Format:** macp-dos-prod/cloudfront-content[YYYY]-[MM]-[DD]-[hh]-[mm]-[ss]-[UniqueString]

**What Gets Logged:**
- Requester identity
- Bucket name
- Request time
- Request action (GET, PUT, DELETE, etc.)
- Response status
- Error codes (if any)
- Bytes sent/received
- Object key accessed

### 6. OwnershipControls
```yaml
OwnershipControls:
  Rules:
    - ObjectOwnership: BucketOwnerEnforced
```
- **Setting:** BucketOwnerEnforced (matches existing bucket)
- **Effect:** Bucket owner automatically owns all objects
- **ACLs:** Disabled for objects
- **Simplifies:** Permission management

---

## Tags (15 Total)

```yaml
Tags:
  - Key: Name
    Value: macp-prod-us-east-1-cloudfront-content (or us-west-2)
    
  - Key: group_nse
    Value: macp-dos-prod
    
  - Key: provisioner
    Value: cloudformation
    
  - Key: role
    Value: data-store
    
  - Key: Backup
    Value: No
    
  - Key: group_ns
    Value: macp-dos
    
  - Key: spf:environment
    Value: prod
    
  - Key: ChargeCode
    Value: tbd
    
  - Key: Project
    Value: macp-dos-prod
    
  - Key: group_n
    Value: macp
    
  - Key: DataClassification
    Value: Proprietary
    
  - Key: Environment
    Value: PROD
    
  - Key: TechnicalPointOfContact
    Value: timothyconners@maximus.com
    
  - Key: BusinessOwner
    Value: toddgriffith@maximus.com
    
  - Key: AccountName
    Value: MACP-DOS
```

**Tag Purposes:**
- **Name:** Bucket identifier
- **group_nse/group_ns/group_n:** Organizational hierarchy
- **provisioner:** Infrastructure management tool
- **role:** Functional purpose
- **Backup:** Backup policy indicator
- **spf:environment/Environment:** Environment designation
- **ChargeCode:** Cost allocation (TBD pending assignment)
- **Project:** Project association
- **DataClassification:** Data sensitivity level
- **TechnicalPointOfContact:** Technical escalation contact
- **BusinessOwner:** Business stakeholder
- **AccountName:** AWS account designation

---

## Bucket Policy (2 Statements)

### Statement 1: Deny Non-HTTPS Traffic
```yaml
Sid: s3DenyNotEncryptedAPI
Effect: Deny
Principal: "*"
Action: "s3:*"
Resource: !Sub '${ContentBucket.Arn}/*'
Condition:
  Bool:
    'aws:SecureTransport': 'false'
```
- **Purpose:** Enforce encryption in transit
- **Effect:** Blocks all S3 operations over HTTP
- **Applies To:** All principals (users, services, roles)
- **Exceptions:** None - HTTPS is mandatory

### Statement 2: Allow CloudFront Service Principal
```yaml
Sid: AllowCloudFrontServicePrincipal
Effect: Allow
Principal:
  Service: cloudfront.amazonaws.com
Action: 's3:GetObject'
Resource: !Sub '${ContentBucket.Arn}/*'
Condition:
  ArnLike:
    'AWS:SourceArn': !Sub 'arn:aws:cloudfront::${AWS::AccountId}:distribution/${MultiServiceDistribution}'
```
- **Purpose:** Grant CloudFront access via Origin Access Control (OAC)
- **Action:** s3:GetObject only (read-only)
- **Principal:** CloudFront service only
- **Condition:** Must be from the specific CloudFront distribution created in this stack
- **Security:** Prevents direct S3 access; forces traffic through CloudFront

---

## Properties NOT Configured

The following S3 features are **NOT** configured (matching existing bucket):

### ❌ Lifecycle Configuration
- **Not configured**
- No automatic object expiration
- No transition to Glacier/Deep Archive
- Objects remain in Standard storage class

### ❌ Cross-Region Replication
- **Not configured**
- No automatic replication to other regions
- Content must be manually uploaded to both buckets

### ❌ CORS Configuration
- **Not configured**
- Cross-origin requests not allowed
- Not needed (CloudFront handles CORS)

### ❌ Website Hosting
- **Not configured**
- Not used as static website
- CloudFront serves the content

### ❌ Bucket Notifications
- **Not configured**
- No Lambda/SQS/SNS triggers
- No event notifications on object operations

### ❌ Object Lock
- **Not configured**
- No immutability/retention policies
- Objects can be deleted/overwritten (if versioned, version preserved)

### ❌ Intelligent Tiering
- **Not configured**
- Objects stay in Standard storage class
- No automatic tiering optimization

### ❌ Requester Pays
- **Not configured**
- Bucket owner pays for storage and transfer

### ❌ Transfer Acceleration
- **Not configured**
- Standard S3 endpoints used

---

## Content Structure

Expected directory structure within buckets:

```
s3://macp-prod-us-east-1-cloudfront-content/
├── admin/
│   └── index.html          # Admin redirect page
├── agent/                  # Future: CCP/Agent workspace
│   └── (to be deployed)
└── chat/                   # Future: Chat widget assets
    └── (to be deployed)
```

**Initial Deployment:**
- Only `/admin/index.html` will be uploaded
- Other paths reserved for future use

---

## CloudFront Integration

### Origin Configuration
- **Origin Type:** S3 bucket (not static website)
- **Origin Path:** Varies by subdomain:
  - AdminS3Origin → `/admin`
  - AgentS3Origin → `/agent`
  - ChatS3Origin → `/chat`
- **Access Method:** Origin Access Control (OAC)
- **Protocol:** HTTPS only

### CloudFront Logging (Separate from S3 Logging)
- **Destination:** maximus-cloudfront-logs-417886991978-us-east-1
- **Prefix:** `prod-us-east-1-admin-portal/` (or us-west-2)
- **Logs:** CloudFront edge requests, not S3 access

---

## Security Summary

| Feature | Status | Purpose |
|---------|--------|---------|
| Encryption at Rest | ✅ Enabled (AES256) | Protect data on disk |
| Encryption in Transit | ✅ Enforced (HTTPS only) | Protect data in transit |
| Public Access | ❌ Blocked (all 4 settings) | Prevent unauthorized access |
| Versioning | ✅ Enabled | Protect against accidental deletion |
| Server Access Logging | ✅ Enabled | Audit trail for compliance |
| CloudFront OAC | ✅ Configured | Restrict access to CloudFront only |
| Bucket Policy | ✅ Restrictive | Deny non-HTTPS, allow CloudFront only |

---

## Operational Considerations

### Storage Costs
- **Storage Class:** S3 Standard
- **Versioning:** Enabled (increases storage costs)
- **Estimated Size:** ~10-50 MB for static content
- **Monthly Cost:** ~$0.10-1.00 per bucket

### Access Patterns
- **Read-Heavy:** Content served via CloudFront (cached)
- **Write Pattern:** Manual uploads during deployments
- **Expected Traffic:** Low direct S3 access (CloudFront handles requests)

### Monitoring
- **CloudWatch Metrics:** Automatically enabled for S3
- **Server Access Logs:** Stored in separate logging bucket
- **CloudTrail:** API calls logged automatically (account-level)

### Backup & Recovery
- **Versioning:** Enabled (automatic version history)
- **Backup Tag:** "No" (no automated backup solution)
- **Recovery:** Use S3 versioning to restore previous versions

---

## Compliance & Governance

### Data Classification
- **Level:** Proprietary
- **Implication:** Internal use, not public data
- **Access Control:** Restricted to authorized services

### Regulatory Considerations
- **Encryption:** Required for proprietary data
- **Access Logging:** Enabled for audit compliance
- **Public Access:** Blocked per security policy

---

## Deployment Impact

### What Happens on Stack Creation
1. S3 bucket created with name: `macp-prod-{region}-cloudfront-content`
2. Encryption automatically applied to all new objects
3. Versioning enabled (all uploads create version IDs)
4. Public access blocked at bucket level
5. Server access logging starts immediately
6. Bucket policy prevents non-HTTPS access
7. CloudFront OAC policy allows distribution access
8. All tags applied to bucket

### Post-Deployment Steps Required
1. Upload content to `/admin/index.html`
2. Verify CloudFront can access content
3. Test admin portal redirect functionality
4. Upload additional content to `/agent` and `/chat` paths as needed

---

## Comparison to Existing Bucket

| Property | Existing Bucket | New Template | Match? |
|----------|----------------|--------------|--------|
| Encryption | AES256, BucketKey=false | AES256, BucketKey=false | ✅ |
| Versioning | Enabled | Enabled | ✅ |
| Public Access Block | All 4 enabled | All 4 enabled | ✅ |
| Ownership Controls | BucketOwnerEnforced | BucketOwnerEnforced | ✅ |
| Server Access Logging | Yes (prefix: cloudfront-content-data-store) | Yes (prefix: cloudfront-content) | ⚠️ Different prefix |
| Bucket Policy | 2 statements | 2 statements | ✅ |
| Lifecycle | None | None | ✅ |
| Replication | None | None | ✅ |
| CORS | None | None | ✅ |
| Website | None | None | ✅ |
| Tags | 15 tags | 15 tags (provisioner=cloudformation) | ✅ |

**Only Difference:** Server access log prefix (simplified for new buckets)

---

## Template Parameters Affecting Bucket

| Parameter | Default | Effect on Bucket |
|-----------|---------|------------------|
| Environment | prod | Included in bucket name |
| Region | us-east-1 | Included in bucket name, selects log destination |
| AWS::AccountId | (auto) | Used in bucket policy CloudFront ARN |
| MultiServiceDistribution | (created) | Used in bucket policy condition |

---

## Questions to Confirm

1. ✅ **Bucket naming convention acceptable?** `macp-prod-us-east-1-cloudfront-content`
2. ✅ **Log prefix change acceptable?** `macp-dos-prod/cloudfront-content` (vs old: `cloudfront-content-data-store`)
3. ✅ **No lifecycle rules needed?** Objects never expire/transition
4. ✅ **No cross-region replication needed?** Manual uploads to both regions
5. ✅ **Versioning enabled acceptable?** Will increase storage costs slightly
6. ✅ **All tags correct?** 15 tags applied to each bucket

---

## Ready to Deploy?

Template creates buckets that match existing configuration with:
- Same security posture
- Same encryption settings
- Same versioning
- Same public access blocks
- Simplified log prefix
- CloudFormation management tag
