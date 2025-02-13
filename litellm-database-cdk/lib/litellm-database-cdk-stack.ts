import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as elasticache from 'aws-cdk-lib/aws-elasticache';
import * as logs from 'aws-cdk-lib/aws-logs';

interface LiteLLMStackProps extends cdk.StackProps {
  vpcId: string;
}

export class LitellmDatabaseCdkStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: LiteLLMStackProps) {
    super(scope, id, props);

    // Create VPC or import one if provided
    const vpc = props.vpcId ?
      ec2.Vpc.fromLookup(this, 'LiteLLMVpc', { vpcId: props.vpcId }) :
      new ec2.Vpc(this, 'LiteLLMVpc', { maxAzs: 2, natGateways: 1, flowLogs: {
        'flowlog1': {
          destination: ec2.FlowLogDestination.toCloudWatchLogs(
            new logs.LogGroup(this, 'VPCFlowLogs', {
              retention: logs.RetentionDays.ONE_MONTH,
            })
          ),
          trafficType: ec2.FlowLogTrafficType.ALL,
          maxAggregationInterval: ec2.FlowLogMaxAggregationInterval.ONE_MINUTE,
        }
      }
     });

   // ------------------------------------------------------------------------
    // --- VPC Endpoints ------------------------------------------------------
    // ------------------------------------------------------------------------
    // Create a security group for Interface Endpoints if you want to control ingress
    // For many use cases, allowing inbound on port 443 from inside the VPC is sufficient.
    const vpcEndpointSG = new ec2.SecurityGroup(this, 'VPCEndpointsSG', {
      vpc,
      description: 'Security group for Interface VPC Endpoints',
      allowAllOutbound: true,
    });
    vpcEndpointSG.addIngressRule(ec2.Peer.ipv4(vpc.vpcCidrBlock), ec2.Port.tcp(443));

    vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
    });

    vpc.addInterfaceEndpoint('SecretsManagerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
      subnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    vpc.addInterfaceEndpoint('ECREndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR,
      subnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    vpc.addInterfaceEndpoint('ECRDockerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
      subnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    // CloudWatch Logs - used by ECS tasks, Flow Logs, etc.
    vpc.addInterfaceEndpoint('CloudWatchLogsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
      subnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    // STS (Interface) - used by ECS tasks to assume roles
    vpc.addInterfaceEndpoint('STSEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.STS,
      subnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    vpc.addInterfaceEndpoint('SageMakerRuntimeEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME,
      subnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    vpc.addInterfaceEndpoint('BedrockEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.BEDROCK,
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    vpc.addInterfaceEndpoint('BedrockRuntimeEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    //Bedrock Agent - Used by middleware to get bedrock managed prompts
    vpc.addInterfaceEndpoint('BedrockAgentEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENT,
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [vpcEndpointSG],
      lookupSupportedAzs: true
    });

    // Create RDS Instance
    const databaseSecret = new secretsmanager.Secret(this, 'DBSecret', {
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          username: 'llmproxy',
        }),
        generateStringKey: 'password',
        excludePunctuation: true,
      },
    });

    const databaseMiddlewareSecret = new secretsmanager.Secret(this, 'DBMiddlewareSecret', {
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          username: 'middleware',
        }),
        generateStringKey: 'password',
        excludePunctuation: true,
      },
    });

    const dbSecurityGroup = new ec2.SecurityGroup(this, 'DBSecurityGroup', {
      vpc,
      description: 'Security group for RDS instance',
      allowAllOutbound: true,
    });

    const database = new rds.DatabaseInstance(this, 'Database', {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_15,
      }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
      vpc,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [dbSecurityGroup],
      credentials: rds.Credentials.fromSecret(databaseSecret),
      databaseName: 'litellm',
      storageType: rds.StorageType.GP3,
      storageEncrypted: true,
    });

    const databaseMiddleware = new rds.DatabaseInstance(this, 'DatabaseMiddleware', {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_15,
      }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
      vpc,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [dbSecurityGroup],
      credentials: rds.Credentials.fromSecret(databaseMiddlewareSecret),
      databaseName: 'middleware',
      storageType: rds.StorageType.GP3,
      storageEncrypted: true,
    });

    const redisSecurityGroup = new ec2.SecurityGroup(this, 'RedisSecurityGroup', {
      vpc,
      description: 'Security group for Redis cluster',
      allowAllOutbound: true,
    });

    // Create Redis Subnet Group
    const redisSubnetGroup = new elasticache.CfnSubnetGroup(this, 'RedisSubnetGroup', {
      description: 'Subnet group for Redis cluster',
      subnetIds: vpc.privateSubnets.map(subnet => subnet.subnetId),
      cacheSubnetGroupName: 'litellm-redis-subnet-group',
    });

    const redisParameterGroup = new elasticache.CfnParameterGroup(this, 'RedisParameterGroup', {
      cacheParameterGroupFamily: 'redis7',
      description: 'Redis parameter group',
    });

    // Create Redis Cluster
    const redis = new elasticache.CfnReplicationGroup(this, 'RedisCluster', {
      replicationGroupDescription: 'Redis cluster',
      engine: 'redis',
      cacheNodeType: 'cache.t3.micro',
      numCacheClusters: 2,
      automaticFailoverEnabled: true,
      cacheParameterGroupName: redisParameterGroup.ref,
      cacheSubnetGroupName: redisSubnetGroup.ref,
      securityGroupIds: [redisSecurityGroup.securityGroupId],
      engineVersion: '7.1',
      port: 6379,
      atRestEncryptionEnabled: true
    });

    // Make sure the subnet group is created before the cluster
    redis.addDependency(redisSubnetGroup);
    redis.addDependency(redisParameterGroup);

    // Add outputs
    new cdk.CfnOutput(this, 'RdsLitellmHostname', {
      value: database.instanceEndpoint.hostname,
      description: 'The hostname of the LiteLLM RDS instance',
    });

    new cdk.CfnOutput(this, 'RdsLitellmSecretArn', {
      value: databaseSecret.secretArn,
      description: 'The ARN of the LiteLLM RDS secret',
    });

    new cdk.CfnOutput(this, 'RdsMiddlewareHostname', {
      value: databaseMiddleware.instanceEndpoint.hostname,
      description: 'The hostname of the Middleware RDS instance',
    });

    new cdk.CfnOutput(this, 'RdsMiddlewareSecretArn', {
      value: databaseMiddlewareSecret.secretArn,
      description: 'The ARN of the Middleware RDS secret',
    });

    new cdk.CfnOutput(this, 'RedisHostName', {
      value: redis.attrPrimaryEndPointAddress,
      description: 'The hostname of the Redis cluster',
    });

    new cdk.CfnOutput(this, 'RedisPort', {
      value: redis.attrPrimaryEndPointPort,
      description: 'The port of the Redis cluster',
    });

    new cdk.CfnOutput(this, 'RdsSecurityGroupId', {
      value: dbSecurityGroup.securityGroupId,
      description: 'The ID of the RDS security group',
    });

    new cdk.CfnOutput(this, 'RedisSecurityGroupId', {
      value: redisSecurityGroup.securityGroupId,
      description: 'The ID of the Redis security group',
    });

    new cdk.CfnOutput(this, 'VpcId', {
      value: vpc.vpcId,
      description: 'The ID of the VPC',
    });
  }
}
