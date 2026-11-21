# ---------------------------------------------------------------- Alarms + dashboard
# DLQ alarms live in modules/queue. Here: API health and a one-page pipeline dashboard.

resource "aws_cloudwatch_metric_alarm" "api_errors" {
  alarm_name          = "${local.name}-api-errors"
  alarm_description   = "The API Lambda is throwing errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = module.fn_api.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  alarm_description   = "The HTTP API is returning 5xx responses"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.http.id, Stage = "$default" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

locals {
  pipeline_functions = [
    module.fn_ingest.name, module.fn_process.name, module.fn_textract_start.name, module.fn_textract_collect.name,
    module.fn_tesseract.name, module.fn_assemble.name, module.fn_finalize.name, module.fn_dlq.name,
  ]
  r = var.aws_region
}

resource "aws_cloudwatch_dashboard" "pipeline" {
  dashboard_name = "${local.name}-pipeline"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title   = "Queue depth (visible messages)", region = local.r, view = "timeSeries", stacked = false, period = 60, stat = "Maximum"
          metrics = [for k, q in module.queue : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q.name, { label = k }]]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title   = "Dead-letter queues", region = local.r, view = "timeSeries", period = 300, stat = "Maximum"
          metrics = [for k, q in module.queue : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q.dlq_name, { label = "${k} DLQ" }]]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title   = "Concurrent executions", region = local.r, view = "timeSeries", period = 60, stat = "Maximum"
          metrics = [for f in local.pipeline_functions : ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", f]]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title = "Errors / throttles", region = local.r, view = "timeSeries", period = 300, stat = "Sum"
          metrics = concat(
            [for f in local.pipeline_functions : ["AWS/Lambda", "Errors", "FunctionName", f]],
            [for f in local.pipeline_functions : ["AWS/Lambda", "Throttles", "FunctionName", f]],
          )
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title = "API", region = local.r, view = "timeSeries", period = 60, stat = "Sum"
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiId", aws_apigatewayv2_api.http.id, "Stage", "$default"],
            [".", "4xx", ".", ".", ".", "."],
            [".", "5xx", ".", ".", ".", "."],
          ]
        }
      },
      {
        type = "log", x = 12, y = 12, width = 12, height = 6
        properties = {
          title = "Recent lifecycle events (EventBridge)", region = local.r, view = "table"
          query = "SOURCE '${aws_cloudwatch_log_group.events.name}' | fields @timestamp, `detail-type`, detail.jobId, detail.status, detail.docsFailed | sort @timestamp desc | limit 50"
        }
      },
    ]
  })
}
