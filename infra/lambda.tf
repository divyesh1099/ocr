# ---------------------------------------------------------------- Code packages
# Zip functions: backend/build (our package + arm64 wheels), built by scripts/build-backend.sh.
data "archive_file" "backend" {
  type        = "zip"
  source_dir  = local.backend_build
  output_path = "${local.root}/backend/dist/backend.zip"
  excludes    = ["**/__pycache__/**"]
}

# Tesseract container image: built for arm64 and pushed to ECR whenever its sources change.
resource "aws_ecr_repository" "tesseract" {
  name                 = "${local.name}-tesseract"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "tesseract" {
  repository = aws_ecr_repository.tesseract.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

locals {
  backend_dir = "${local.root}/backend"
  tesseract_inputs = sort(concat(
    tolist(fileset(local.backend_dir, "src/**/*.py")),
    ["requirements.txt", "tesseract/Dockerfile"],
  ))
  tesseract_tag = substr(sha256(join("", [for f in local.tesseract_inputs : filesha256("${local.backend_dir}/${f}")])), 0, 16)
  tesseract_uri = "${aws_ecr_repository.tesseract.repository_url}:${local.tesseract_tag}"
}

resource "terraform_data" "tesseract_image" {
  triggers_replace = [local.tesseract_uri]

  provisioner "local-exec" {
    command = "${local.root}/scripts/push-tesseract-image.sh"
    environment = {
      IMAGE_URI   = local.tesseract_uri
      AWS_REGION  = var.aws_region
      BACKEND_DIR = local.backend_dir
    }
  }
}

# ---------------------------------------------------------------- Reusable IAM statements
locals {
  table_arns = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]

  st_dynamodb = {
    sid = "DynamoDB"
    actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
      "dynamodb:Query", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:ConditionCheckItem",
    "dynamodb:DescribeTable"]
    resources = local.table_arns
  }
  st_s3_objects = {
    sid = "DataObjects"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts"]
    resources = ["${aws_s3_bucket.data.arn}/*"]
  }
  st_s3_bucket = {
    sid       = "DataBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
    resources = [aws_s3_bucket.data.arn]
  }
  st_events = {
    sid       = "PublishLifecycleEvents"
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.pipeline.arn]
  }
  st_textract = {
    sid       = "Textract"
    actions   = ["textract:DetectDocumentText", "textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"]
    resources = ["*"]
  }

  base_statements   = [local.st_dynamodb, local.st_events]
  worker_statements = [local.st_dynamodb, local.st_events, local.st_s3_objects, local.st_s3_bucket]
}

# Send permission to a set of queues.
# Anything that calls jobs.py may move work to assemble (unit finished) or finalize (job finished).
locals {
  send_to       = { for k, q in module.queue : k => q.arn }
  send_pipeline = [local.send_to["assemble"], local.send_to["finalize"]]
}

# ---------------------------------------------------------------- Functions
locals {
  zip = {
    package_file     = data.archive_file.backend.output_path
    source_code_hash = data.archive_file.backend.output_base64sha256
  }
}

module "fn_pre_sign_up" {
  source             = "./modules/lambda"
  name               = "${local.name}-pre-sign-up"
  description        = "Cognito pre sign-up: enforces the access mode"
  handler            = "ocrsvc.handlers.triggers.pre_sign_up"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 512
  timeout_s          = 5
  environment        = local.common_env
  policy_statements  = local.base_statements
  log_retention_days = var.log_retention_days
}

module "fn_post_confirmation" {
  source             = "./modules/lambda"
  name               = "${local.name}-post-confirmation"
  description        = "Cognito post confirmation: creates the user profile"
  handler            = "ocrsvc.handlers.triggers.post_confirmation"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 512
  timeout_s          = 5
  environment        = local.common_env
  policy_statements  = local.base_statements
  log_retention_days = var.log_retention_days
}

module "fn_api" {
  source             = "./modules/lambda"
  name               = "${local.name}-api"
  description        = "HTTP API (behind CloudFront /api)"
  handler            = "ocrsvc.handlers.api.handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 1024
  timeout_s          = 29
  environment        = merge(local.common_env, { USER_POOL_ID = aws_cognito_user_pool.main.id })
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.worker_statements, [
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = concat([local.send_to["ingest"]], local.send_pipeline) },
    { sid = "RazorpaySecret", actions = ["secretsmanager:GetSecretValue"], resources = [aws_secretsmanager_secret.razorpay.arn] },
    { sid = "CognitoAdmin", actions = ["cognito-idp:AdminDisableUser", "cognito-idp:AdminEnableUser", "cognito-idp:AdminUserGlobalSignOut"],
    resources = [aws_cognito_user_pool.main.arn] },
  ])
}

module "fn_ingest" {
  source             = "./modules/lambda"
  name               = "${local.name}-ingest"
  description        = "Scans uploads, classifies files, fans out zip batches"
  handler            = "ocrsvc.handlers.ingest.handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 2048
  timeout_s          = local.long_timeout
  ephemeral_mb       = 2048
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.worker_statements, [
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = concat([local.send_to["ingest"], local.send_to["process"]], local.send_pipeline) },
    { sid = "Consume", actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], resources = [module.queue["ingest"].arn] },
  ])
}

