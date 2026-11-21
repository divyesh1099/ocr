# One Lambda function with its own least-privilege IAM role and log group.
# Zip functions pass `package_file` + `handler`; container functions pass `image_uri`.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" { type = string }
variable "description" {
  type    = string
  default = ""
}
variable "handler" {
  type    = string
  default = null
}
variable "package_file" {
  type    = string
  default = null
}
variable "source_code_hash" {
  type    = string
  default = null
}
variable "image_uri" {
  type    = string
  default = null
}
variable "image_command" {
  type    = list(string)
  default = null
}
variable "runtime" {
  type    = string
  default = "python3.12"
}
variable "memory_mb" {
  type    = number
  default = 1024
}
variable "timeout_s" {
  type    = number
  default = 60
}
variable "ephemeral_mb" {
  type    = number
  default = 512
}
variable "environment" {
  type    = map(string)
  default = {}
}
variable "policy_statements" {
  description = "Extra IAM statements for the function's role."
  type = list(object({
    sid       = string
    actions   = list(string)
    resources = list(string)
  }))
  default = []
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "tags" {
  type    = map(string)
  default = {}
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "this" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
  dynamic "statement" {
    for_each = var.policy_statements
    content {
      sid       = statement.value.sid
      actions   = statement.value.actions
      resources = statement.value.resources
    }
  }
}

resource "aws_iam_role_policy" "this" {
  name   = "permissions"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.this.json
}

resource "aws_lambda_function" "this" {
  function_name = var.name
  description   = var.description
  role          = aws_iam_role.this.arn
  architectures = ["arm64"]
  memory_size   = var.memory_mb
  timeout       = var.timeout_s

  package_type     = var.image_uri == null ? "Zip" : "Image"
  filename         = var.image_uri == null ? var.package_file : null
  source_code_hash = var.image_uri == null ? var.source_code_hash : null
  handler          = var.image_uri == null ? var.handler : null
  runtime          = var.image_uri == null ? var.runtime : null
  image_uri        = var.image_uri

  dynamic "image_config" {
    for_each = var.image_uri != null && var.image_command != null ? [1] : []
    content {
      command = var.image_command
    }
  }

  ephemeral_storage {
    size = var.ephemeral_mb
  }

  environment {
    variables = var.environment
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this.name
  }

  tags = var.tags

  # The role policy must exist before SQS event source mappings validate permissions.
  depends_on = [aws_iam_role_policy.this]
}

output "arn" { value = aws_lambda_function.this.arn }
output "name" { value = aws_lambda_function.this.function_name }
output "invoke_arn" { value = aws_lambda_function.this.invoke_arn }
output "role_arn" { value = aws_iam_role.this.arn }
output "role_name" { value = aws_iam_role.this.name }
