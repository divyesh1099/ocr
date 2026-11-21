#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { OcrStack } from '../lib/ocr-stack';

const app = new cdk.App();
const ctx = (k: string) => app.node.tryGetContext(k);

new OcrStack(app, ctx('stackName') || 'Ocr', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || 'ap-south-1',
  },
  superadminEmail: String(ctx('superadminEmail') || process.env.SUPERADMIN_EMAIL || ''),
  alarmEmail: String(ctx('alarmEmail') || process.env.ALARM_EMAIL || ''),
  domainName: String(ctx('domainName') || ''),
  certificateArn: String(ctx('certificateArn') || process.env.CERTIFICATE_ARN || ''),
  textractConcurrency: Number(ctx('textractConcurrency') || 5),
  tesseractConcurrency: Number(ctx('tesseractConcurrency') || 50),
  uploadRetentionDays: Number(ctx('uploadRetentionDays') || 7),
  resultRetentionDays: Number(ctx('resultRetentionDays') || 30),
  description: 'Large-scale OCR website: S3 + SQS + Lambda + Textract/Tesseract + Cognito + CloudFront',
});
