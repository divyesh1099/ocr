data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  name       = var.project
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  use_domain      = var.domain_name != ""
  cloudflare_zone = var.cloudflare_zone != "" ? var.cloudflare_zone : join(".", slice(split(".", var.domain_name), 1, length(split(".", var.domain_name))))
  alarm_email     = var.alarm_email != "" ? var.alarm_email : var.superadmin_email
  site_url        = local.use_domain ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.site.domain_name}"

  root          = abspath("${path.module}/..")
  backend_build = "${local.root}/backend/build"

  tags = {
    Project   = var.project
    ManagedBy = "terraform"
    Repo      = var.github_repository
  }

  # Environment shared by every backend function.
  common_env = {
    TABLE_NAME              = aws_dynamodb_table.main.name
    DATA_BUCKET             = aws_s3_bucket.data.bucket
    SUPERADMIN_EMAIL        = lower(var.superadmin_email)
    INGEST_QUEUE_URL        = module.queue["ingest"].url
    PROCESS_QUEUE_URL       = module.queue["process"].url
    TEXTRACT_QUEUE_URL      = module.queue["textract"].url
    TESSERACT_QUEUE_URL     = module.queue["tesseract"].url
    ASSEMBLE_QUEUE_URL      = module.queue["assemble"].url
    FINALIZE_QUEUE_URL      = module.queue["finalize"].url
    TEXTRACT_SNS_TOPIC_ARN  = aws_sns_topic.textract.arn
    TEXTRACT_SNS_ROLE_ARN   = aws_iam_role.textract_publish.arn
    RAZORPAY_SECRET_ARN     = aws_secretsmanager_secret.razorpay.arn
    EVENT_BUS_NAME          = aws_cloudwatch_event_bus.pipeline.name
    STALL_HOURS             = tostring(var.stall_hours)
    POWERTOOLS_SERVICE_NAME = "ocr"
  }
}
