output "site_url" {
  value = local.site_url
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.site.domain_name
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.site.id
}

output "web_bucket" {
  value = aws_s3_bucket.web.bucket
}

output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "api_direct_url" {
  description = "For local frontend dev: set apiBase to this + /api"
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "frontend_config" {
  description = "Written to config.json next to the site by scripts/deploy.sh"
  value = {
    region           = var.aws_region
    userPoolId       = aws_cognito_user_pool.main.id
    userPoolClientId = aws_cognito_user_pool_client.web.id
    apiBase          = "/api"
  }
}

output "razorpay_secret_arn" {
  description = "Put your Razorpay keys in this secret"
  value       = aws_secretsmanager_secret.razorpay.arn
}

output "razorpay_webhook_url" {
  value = "${local.site_url}/api/webhooks/razorpay"
}

output "event_bus_name" {
  value = aws_cloudwatch_event_bus.pipeline.name
}

output "dashboard_url" {
  value = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/${aws_cloudwatch_dashboard.pipeline.dashboard_name}"
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN secret in GitHub"
  value       = local.github_enabled ? aws_iam_role.github_deploy[0].arn : ""
}
