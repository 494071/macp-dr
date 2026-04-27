import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';
import * as path from 'path';

export interface FailoverApiStackProps extends cdk.StackProps {
  environment: string;
  certificateArn: string;
  hostedZoneId: string;
  hostedZoneName: string;
  failoverTableName: string;
  distributionId: string;
}

export class FailoverApiStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly failoverFunction: lambda.Function;
  public readonly customDomainName: string;
  public readonly apiKey: apigateway.ApiKey;

  constructor(scope: Construct, id: string, props: FailoverApiStackProps) {
    super(scope, id, props);

    const { environment, certificateArn, hostedZoneId, hostedZoneName, failoverTableName, distributionId } = props;
    const region = this.region;
    this.customDomainName = `failover-api.${hostedZoneName}`;

    // Lambda function for Failover API
    this.failoverFunction = new lambda.Function(this, 'FailoverFunction', {
      functionName: `macp-dr-${environment}-failover-api`,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/failover-api')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      logRetention: logs.RetentionDays.ONE_WEEK,
      description: 'Failover API Lambda - handles region failover requests',
      environment: {
        TABLE_NAME: failoverTableName,
        DISTRIBUTION_ID: distributionId,
      },
    });

    // Grant Lambda permission to write to DynamoDB
    const failoverTable = dynamodb.Table.fromTableName(this, 'FailoverTable', failoverTableName);
    failoverTable.grantReadWriteData(this.failoverFunction);

    // Grant Lambda permission to create CloudFront invalidations
    this.failoverFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cloudfront:CreateInvalidation'],
      resources: [`arn:aws:cloudfront::${this.account}:distribution/${distributionId}`],
    }));

    // API Gateway with API Key requirement
    this.api = new apigateway.RestApi(this, 'FailoverApi', {
      restApiName: `macp-dr-${environment}-failover-api`,
      description: 'Failover API for MACP DR - triggers region failover',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: {
        stageName: 'prod',
        throttlingBurstLimit: 10,
        throttlingRateLimit: 5,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: ['POST', 'OPTIONS'],
        allowHeaders: ['Content-Type', 'x-api-key'],
      },
    });

    // WAF tag for Firewall Manager compliance
    cdk.Tags.of(this.api).add('mms:waf-rules', 'usonlyfortinet');

    // Create API Key
    this.apiKey = new apigateway.ApiKey(this, 'FailoverApiKey', {
      apiKeyName: `macp-dr-${environment}-failover-key`,
      description: 'API key for failover portal',
      enabled: true,
    });

    // Create Usage Plan and associate API Key
    const usagePlan = new apigateway.UsagePlan(this, 'FailoverUsagePlan', {
      name: `macp-dr-${environment}-failover-plan`,
      description: 'Usage plan for failover API',
      throttle: {
        rateLimit: 5,
        burstLimit: 10,
      },
      quota: {
        limit: 100,
        period: apigateway.Period.DAY,
      },
      apiStages: [{
        api: this.api,
        stage: this.api.deploymentStage,
      }],
    });

    usagePlan.addApiKey(this.apiKey);

    // Lambda integration
    const failoverIntegration = new apigateway.LambdaIntegration(this.failoverFunction);

    // POST /failover endpoint (requires API key)
    const failoverResource = this.api.root.addResource('failover');
    failoverResource.addMethod('POST', failoverIntegration, {
      apiKeyRequired: true,
    });

    // POST /invalidate endpoint (requires API key)
    const invalidateResource = this.api.root.addResource('invalidate');
    invalidateResource.addMethod('POST', failoverIntegration, {
      apiKeyRequired: true,
    });

    // GET /status endpoint (no API key required - same as health endpoint)
    const statusResource = this.api.root.addResource('status');
    statusResource.addMethod('GET', failoverIntegration, {
      apiKeyRequired: false,
    });

    // ==========================================================================
    // Custom Domain
    // ==========================================================================
    
    // Import certificate (must be in same region for regional API Gateway)
    const certificate = acm.Certificate.fromCertificateArn(this, 'Certificate', certificateArn);

    // Create custom domain for API Gateway
    const domainName = new apigateway.DomainName(this, 'CustomDomain', {
      domainName: this.customDomainName,
      certificate: certificate,
      endpointType: apigateway.EndpointType.REGIONAL,
      securityPolicy: apigateway.SecurityPolicy.TLS_1_2,
    });

    // Map custom domain to API
    new apigateway.BasePathMapping(this, 'BasePathMapping', {
      domainName: domainName,
      restApi: this.api,
      stage: this.api.deploymentStage,
    });

    // ==========================================================================
    // Route 53 DNS Record
    // ==========================================================================
    
    // Import hosted zone
    const hostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'HostedZone', {
      hostedZoneId: hostedZoneId,
      zoneName: hostedZoneName,
    });

    // Create A record for custom domain
    new route53.ARecord(this, 'ApiARecord', {
      zone: hostedZone,
      recordName: 'failover-api',
      target: route53.RecordTarget.fromAlias(new targets.ApiGatewayDomain(domainName)),
      comment: 'Failover API',
    });

    // ==========================================================================
    // Outputs
    // ==========================================================================
    
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: this.api.url,
      description: 'Failover API URL (default)',
    });

    new cdk.CfnOutput(this, 'CustomDomainUrl', {
      value: `https://${this.customDomainName}/`,
      description: 'Failover API Custom Domain URL',
    });

    new cdk.CfnOutput(this, 'ApiKeyId', {
      value: this.apiKey.keyId,
      description: 'API Key ID (use AWS CLI to retrieve actual key value)',
    });

    new cdk.CfnOutput(this, 'LambdaArn', {
      value: this.failoverFunction.functionArn,
      description: 'Failover Lambda ARN',
    });
  }
}
