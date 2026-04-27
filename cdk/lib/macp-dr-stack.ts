import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import { Construct } from 'constructs';
import * as path from 'path';

export interface MacpDrStackProps extends cdk.StackProps {
  environment: string;
  certificateArn: string;
  webAclArn: string;
  subdomains: string[];
  loggingBucket: string;
  s3LoggingBucket: string;
  hostedZoneId: string;
  hostedZoneName: string;
}

export class MacpDrStack extends cdk.Stack {
  public readonly distribution: cloudfront.Distribution;
  public readonly primaryBucket: s3.Bucket;
  public readonly failoverTable: dynamodb.TableV2;

  constructor(scope: Construct, id: string, props: MacpDrStackProps) {
    super(scope, id, props);

    const { environment, certificateArn, webAclArn, subdomains, loggingBucket, s3LoggingBucket, hostedZoneId, hostedZoneName } = props;

    // ==========================================================================
    // DynamoDB Global Table for failover control signal
    // ==========================================================================
    this.failoverTable = new dynamodb.TableV2(this, 'FailoverStateTable', {
      tableName: `macp-dr-${environment}-failover-state`,
      partitionKey: { name: 'config_key', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      dynamoStream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      // Global Table replicas
      replicas: [
        { region: 'us-west-2' }
      ],
    });

    // ==========================================================================
    // S3 Bucket - Primary (us-east-1)
    // ==========================================================================
    
    // DR bucket name (created by DrBucketStack in us-west-2)
    const drBucketName = `macp-dr-opt7-content-${environment}-us-west-2`;
    
    // Import the S3 logging bucket
    const serverAccessLogsBucket = s3.Bucket.fromBucketName(this, 'S3LogsBucket', s3LoggingBucket);
    
    this.primaryBucket = new s3.Bucket(this, 'PrimaryContentBucket', {
      bucketName: `macp-dr-opt7-content-${environment}-us-east-1`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      serverAccessLogsBucket: serverAccessLogsBucket,
      serverAccessLogsPrefix: `macp-dr-opt7-content-${environment}-us-east-1/`,
    });
    
    // Import DR bucket (created in us-west-2 by DrBucketStack)
    const drBucket = s3.Bucket.fromBucketAttributes(this, 'DRBucket', {
      bucketName: drBucketName,
      region: 'us-west-2',
    });

    // ==========================================================================
    // Lambda@Edge for origin routing
    // ==========================================================================
    
    // IAM role for Lambda@Edge
    const originRouterRole = new iam.Role(this, 'OriginRouterRole', {
      roleName: `macp-dr-${environment}-origin-router-role`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('lambda.amazonaws.com'),
        new iam.ServicePrincipal('edgelambda.amazonaws.com')
      ),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')
      ],
    });

