# An SQS work queue with its dead-letter queue, TLS-only policies and a DLQ alarm.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" { type = string }
variable "consumer_timeout_s" {
  description = "Timeout of the Lambda consuming this queue; visibility = timeout + 2 min."
  type        = number
}
variable "max_receive_count" {
  type    = number
  default = 5
}
variable "alarm_topic_arn" { type = string }
variable "extra_policy_statements" {
  description = "Additional queue-policy statements (e.g. let SNS deliver)."
  type        = list(any)
  default     = []
}

resource "aws_sqs_queue" "dlq" {
  name                       = "${var.name}-dlq"
  message_retention_seconds  = 14 * 86400
  visibility_timeout_seconds = 180 # > DLQ handler timeout
  sqs_managed_sse_enabled    = true
}

resource "aws_sqs_queue" "this" {
  name                       = var.name
  visibility_timeout_seconds = var.consumer_timeout_s + 120
  message_retention_seconds  = 4 * 86400
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this.arn]
  })
}

locals {
  deny_insecure = [for arn in [aws_sqs_queue.this.arn, aws_sqs_queue.dlq.arn] : {
    Sid       = "DenyInsecureTransport"
    Effect    = "Deny"
    Principal = "*"
    Action    = "sqs:*"
    Resource  = arn
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }]
}

resource "aws_sqs_queue_policy" "this" {
  queue_url = aws_sqs_queue.this.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = concat([local.deny_insecure[0]], var.extra_policy_statements)
  })
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy    = jsonencode({ Version = "2012-10-17", Statement = [local.deny_insecure[1]] })
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  alarm_name          = "${var.name}-dlq-not-empty"
  alarm_description   = "Messages landed in the ${var.name} dead-letter queue (the DLQ handler marks that work failed so the job still finishes; check the logs)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alarm_topic_arn]
}

output "arn" { value = aws_sqs_queue.this.arn }
output "url" { value = aws_sqs_queue.this.id }
output "name" { value = aws_sqs_queue.this.name }
output "dlq_arn" { value = aws_sqs_queue.dlq.arn }
output "dlq_name" { value = aws_sqs_queue.dlq.name }
