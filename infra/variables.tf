variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "ocr"
}

variable "aws_region" {
  description = "Region for everything except the CloudFront certificate. Textract must be available here."
  type        = string
  default     = "ap-south-1"
}

variable "superadmin_email" {
  description = "This email becomes the superadmin when it signs up."
  type        = string
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.superadmin_email))
    error_message = "superadmin_email must be an email address."
  }
}

variable "alarm_email" {
  description = "Where alarms and job failure/stall alerts are emailed. Defaults to the superadmin."
  type        = string
  default     = ""
}

variable "domain_name" {
  description = "Custom domain for the site (DNS in Cloudflare). Empty = serve on the *.cloudfront.net address."
  type        = string
  default     = "ocr.divyeshvishwakarma.com"
}

variable "cloudflare_zone" {
  description = "Cloudflare zone that holds domain_name. Empty = derived (ocr.example.com -> example.com)."
  type        = string
  default     = ""
}

variable "textract_concurrency" {
  description = "Max parallel Textract-start Lambdas (raise together with your Textract TPS quota)."
  type        = number
  default     = 5
}

variable "tesseract_concurrency" {
  description = "Max parallel Tesseract Lambdas."
  type        = number
  default     = 50
}

variable "upload_retention_days" {
  description = "Uploaded originals are deleted after this many days."
  type        = number
  default     = 7
}

variable "result_retention_days" {
  description = "Results are deleted after this many days."
  type        = number
  default     = 30
}

variable "stall_hours" {
  description = "A job still processing after this many hours triggers one 'Job Stalled' alert."
  type        = number
  default     = 6
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "github_repository" {
  description = "owner/repo allowed to deploy through GitHub Actions OIDC. Empty = no deploy role."
  type        = string
  default     = "divyesh1099/ocr"
}
