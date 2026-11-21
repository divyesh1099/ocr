# ---------------------------------------------------------------- Alarm topic (email)
resource "aws_sns_topic" "alarms" {
  name         = "${local.name}-alarms"
  display_name = "OCR alarms"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = local.alarm_email
}

data "aws_iam_policy_document" "alarms_topic" {
  statement {
    sid       = "CloudWatchAlarms"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
  statement {
    sid       = "EventBridgeAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.alerts.arn]
    }
  }
}

resource "aws_sns_topic_policy" "alarms" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarms_topic.json
}

# ---------------------------------------------------------------- Pipeline queues
#
#  ingest ─► process ─┬─► textract ─(Textract async ─► SNS)─► textract-done ─┐
#                     └─► tesseract ─────────────────────────────────────────┴─► assemble ─► finalize
#
# Each queue has its own DLQ; every DLQ feeds the DLQ handler, which settles the failed work
# so the job still finishes.
locals {
  long_timeout = 900
  queues = {
    ingest        = { timeout = local.long_timeout, max_receive = 5 }
    process       = { timeout = local.long_timeout, max_receive = 5 }
    textract      = { timeout = 120, max_receive = 12 } # extra retries for Textract throttling
    textract-done = { timeout = local.long_timeout, max_receive = 5 }
    tesseract     = { timeout = local.long_timeout, max_receive = 3 }
    assemble      = { timeout = local.long_timeout, max_receive = 5 }
    finalize      = { timeout = local.long_timeout, max_receive = 5 }
  }
}

module "queue" {
  source   = "./modules/queue"
  for_each = local.queues

  name               = "${local.name}-${each.key}"
  consumer_timeout_s = each.value.timeout
  max_receive_count  = each.value.max_receive
  alarm_topic_arn    = aws_sns_topic.alarms.arn

  extra_policy_statements = each.key == "textract-done" ? [{
    Sid       = "TextractTopicDelivers"
    Effect    = "Allow"
    Principal = { Service = "sns.amazonaws.com" }
    Action    = "sqs:SendMessage"
    Resource  = "arn:${local.partition}:sqs:${var.aws_region}:${local.account_id}:${local.name}-textract-done"
    Condition = { ArnEquals = { "aws:SourceArn" = "arn:${local.partition}:sns:${var.aws_region}:${local.account_id}:${local.name}-textract-done" } }
  }] : []
}

# ---------------------------------------------------------------- Textract completion notifications
# Textract async jobs can only notify through SNS; the topic fans into the textract-done queue.
resource "aws_sns_topic" "textract" {
  name         = "${local.name}-textract-done"
  display_name = "Textract job completions"
}

resource "aws_sns_topic_subscription" "textract_done" {
  topic_arn            = aws_sns_topic.textract.arn
  protocol             = "sqs"
  endpoint             = module.queue["textract-done"].arn
  raw_message_delivery = true
}

data "aws_iam_policy_document" "textract_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["textract.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "textract_publish" {
  name               = "${local.name}-textract-publish"
  description        = "Lets Textract publish async job completions to the OCR SNS topic"
  assume_role_policy = data.aws_iam_policy_document.textract_assume.json
}

resource "aws_iam_role_policy" "textract_publish" {
  name = "publish"
  role = aws_iam_role.textract_publish.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.textract.arn }]
  })
}