module "fn_process" {
  source             = "./modules/lambda"
  name               = "${local.name}-process"
  description        = "Counts pages, reserves credits, splits documents into OCR units"
  handler            = "ocrsvc.handlers.process.handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 3008
  timeout_s          = local.long_timeout
  ephemeral_mb       = 10240
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.worker_statements, [
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = concat([local.send_to["textract"], local.send_to["tesseract"]], local.send_pipeline) },
    { sid = "Consume", actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], resources = [module.queue["process"].arn] },
  ])
}

module "fn_textract_start" {
  source             = "./modules/lambda"
  name               = "${local.name}-textract-start"
  description        = "Starts Textract jobs (sync for images, async for PDFs/TIFFs)"
  handler            = "ocrsvc.handlers.textract.start_handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 512
  timeout_s          = 120
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.worker_statements, [
    local.st_textract,
    { sid = "PassTextractRole", actions = ["iam:PassRole"], resources = [aws_iam_role.textract_publish.arn] },
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = local.send_pipeline },
    { sid = "Consume", actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], resources = [module.queue["textract"].arn] },
  ])
}

module "fn_textract_collect" {
  source             = "./modules/lambda"
  name               = "${local.name}-textract-collect"
  description        = "Collects finished async Textract jobs"
  handler            = "ocrsvc.handlers.textract.collect_handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 2048
  timeout_s          = local.long_timeout
  ephemeral_mb       = 1024
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.worker_statements, [
    local.st_textract,
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = local.send_pipeline },
    { sid = "Consume", actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], resources = [module.queue["textract-done"].arn] },
  ])
}

module "fn_tesseract" {
  source             = "./modules/lambda"
  name               = "${local.name}-tesseract"
  description        = "Tesseract 5 OCR worker (container image)"
  image_uri          = local.tesseract_uri
  memory_mb          = 4096
  timeout_s          = local.long_timeout
  ephemeral_mb       = 4096
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.worker_statements, [
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = local.send_pipeline },
    { sid = "Consume", actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], resources = [module.queue["tesseract"].arn] },
  ])
  depends_on = [terraform_data.tesseract_image]
}

module "fn_assemble" {
  source             = "./modules/lambda"
  name               = "${local.name}-assemble"
  description        = "Merges OCR units into per-file txt/json/pdf outputs"
  handler            = "ocrsvc.handlers.assemble.handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 3008
  timeout_s          = local.long_timeout
  ephemeral_mb       = 10240
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.worker_statements, [
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = local.send_pipeline },
    { sid = "Consume", actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], resources = [module.queue["assemble"].arn] },
  ])
}

module "fn_finalize" {
  source             = "./modules/lambda"
  name               = "${local.name}-finalize"
  description        = "Streams all outputs into the job's result zip; deletes jobs"
  handler            = "ocrsvc.handlers.finalize.handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 3008
  timeout_s          = local.long_timeout
  ephemeral_mb       = 2048
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.worker_statements, [
    { sid = "Consume", actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], resources = [module.queue["finalize"].arn] },
  ])
}

module "fn_dlq" {
  source             = "./modules/lambda"
  name               = "${local.name}-dlq"
  description        = "Settles work that failed repeatedly so jobs still finish"
  handler            = "ocrsvc.handlers.dlq.handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 512
  timeout_s          = 120
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.base_statements, [
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = local.send_pipeline },
    { sid = "ConsumeDlqs", actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], resources = [for q in module.queue : q.dlq_arn] },
  ])
}

module "fn_sweeper" {
  source             = "./modules/lambda"
  name               = "${local.name}-sweeper"
  description        = "Scheduled safety net: re-checks, re-finalizes and alerts on stuck jobs"
  handler            = "ocrsvc.handlers.sweeper.handler"
  package_file       = local.zip.package_file
  source_code_hash   = local.zip.source_code_hash
  memory_mb          = 512
  timeout_s          = 120
  environment        = local.common_env
  log_retention_days = var.log_retention_days
  policy_statements = concat(local.base_statements, [
    { sid = "SendQueues", actions = ["sqs:SendMessage"], resources = local.send_pipeline },
  ])
}

# ---------------------------------------------------------------- SQS -> Lambda triggers
locals {
  consumers = {
    ingest        = { fn = module.fn_ingest.arn, batch = 5, concurrency = 20 }
    process       = { fn = module.fn_process.arn, batch = 1, concurrency = 20 }
    textract      = { fn = module.fn_textract_start.arn, batch = 5, concurrency = max(2, var.textract_concurrency) }
    textract-done = { fn = module.fn_textract_collect.arn, batch = 2, concurrency = 10 }
    tesseract     = { fn = module.fn_tesseract.arn, batch = 1, concurrency = max(2, var.tesseract_concurrency) }
    assemble      = { fn = module.fn_assemble.arn, batch = 1, concurrency = 20 }
    finalize      = { fn = module.fn_finalize.arn, batch = 1, concurrency = 5 }
  }
}

resource "aws_lambda_event_source_mapping" "work" {
  for_each                = local.consumers
  event_source_arn        = module.queue[each.key].arn
  function_name           = each.value.fn
  batch_size              = each.value.batch
  function_response_types = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = each.value.concurrency
  }
}

resource "aws_lambda_event_source_mapping" "dlq" {
  for_each         = local.queues
  event_source_arn = module.queue[each.key].dlq_arn
  function_name    = module.fn_dlq.arn
  batch_size       = 10
  scaling_config {
    maximum_concurrency = 2
  }
}
