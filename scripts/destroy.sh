#!/bin/bash

set -e

cd "$(dirname "$0")/../terraform"

terraform init
terraform destroy -auto-approve
