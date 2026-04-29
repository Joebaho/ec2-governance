resource "aws_iam_role" "lambda_role" {
  name = "ec2-governance-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeVolumes",
          "ec2:CreateSnapshot",
          "ec2:TerminateInstances",
          "s3:PutObject",
          "s3:GetObject"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "logs:*"
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "this" {
  function_name    = "ec2-governance-engine"
  filename         = "../lambda/package.zip"
  source_code_hash = fileexists("../lambda/package.zip") ? filebase64sha256("../lambda/package.zip") : null
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.11"
  role             = aws_iam_role.lambda_role.arn

  environment {
    variables = {
      SNS_TOPIC_ARN      = var.sns_topic_arn
      SLACK_WEBHOOK      = var.slack_webhook
      REPORT_BUCKET_NAME = var.report_bucket_name
      SNAPSHOT_STATES    = var.snapshot_states
      TERMINATE_STATES   = var.terminate_states
    }
  }
}

resource "aws_cloudwatch_event_rule" "schedule" {
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.this.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
