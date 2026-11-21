# ---------------------------------------------------------------- EventBridge
#
# Work moves through SQS (ordered retries, DLQs, back-pressure). EventBridge carries the
# job *lifecycle*, which people and other systems react to:
#
#   backend ──PutEvents──► bus "ocr-pipeline"  (source: ocr.pipeline)
#                            ├─ rule all-events ──► CloudWatch Logs  /aws/events/ocr-pipeline  (audit trail)
#                            ├─ archive (30 days, replayable)
#                            └─ rule alerts (Job Failed | Job Stalled) ──► SNS alarm topic ──► email
#
#   EventBridge Scheduler ── every 15 min ──► sweeper Lambda (re-check / re-finalize / stall alert)

resource "aws_cloudwatch_event_bus" "pipeline" {
  name        = "${local.name}-pipeline"
  description = "OCR job lifecycle events"
}

resource "aws_cloudwatch_event_archive" "pipeline" {
  name             = "${local.name}-pipeline"
  description      = "Replayable copy of all OCR lifecycle events"
  event_source_arn = aws_cloudwatch_event_bus.pipeline.arn
  retention_days   = 30
}

# --- audit log
resource "aws_cloudwatch_log_group" "events" {
  name              = "/aws/events/${local.name}-pipeline"
  retention_in_days = 90
}

data "aws_iam_policy_document" "events_to_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.events.arn}:*"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.all.arn]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "events" {
  policy_name     = "${local.name}-events-to-logs"
  policy_document = data.aws_iam_policy_document.events_to_logs.json
}

resource "aws_cloudwatch_event_rule" "all" {
  name           = "${local.name}-all-events"
  description    = "Every OCR lifecycle event into CloudWatch Logs"
  event_bus_name = aws_cloudwatch_event_bus.pipeline.name
  event_pattern  = jsonencode({ source = ["ocr.pipeline"] })
}

resource "aws_cloudwatch_event_target" "all_to_logs" {
  rule           = aws_cloudwatch_event_rule.all.name
  event_bus_name = aws_cloudwatch_event_bus.pipeline.name
  target_id      = "logs"
  arn            = aws_cloudwatch_log_group.events.arn
  depends_on     = [aws_cloudwatch_log_resource_policy.events]
}

# --- alerts by email
resource "aws_cloudwatch_event_rule" "alerts" {
  name           = "${local.name}-job-alerts"
  description    = "Failed or stalled jobs -> email"
  event_bus_name = aws_cloudwatch_event_bus.pipeline.name
  event_pattern = jsonencode({
    source        = ["ocr.pipeline"]
    "detail-type" = ["Job Failed", "Job Stalled"]
  })
}

resource "aws_cloudwatch_event_target" "alerts_to_sns" {
  rule           = aws_cloudwatch_event_rule.alerts.name
  event_bus_name = aws_cloudwatch_event_bus.pipeline.name
  target_id      = "email"
  arn            = aws_sns_topic.alarms.arn

  input_transformer {
    input_paths = {
      type    = "$.detail-type"
      job     = "$.detail.jobId"
      name    = "$.detail.name"
      owner   = "$.detail.owner"
      error   = "$.detail.lastError"
      pending = "$.detail.pending"
      hours   = "$.detail.hoursRunning"
    }
    input_template = <<-EOT
      "OCR <type>: job <job> (\"<name>\", owner <owner>). Hours running: <hours>. Pending work items: <pending>. Last error: <error>. Open ${local.site_url}/admin to force-finish or inspect it."
    EOT
  }
}

# --- scheduled sweeper
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-sweeper"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = module.fn_sweeper.arn }]
  })
}

resource "aws_scheduler_schedule" "sweeper" {
  name                = "${local.name}-sweeper"
  description         = "Safety net for stuck OCR jobs"
  schedule_expression = "rate(15 minutes)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = module.fn_sweeper.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 600
    }
  }
}