    // Grant DynamoDB read access in both regions
    originRouterRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem'],
      resources: [
        `arn:aws:dynamodb:us-east-1:${this.account}:table/macp-dr-${environment}-failover-state`,
        `arn:aws:dynamodb:us-west-2:${this.account}:table/macp-dr-${environment}-failover-state`,
      ],
    }));

    // Grant S3 read access for both buckets (Lambda signs requests using SigV4)
    originRouterRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: [
        `arn:aws:s3:::macp-dr-opt7-content-${environment}-us-east-1/*`,
        `arn:aws:s3:::macp-dr-opt7-content-${environment}-us-west-2/*`,
      ],
    }));

    // Lambda@Edge function (x86_64 required - Lambda@Edge doesn't support ARM64)
    const originRouter = new cloudfront.experimental.EdgeFunction(this, 'OriginRouter', {
      functionName: `macp-dr-${environment}-origin-router`,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: 'origin_router.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda')),
      role: originRouterRole,
      timeout: cdk.Duration.seconds(5),
      memorySize: 128,
    });

    // ==========================================================================
    // CloudFront Distribution with Origin Group
    // ==========================================================================
    
    // Import certificate
    const certificate = acm.Certificate.fromCertificateArn(this, 'Certificate', certificateArn);

    // CloudFront Function to pass original Host header to Lambda@Edge
    // CloudFront Functions run at viewer-request and can set custom headers
    const hostPassthroughFunction = new cloudfront.Function(this, 'HostPassthrough', {
      functionName: `macp-dr-${environment}-host-passthrough`,
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var request = event.request;
  // Copy the original Host header to a custom header for Lambda@Edge
  request.headers['x-original-host'] = { value: request.headers.host.value };
  return request;
}
      `),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      comment: 'Passes original Host header to Lambda@Edge for subdomain routing',
    });

    // S3 Origin - Lambda@Edge handles routing and SigV4 signing
    // We define a single origin for CloudFront, but Lambda overrides it based on DynamoDB
    const primaryOrigin = new origins.S3Origin(this.primaryBucket, {
      originAccessIdentity: undefined, // No OAI/OAC - Lambda signs requests
    });

    // Note: No Origin Group - manual failover only via DynamoDB
    // Lambda@Edge reads active_region and routes to the appropriate S3 bucket

    // Custom cache policy that includes x-original-host header in cache key
    // This ensures each subdomain has its own cache entries
    const cachePolicy = new cloudfront.CachePolicy(this, 'CachePolicy', {
      cachePolicyName: `macp-dr-${environment}-cache-policy`,
      defaultTtl: cdk.Duration.days(1),
      maxTtl: cdk.Duration.days(365),
      minTtl: cdk.Duration.seconds(0),
      headerBehavior: cloudfront.CacheHeaderBehavior.allowList('x-original-host'),
      queryStringBehavior: cloudfront.CacheQueryStringBehavior.none(),
      cookieBehavior: cloudfront.CacheCookieBehavior.none(),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
    });

    // Default behavior with Lambda@Edge and CloudFront Function
    const defaultBehavior: cloudfront.BehaviorOptions = {
      origin: primaryOrigin,  // Lambda@Edge overrides this based on DynamoDB
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
      cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
      compress: true,
      cachePolicy: cachePolicy,
      // Origin request policy to forward headers to Lambda@Edge
      originRequestPolicy: new cloudfront.OriginRequestPolicy(this, 'OriginRequestPolicy', {
        originRequestPolicyName: `macp-dr-${environment}-origin-request-policy`,
        headerBehavior: cloudfront.OriginRequestHeaderBehavior.allowList(
          'x-original-host',
          'CloudFront-Viewer-Country',
          'CloudFront-Viewer-City'
        ),
      }),
      functionAssociations: [{
        function: hostPassthroughFunction,
        eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
      }],
      edgeLambdas: [{
        eventType: cloudfront.LambdaEdgeEventType.ORIGIN_REQUEST,
        functionVersion: originRouter.currentVersion,
        includeBody: false,
      }],
    };

    // Distribution
    // Note: defaultRootObject removed - Lambda@Edge handles index.html for subdomain routing
    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `MACP DR Distribution - ${environment}`,
      domainNames: subdomains,
      certificate: certificate,
      defaultBehavior: defaultBehavior,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      webAclId: webAclArn,
      enableLogging: true,
      logBucket: s3.Bucket.fromBucketName(this, 'LogBucket', loggingBucket),
      logFilePrefix: `macp-dr/cloudfront/`,
      geoRestriction: cloudfront.GeoRestriction.allowlist('US'),
      errorResponses: [
        { httpStatus: 403, ttl: cdk.Duration.seconds(0) },
        { httpStatus: 500, ttl: cdk.Duration.seconds(0) },
        { httpStatus: 502, ttl: cdk.Duration.seconds(0) },
        { httpStatus: 503, ttl: cdk.Duration.seconds(0) },
      ],
    });

    // Note: Path-based behaviors (/admin/*, /agent/*, /chat/*) are NOT needed
    // because Lambda@Edge handles subdomain→folder routing on all requests.
    // The default behavior handles everything.

    // ==========================================================================
    // Route53 DNS Records
    // ==========================================================================
    
    // Import the existing public hosted zone
    const hostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'HostedZone', {
      hostedZoneId: hostedZoneId,
      zoneName: hostedZoneName,
    });

    // Create A (Alias) records for each subdomain pointing to CloudFront
    for (const subdomain of subdomains) {
      const recordName = subdomain.replace(`.${hostedZoneName}`, '');
      new route53.ARecord(this, `ARecord-${recordName}`, {
        zone: hostedZone,
        recordName: recordName,
        target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(this.distribution)),
        comment: `MACP DR - ${subdomain}`,
      });
    }

    // ==========================================================================
    // Outputs
    // ==========================================================================
    
    new cdk.CfnOutput(this, 'DistributionId', {
      value: this.distribution.distributionId,
      description: 'CloudFront Distribution ID',
    });

    new cdk.CfnOutput(this, 'DistributionDomainName', {
      value: this.distribution.distributionDomainName,
      description: 'CloudFront Distribution Domain Name',
    });

    new cdk.CfnOutput(this, 'FailoverTableName', {
      value: this.failoverTable.tableName,
      description: 'DynamoDB Failover State Table Name',
    });

    new cdk.CfnOutput(this, 'PrimaryBucketName', {
      value: this.primaryBucket.bucketName,
      description: 'Primary S3 Bucket Name',
    });

    new cdk.CfnOutput(this, 'DRBucketName', {
      value: drBucketName,
      description: 'DR S3 Bucket Name',
    });

    subdomains.forEach((subdomain, index) => {
      new cdk.CfnOutput(this, `URL${index}`, {
        value: `https://${subdomain}`,
        description: `Subdomain URL: ${subdomain}`,
      });
    });
  }
}
