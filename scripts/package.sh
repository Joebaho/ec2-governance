#!/bin/bash

set -e

cd lambda

rm -rf package.zip
pip install -r requirements.txt -t .

zip -r package.zip .