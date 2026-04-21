import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import * as path from 'path';

export interface ChatApiStackProps extends cdk.StackProps {
  environment: string;
  certificateArn: string;
  hostedZoneId: string;
  hostedZoneName: string;
  customDomainPrefix: string;  // 'east-api' or 'west-api'
}

export class ChatApiStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly chatFunction: lambda.Function;
  public readonly customDomainName: string;

  constructor(scope: Construct, id: string, props: ChatApiStackProps) {
    super(scope, id, props);

    const { environment, certificateArn, hostedZoneId, hostedZoneName, customDomainPrefix } = props;
    const region = this.region;
    this.customDomainName = `${customDomainPrefix}.${hostedZoneName}`;

    // Lambda function for Chat API
    this.chatFunction = new lambda.Function(this, 'ChatFunction', {
      functionName: `macp-dr-${environment}-chat-api-${region}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/chat-api')),
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      logRetention: logs.RetentionDays.ONE_WEEK,
      description: `Chat API Lambda - ${region}`,
    });

    // API Gateway
    this.api = new apigateway.RestApi(this, 'ChatApi', {
      restApiName: `macp-dr-${environment}-chat-api`,
      description: `Chat API for MACP DR - ${region}`,
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: {
        stageName: 'prod',
        throttlingBurstLimit: 100,
        throttlingRateLimit: 50,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: ['GET', 'OPTIONS'],
        allowHeaders: ['Content-Type'],
      },
    });

    // Lambda integration
    const chatIntegration = new apigateway.LambdaIntegration(this.chatFunction, {
      requestTemplates: { 'application/json': '{ "statusCode": "200" }' },
    });

    // GET /chat endpoint
    const chatResource = this.api.root.addResource('chat');
    chatResource.addMethod('GET', chatIntegration);

    // GET /health endpoint for health checks
    const healthResource = this.api.root.addResource('health');
    healthResource.addMethod('GET', chatIntegration);

    // Root endpoint
    this.api.root.addMethod('GET', chatIntegration);

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
      recordName: customDomainPrefix,
      target: route53.RecordTarget.fromAlias(new targets.ApiGatewayDomain(domainName)),
      comment: `Chat API - ${region}`,
    });

    // ==========================================================================
    // Outputs
    // ==========================================================================
    
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: this.api.url,
      description: 'Chat API URL (default)',
    });

    new cdk.CfnOutput(this, 'CustomDomainUrl', {
      value: `https://${this.customDomainName}/`,
      description: 'Chat API Custom Domain URL',
    });

    new cdk.CfnOutput(this, 'ApiEndpoint', {
      value: `${this.api.restApiId}.execute-api.${region}.amazonaws.com`,
      description: 'API Gateway endpoint',
    });

    new cdk.CfnOutput(this, 'LambdaArn', {
      value: this.chatFunction.functionArn,
      description: 'Chat Lambda ARN',
    });
  }
}
