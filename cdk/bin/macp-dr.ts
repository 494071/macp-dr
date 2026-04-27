#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { MacpDrStack } from '../lib/macp-dr-stack';
import { DrBucketStack } from '../lib/dr-bucket-stack';
import { ChatApiStack } from '../lib/chat-api-stack';
import { FailoverApiStack } from '../lib/failover-api-stack';

const app = new cdk.App();

// Configuration
const environment = app.node.tryGetContext('environment') || 'prod';
const accountId = '417886991978';

// Standard project tags
const projectTags: { [key: string]: string } = {
  'group_nse': 'macp-dos-prod',
  'provisioner': 'cdk',
  'role': 'dr-infrastructure',
  'Backup': 'No',
  'group_ns': 'macp-dos',
  'spf:environment': 'prod',
  'ChargeCode': 'tbd',
  'Project': 'macp-dos-prod',
  'group_n': 'macp',
  'DataClassification': 'Proprietary',
  'Environment': 'PROD',
  'TechnicalPointOfContact': 'timothyconners@maximus.com',
  'BusinessOwner': 'toddgriffith@maximus.com',
  'AccountName': 'MACP-DOS',
  'mms:waf-rules': 'usonlyfortinet',
};

const config = {
  environment,
  certificateArn: 'arn:aws:acm:us-east-1:417886991978:certificate/e925eda1-90b5-4607-8ad2-c2e924a3bb71',
  webAclArn: 'arn:aws:wafv2:us-east-1:417886991978:global/webacl/FMManagedWebACLV2-mms-waf-acl-US-Only-fortinet-1776365461408/0e665655-35a1-448e-bfaa-e1fc3a90ac70',
  subdomains: [
    'admin.prod.gsa.dos.macp.cloud',
    'agent.prod.gsa.dos.macp.cloud',
    'chat.prod.gsa.dos.macp.cloud',
    'chat-api.prod.gsa.dos.macp.cloud',
    'health.prod.gsa.dos.macp.cloud',
    'portal.prod.gsa.dos.macp.cloud',
  ],
  loggingBucket: 'maximus-cloudfront-logs-417886991978-us-east-1',
  s3LoggingBucketEast: 'maximus-federal-s3-logs-417886991978-us-east-1',
  s3LoggingBucketWest: 'maximus-federal-s3-logs-417886991978-us-west-2',
  hostedZoneId: 'Z10445293PTGB9ZOBN0G8',
  hostedZoneName: 'prod.gsa.dos.macp.cloud',
  distributionId: 'E1KLVY7Q1RG0RK',
  failoverTableName: 'macp-dr-prod-failover-state',
};

// DR Bucket Stack (deploy to us-west-2 first)
const drBucketStack = new DrBucketStack(app, 'Option7DrBucketStack', {
  env: { account: accountId, region: 'us-west-2' },
  environment,
  s3LoggingBucket: config.s3LoggingBucketWest,
  crossRegionReferences: true,
});

// Main DR Stack (deploy to us-east-1)
// Note: Stack ID kept as 'Option7Stack' to match existing deployed stack
const mainStack = new MacpDrStack(app, 'Option7Stack', {
  env: { account: accountId, region: 'us-east-1' },
  ...config,
  s3LoggingBucket: config.s3LoggingBucketEast,
  crossRegionReferences: true,
});

// Chat API Stack - Primary (us-east-1)
const chatApiEast = new ChatApiStack(app, 'ChatApiStackEast', {
  env: { account: accountId, region: 'us-east-1' },
  environment,
  certificateArn: 'arn:aws:acm:us-east-1:417886991978:certificate/e925eda1-90b5-4607-8ad2-c2e924a3bb71',
  hostedZoneId: config.hostedZoneId,
  hostedZoneName: config.hostedZoneName,
  customDomainPrefix: 'east-api',
});

// Chat API Stack - DR (us-west-2)
const chatApiWest = new ChatApiStack(app, 'ChatApiStackWest', {
  env: { account: accountId, region: 'us-west-2' },
  environment,
  certificateArn: 'arn:aws:acm:us-west-2:417886991978:certificate/676a38de-1645-409c-b35e-8ce710f70e72',
  hostedZoneId: config.hostedZoneId,
  hostedZoneName: config.hostedZoneName,
  customDomainPrefix: 'west-api',
});

// Failover API Stack (us-east-1 only - controls both regions)
const failoverApi = new FailoverApiStack(app, 'FailoverApiStack', {
  env: { account: accountId, region: 'us-east-1' },
  environment,
  certificateArn: config.certificateArn,
  hostedZoneId: config.hostedZoneId,
  hostedZoneName: config.hostedZoneName,
  failoverTableName: config.failoverTableName,
  distributionId: config.distributionId,
});

// Apply tags to all stacks
const allStacks = [drBucketStack, mainStack, chatApiEast, chatApiWest, failoverApi];
for (const stack of allStacks) {
  for (const [key, value] of Object.entries(projectTags)) {
    cdk.Tags.of(stack).add(key, value);
  }
}
