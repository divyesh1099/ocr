import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import * as authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';

export interface OcrStackProps extends cdk.StackProps {
  superadminEmail: string;
  alarmEmail: string;
  /** e.g. ocr.dshare.me (DNS is managed in Cloudflare, see scripts/cloudflare_domain.py) */
  domainName: string;
  /** ACM certificate in us-east-1 covering domainName (required for a custom domain) */
  certificateArn: string;
  textractConcurrency: number;
  tesseractConcurrency: number;
  uploadRetentionDays: number;
  resultRetentionDays: number;
}

const ROOT = path.join(__dirname, '..', '..');
const BACKEND_BUILD = path.join(ROOT, 'backend', 'build');
const FRONTEND_DIST = path.join(ROOT, 'frontend', 'dist');

export class OcrStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: OcrStackProps) {
    super(scope, id, props);
    if (!props.superadminEmail) {
      throw new Error('Set the superadmin email: cdk deploy -c superadminEmail=you@example.com');
    }
    const useDomain = Boolean(props.domainName && props.certificateArn);

    // ------------------------------------------------------------------ storage
    const table = new dynamodb.TableV2(this, 'Table', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'ttl',
      globalSecondaryIndexes: [{
        indexName: 'GSI1',
        partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      }],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const webBucket = new s3.Bucket(this, 'WebBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ------------------------------------------------------------------ CDN (site + /api)
    const spaRewrite = new cloudfront.Function(this, 'SpaRewrite', {
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var req = event.request;
  if (!req.uri.includes('.') ) { req.uri = '/index.html'; }
  return req;
}`),
    });

    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      securityHeadersBehavior: {
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: true, override: true },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
      },
    });

    // The API origin is added below (after the API exists); start with the site behaviour.
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'OCR website',
      defaultRootObject: 'index.html',
      domainNames: useDomain ? [props.domainName] : undefined,
      certificate: useDomain ? acm.Certificate.fromCertificateArn(this, 'Cert', props.certificateArn) : undefined,
      minimumProtocolVersion: useDomain ? cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021 : undefined,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_200, // includes India
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(webBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: securityHeaders,
        functionAssociations: [{ function: spaRewrite, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST }],
      },
    });

    const siteOrigins = [
      `https://${distribution.distributionDomainName}`,
      ...(props.domainName ? [`https://${props.domainName}`] : []),
      'http://localhost:5173',
    ];

    const dataBucket = new s3.Bucket(this, 'DataBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      cors: [{
        allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET, s3.HttpMethods.HEAD],
        allowedOrigins: siteOrigins,
        allowedHeaders: ['*'],
        exposedHeaders: ['ETag'],
        maxAge: 3600,
      }],
      lifecycleRules: [
        { id: 'uploads', prefix: 'uploads/', expiration: cdk.Duration.days(props.uploadRetentionDays) },
        { id: 'work', prefix: 'work/', expiration: cdk.Duration.days(3) },
        { id: 'results', prefix: 'results/', expiration: cdk.Duration.days(props.resultRetentionDays) },
        { id: 'abort-mpu', abortIncompleteMultipartUploadAfter: cdk.Duration.days(2) },
      ],
    });

    // ------------------------------------------------------------------ secrets & alarms
    const razorpaySecret = new secretsmanager.Secret(this, 'RazorpaySecret', {
      description: 'Razorpay API keys for the OCR site: {"keyId","keySecret","webhookSecret"}',
      secretObjectValue: {
        keyId: cdk.SecretValue.unsafePlainText('CHANGE_ME'),
        keySecret: cdk.SecretValue.unsafePlainText('CHANGE_ME'),
        webhookSecret: cdk.SecretValue.unsafePlainText('CHANGE_ME'),
      },
    });

    const alarmTopic = new sns.Topic(this, 'AlarmTopic', { displayName: 'OCR alarms' });
    alarmTopic.addSubscription(new subs.EmailSubscription(props.alarmEmail || props.superadminEmail));

    // ------------------------------------------------------------------ queues
    const queue = (name: string, fnTimeout: cdk.Duration, maxReceive = 5) => {
      const dlq = new sqs.Queue(this, `${name}Dlq`, {
        retentionPeriod: cdk.Duration.days(14),
        visibilityTimeout: cdk.Duration.minutes(3), // > DLQ handler timeout
        encryption: sqs.QueueEncryption.SQS_MANAGED,
        enforceSSL: true,
      });
      const q = new sqs.Queue(this, `${name}Queue`, {
        visibilityTimeout: fnTimeout.plus(cdk.Duration.minutes(2)),
        retentionPeriod: cdk.Duration.days(4),
        encryption: sqs.QueueEncryption.SQS_MANAGED,
        enforceSSL: true,
        deadLetterQueue: { queue: dlq, maxReceiveCount: maxReceive },
      });
      new cloudwatch.Alarm(this, `${name}DlqAlarm`, {
        alarmDescription: `Messages landed in the ${name} dead-letter queue`,
        metric: dlq.metricApproximateNumberOfMessagesVisible({ period: cdk.Duration.minutes(5) }),
        threshold: 1,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction(new cwActions.SnsAction(alarmTopic));
      return { q, dlq };
    };
    const LONG = cdk.Duration.minutes(15);
    const ingestQ = queue('Ingest', LONG);
    const processQ = queue('Process', LONG);
    const textractQ = queue('Textract', cdk.Duration.minutes(2), 12); // throttling retries
    const textractDoneQ = queue('TextractDone', LONG);
    const tesseractQ = queue('Tesseract', LONG, 3);
    const assembleQ = queue('Assemble', LONG);
    const finalizeQ = queue('Finalize', LONG);

    // ------------------------------------------------------------------ Textract notifications
    const textractTopic = new sns.Topic(this, 'TextractTopic', { displayName: 'Textract job completions' });
    textractTopic.addSubscription(new subs.SqsSubscription(textractDoneQ.q, { rawMessageDelivery: true }));
    const textractRole = new iam.Role(this, 'TextractPublishRole', {
      assumedBy: new iam.ServicePrincipal('textract.amazonaws.com'),
      description: 'Lets Textract publish async job completions to the OCR SNS topic',
    });
    textractTopic.grantPublish(textractRole);

    // ------------------------------------------------------------------ Cognito
    const commonEnv: Record<string, string> = {
      TABLE_NAME: table.tableName,
      DATA_BUCKET: dataBucket.bucketName,
      SUPERADMIN_EMAIL: props.superadminEmail.toLowerCase(),
      INGEST_QUEUE_URL: ingestQ.q.queueUrl,
      PROCESS_QUEUE_URL: processQ.q.queueUrl,
      TEXTRACT_QUEUE_URL: textractQ.q.queueUrl,
      TESSERACT_QUEUE_URL: tesseractQ.q.queueUrl,
      ASSEMBLE_QUEUE_URL: assembleQ.q.queueUrl,
      FINALIZE_QUEUE_URL: finalizeQ.q.queueUrl,
      TEXTRACT_SNS_TOPIC_ARN: textractTopic.topicArn,
      TEXTRACT_SNS_ROLE_ARN: textractRole.roleArn,
      RAZORPAY_SECRET_ARN: razorpaySecret.secretArn,
      POWERTOOLS_SERVICE_NAME: 'ocr',
    };

    if (!fs.existsSync(path.join(BACKEND_BUILD, 'ocrsvc'))) {
      throw new Error('backend/build is missing: run scripts/build-backend.sh first');
    }
    const code = lambda.Code.fromAsset(BACKEND_BUILD);
    const fn = (name: string, handler: string, opts: Partial<lambda.FunctionProps> = {}) => {
      const f = new lambda.Function(this, name, {
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.ARM_64,
        code,
        handler,
        memorySize: 1024,
        timeout: cdk.Duration.seconds(60),
        environment: commonEnv,
        tracing: lambda.Tracing.ACTIVE,
        logGroup: new logs.LogGroup(this, `${name}Logs`, {
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        ...opts,
      });
      table.grantReadWriteData(f);
      return f;
    };

    const preSignUp = fn('PreSignUpFn', 'ocrsvc.handlers.triggers.pre_sign_up', { memorySize: 512, timeout: cdk.Duration.seconds(5) });
    const postConfirm = fn('PostConfirmationFn', 'ocrsvc.handlers.triggers.post_confirmation', { memorySize: 512, timeout: cdk.Duration.seconds(5) });

    const userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: { email: { required: true, mutable: true } },
      passwordPolicy: { minLength: 10, requireLowercase: true, requireUppercase: false, requireDigits: true, requireSymbols: false },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      userVerification: {
        emailSubject: 'Your OCR verification code',
        emailBody: 'Your verification code is {####}',
        emailStyle: cognito.VerificationEmailStyle.CODE,
      },
      lambdaTriggers: { preSignUp, postConfirmation: postConfirm },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const userPoolClient = userPool.addClient('WebClient', {
      authFlows: { userSrp: true },
      preventUserExistenceErrors: true,
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    // ------------------------------------------------------------------ API
    const apiFn = fn('ApiFn', 'ocrsvc.handlers.api.handler', {
      memorySize: 1024,
      timeout: cdk.Duration.seconds(29),
      environment: { ...commonEnv, USER_POOL_ID: userPool.userPoolId },
    });
    dataBucket.grantReadWrite(apiFn);
    dataBucket.grantDelete(apiFn);
    ingestQ.q.grantSendMessages(apiFn);
    finalizeQ.q.grantSendMessages(apiFn);
    razorpaySecret.grantRead(apiFn);
    apiFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminDisableUser', 'cognito-idp:AdminEnableUser', 'cognito-idp:AdminUserGlobalSignOut'],
      resources: [userPool.userPoolArn],
    }));

    const httpApi = new apigw.HttpApi(this, 'HttpApi', {
      description: 'OCR API (served through CloudFront at /api)',
      // The site reaches the API through CloudFront (same origin), so CORS is only for local dev.
      // (Referencing the distribution domain here would create a circular dependency.)
      corsPreflight: {
        allowOrigins: ['http://localhost:5173', ...(props.domainName ? [`https://${props.domainName}`] : [])],
        allowHeaders: ['authorization', 'content-type'],
        allowMethods: [apigw.CorsHttpMethod.ANY],
        maxAge: cdk.Duration.hours(1),
      },
    });
    const apiIntegration = new integrations.HttpLambdaIntegration('ApiIntegration', apiFn);
    const jwt = new authorizers.HttpUserPoolAuthorizer('Jwt', userPool, { userPoolClients: [userPoolClient] });
    httpApi.addRoutes({ path: '/api/{proxy+}', methods: [apigw.HttpMethod.GET, apigw.HttpMethod.POST, apigw.HttpMethod.PUT, apigw.HttpMethod.PATCH, apigw.HttpMethod.DELETE], integration: apiIntegration, authorizer: jwt });
    httpApi.addRoutes({ path: '/api/public/{proxy+}', methods: [apigw.HttpMethod.GET], integration: apiIntegration });
    httpApi.addRoutes({ path: '/api/webhooks/{proxy+}', methods: [apigw.HttpMethod.POST], integration: apiIntegration });
    const stage = httpApi.defaultStage!.node.defaultChild as apigw.CfnStage;
    stage.defaultRouteSettings = { throttlingBurstLimit: 200, throttlingRateLimit: 100 };

    distribution.addBehavior('/api/*', new origins.HttpOrigin(`${httpApi.apiId}.execute-api.${this.region}.amazonaws.com`), {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
    });

    // ------------------------------------------------------------------ pipeline workers
    const worker = (name: string, handler: string, memory: number, ephemeralGb: number) => {
      const f = fn(name, handler, {
        memorySize: memory,
        timeout: LONG,
        ephemeralStorageSize: cdk.Size.gibibytes(ephemeralGb),
      });
      dataBucket.grantReadWrite(f);
      return f;
    };
    const textractPerms = new iam.PolicyStatement({
      actions: ['textract:DetectDocumentText', 'textract:StartDocumentTextDetection', 'textract:GetDocumentTextDetection'],
      resources: ['*'],
    });

    const ingestFn = worker('IngestFn', 'ocrsvc.handlers.ingest.handler', 2048, 2);
    ingestQ.q.grantSendMessages(ingestFn);
    processQ.q.grantSendMessages(ingestFn);
    finalizeQ.q.grantSendMessages(ingestFn);
    ingestFn.addEventSource(new SqsEventSource(ingestQ.q, { batchSize: 5, maxConcurrency: 20, reportBatchItemFailures: true }));

    const processFn = worker('ProcessFn', 'ocrsvc.handlers.process.handler', 3008, 10);
    textractQ.q.grantSendMessages(processFn);
    tesseractQ.q.grantSendMessages(processFn);
    finalizeQ.q.grantSendMessages(processFn);
    processFn.addEventSource(new SqsEventSource(processQ.q, { batchSize: 1, maxConcurrency: 20, reportBatchItemFailures: true }));

    const textractStartFn = fn('TextractStartFn', 'ocrsvc.handlers.textract.start_handler', { memorySize: 512, timeout: cdk.Duration.minutes(2) });
    dataBucket.grantReadWrite(textractStartFn);
    textractStartFn.addToRolePolicy(textractPerms);
    textractStartFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['iam:PassRole'], resources: [textractRole.roleArn] }));
    assembleQ.q.grantSendMessages(textractStartFn);
    textractStartFn.addEventSource(new SqsEventSource(textractQ.q, {
      batchSize: 5, maxConcurrency: Math.max(2, props.textractConcurrency), reportBatchItemFailures: true,
    }));

    const textractCollectFn = worker('TextractCollectFn', 'ocrsvc.handlers.textract.collect_handler', 2048, 1);
    textractCollectFn.addToRolePolicy(textractPerms);
    assembleQ.q.grantSendMessages(textractCollectFn);
    textractCollectFn.addEventSource(new SqsEventSource(textractDoneQ.q, { batchSize: 2, maxConcurrency: 10, reportBatchItemFailures: true }));

    const tesseractFn = new lambda.DockerImageFunction(this, 'TesseractFn', {
      code: lambda.DockerImageCode.fromImageAsset(path.join(ROOT, 'backend'), { file: 'tesseract/Dockerfile' }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 4096,
      timeout: LONG,
      ephemeralStorageSize: cdk.Size.gibibytes(4),
      environment: commonEnv,
      tracing: lambda.Tracing.ACTIVE,
      logGroup: new logs.LogGroup(this, 'TesseractFnLogs', { retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY }),
    });
    table.grantReadWriteData(tesseractFn);
    dataBucket.grantReadWrite(tesseractFn);
    assembleQ.q.grantSendMessages(tesseractFn);
    tesseractFn.addEventSource(new SqsEventSource(tesseractQ.q, {
      batchSize: 1, maxConcurrency: Math.max(2, props.tesseractConcurrency), reportBatchItemFailures: true,
    }));

    const assembleFn = worker('AssembleFn', 'ocrsvc.handlers.assemble.handler', 3008, 10);
    finalizeQ.q.grantSendMessages(assembleFn);
    assembleFn.addEventSource(new SqsEventSource(assembleQ.q, { batchSize: 1, maxConcurrency: 20, reportBatchItemFailures: true }));

    const finalizeFn = worker('FinalizeFn', 'ocrsvc.handlers.finalize.handler', 3008, 2);
    dataBucket.grantDelete(finalizeFn);
    finalizeFn.addEventSource(new SqsEventSource(finalizeQ.q, { batchSize: 1, maxConcurrency: 5, reportBatchItemFailures: true }));

    // Every DLQ feeds one handler that settles the failed work so jobs never hang.
    const dlqFn = fn('DlqFn', 'ocrsvc.handlers.dlq.handler', { memorySize: 512, timeout: cdk.Duration.minutes(2) });
    assembleQ.q.grantSendMessages(dlqFn);
    finalizeQ.q.grantSendMessages(dlqFn);
    for (const d of [ingestQ, processQ, textractQ, textractDoneQ, tesseractQ, assembleQ, finalizeQ]) {
      dlqFn.addEventSource(new SqsEventSource(d.dlq, { batchSize: 10, maxConcurrency: 2 }));
    }

    // ------------------------------------------------------------------ frontend
    if (fs.existsSync(path.join(FRONTEND_DIST, 'index.html'))) {
      new s3deploy.BucketDeployment(this, 'DeployWeb', {
        destinationBucket: webBucket,
        sources: [
          s3deploy.Source.asset(FRONTEND_DIST, { exclude: ['config.json'] }),
          s3deploy.Source.jsonData('config.json', {
            region: this.region,
            userPoolId: userPool.userPoolId,
            userPoolClientId: userPoolClient.userPoolClientId,
            apiBase: '/api',
          }),
        ],
        distribution,
        distributionPaths: ['/index.html', '/config.json'],
        memoryLimit: 1024,
      });
    } else {
      cdk.Annotations.of(this).addWarning('frontend/dist not found: run `npm run build` in frontend/ before deploying the site');
    }

    // ------------------------------------------------------------------ outputs
    new cdk.CfnOutput(this, 'SiteUrl', { value: useDomain ? `https://${props.domainName}` : `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, 'CloudFrontDomain', { value: distribution.distributionDomainName, description: 'Point the Cloudflare CNAME here' });
    new cdk.CfnOutput(this, 'ApiDirectUrl', { value: httpApi.apiEndpoint });
    new cdk.CfnOutput(this, 'UserPoolId', { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: userPoolClient.userPoolClientId });
    new cdk.CfnOutput(this, 'DataBucketName', { value: dataBucket.bucketName });
    new cdk.CfnOutput(this, 'RazorpaySecretArn', { value: razorpaySecret.secretArn, description: 'Put your Razorpay keys here' });
    new cdk.CfnOutput(this, 'RazorpayWebhookUrl', { value: `${useDomain ? `https://${props.domainName}` : `https://${distribution.distributionDomainName}`}/api/webhooks/razorpay` });
  }
}
