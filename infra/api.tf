# ---------------------------------------------------------------- HTTP API (Cognito JWT)
resource "aws_apigatewayv2_api" "http" {
  name          = "${local.name}-api"
  description   = "OCR API (served through CloudFront at /api)"
  protocol_type = "HTTP"

  # The site calls the API through CloudFront (same origin), so CORS only matters for local dev.
  cors_configuration {
    allow_origins = compact(["http://localhost:5173", local.use_domain ? "https://${var.domain_name}" : ""])
    allow_headers = ["authorization", "content-type"]
    allow_methods = ["*"]
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.http.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
    audience = [aws_cognito_user_pool_client.web.id]
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.fn_api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

locals {
  # Signed-in routes use explicit methods so CORS preflight (OPTIONS) is answered by API Gateway.
  api_routes = merge(
    { for m in ["GET", "POST", "PUT", "PATCH", "DELETE"] : "${m} /api/{proxy+}" => true },
    { "GET /api/public/{proxy+}" = false, "POST /api/webhooks/{proxy+}" = false },
  )
}

resource "aws_apigatewayv2_route" "api" {
  for_each           = local.api_routes
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = each.value ? "JWT" : "NONE"
  authorizer_id      = each.value ? aws_apigatewayv2_authorizer.jwt.id : null
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${local.name}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 200
    throttling_rate_limit  = 100
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId        = "$context.requestId", ip = "$context.identity.sourceIp", method = "$context.httpMethod",
      path             = "$context.path", status = "$context.status", latencyMs = "$context.responseLatency",
      error            = "$context.error.message", authError = "$context.authorizer.error",
      integrationError = "$context.integrationErrorMessage", sub = "$context.authorizer.claims.sub"
    })
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = module.fn_api.name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
