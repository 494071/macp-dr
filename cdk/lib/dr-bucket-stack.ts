import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface DrBucketStackProps extends cdk.StackProps {
  environment: string;
  s3LoggingBucket: string;
}

/**
 * DR Bucket Stack - deployed to us-west-2
 * 
 * This stack creates the DR S3 bucket that receives replicated content
 * from the primary bucket in us-east-1.
 */
export class DrBucketStack extends cdk.Stack {
  public readonly drBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: DrBucketStackProps) {
    super(scope, id, props);

    const { environment, s3LoggingBucket } = props;

    // Import the S3 logging bucket
    const serverAccessLogsBucket = s3.Bucket.fromBucketName(this, 'S3LogsBucket', s3LoggingBucket);

    // DR S3 Bucket (replication destination)
    this.drBucket = new s3.Bucket(this, 'DrContentBucket', {
      bucketName: `macp-dr-opt7-content-${environment}-us-west-2`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,  // Required for CRR destination
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      serverAccessLogsBucket: serverAccessLogsBucket,
      serverAccessLogsPrefix: `macp-dr-opt7-content-${environment}-us-west-2/`,
    });

    // ==========================================================================
    // Outputs
    // ==========================================================================
    
    new cdk.CfnOutput(this, 'DrBucketName', {
      value: this.drBucket.bucketName,
      description: 'DR S3 Bucket Name',
      exportName: `Option7-DrBucketName-${environment}`,
    });

    new cdk.CfnOutput(this, 'DrBucketArn', {
      value: this.drBucket.bucketArn,
      description: 'DR S3 Bucket ARN',
      exportName: `Option7-DrBucketArn-${environment}`,
    });

    new cdk.CfnOutput(this, 'DrBucketDomainName', {
      value: this.drBucket.bucketRegionalDomainName,
      description: 'DR S3 Bucket Regional Domain Name',
      exportName: `Option7-DrBucketDomain-${environment}`,
    });
  }
}
