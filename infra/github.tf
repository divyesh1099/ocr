# ---------------------------------------------------------------- GitHub Actions deploy role (OIDC)
# No long-lived AWS keys in GitHub: the deploy workflow exchanges its OIDC token for this role.
# Only the `production` environment of the configured repository can assume it.

locals {
  github_enabled = var.github_repository != ""
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = local.github_enabled ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_assume" {
  count = local.github_enabled ? 1 : 0
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:production"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  count                = local.github_enabled ? 1 : 0
  name                 = "${local.name}-github-deploy"
  description          = "Assumed by GitHub Actions (${var.github_repository}, environment production) to run terraform apply"
  assume_role_policy   = data.aws_iam_policy_document.github_assume[0].json
  max_session_duration = 7200
}

# Terraform manages IAM roles, Cognito, CloudFront, etc., so the deploy role needs broad rights.
# It can only be assumed from the protected production environment of this one repository.
resource "aws_iam_role_policy_attachment" "github_deploy" {
  count      = local.github_enabled ? 1 : 0
  role       = aws_iam_role.github_deploy[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AdministratorAccess"
}
