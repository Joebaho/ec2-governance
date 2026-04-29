terraform {
  backend "s3" {
    bucket = "baho-backup-bucket"
    key    = "ec2-governance/terraform.tfstate"
    region = "us-west-2"
  }
}
