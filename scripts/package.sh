#!/bin/bash

set -e

cd lambda

rm -rf package.zip
rm -rf __pycache__
rm -rf urllib3 urllib3-*.dist-info
pip install -r requirements.txt -t .

zip -r package.zip .
