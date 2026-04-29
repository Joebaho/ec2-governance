variable "region" {
  default = "us-west-2"
}

variable "sns_topic_arn" {
  type    = string
  default = ""
}

variable "slack_webhook" {
  type    = string
  default = ""
}

variable "report_bucket_name" {
  type    = string
  default = ""
}

variable "snapshot_states" {
  type    = string
  default = "stopped"
}

variable "terminate_states" {
  type    = string
  default = "stopped"
}
