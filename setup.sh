#!/usr/bin/env bash
# =============================================================================
# DS5220 DP3 — One-shot setup & deploy script
# Run this once from the repo root after `pip install chalice`.
#
# What it does:
#   1. Creates the DynamoDB table (arnavwx-prices)
#   2. Creates the S3 bucket (arnavwx-dp3-plots) with public-read access
#   3. Deploys the ingestion Chalice app (scheduled Lambda)
#   4. Deploys the API Chalice app (API Gateway + Lambda)
#   5. Prints the API URL and the Discord registration command
# =============================================================================

set -euo pipefail

REGION="us-east-1"
TABLE_NAME="arnavwx-prices"
BUCKET_NAME="arnavwx-dp3-plots"
PROJECT_ID="arnavwx"

echo "================================================================"
echo " DS5220 DP3 Setup"
echo "================================================================"
echo ""

# --- 1. DynamoDB Table -------------------------------------------------------
echo ">>> [1/5] Creating DynamoDB table: $TABLE_NAME"
aws dynamodb create-table \
  --table-name "$TABLE_NAME" \
  --attribute-definitions \
    AttributeName=coin,AttributeType=S \
    AttributeName=timestamp,AttributeType=S \
  --key-schema \
    AttributeName=coin,KeyType=HASH \
    AttributeName=timestamp,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION" \
  2>/dev/null \
  && echo "    Table created." \
  || echo "    Table already exists — skipping."

# Wait for the table to become ACTIVE
echo "    Waiting for table to become ACTIVE..."
aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"
echo "    Table is ACTIVE."
echo ""

# --- 2. S3 Bucket ------------------------------------------------------------
echo ">>> [2/5] Creating S3 bucket: $BUCKET_NAME"
# us-east-1 does NOT accept --create-bucket-configuration; other regions do.
aws s3api create-bucket \
  --bucket "$BUCKET_NAME" \
  --region "$REGION" \
  2>/dev/null \
  && echo "    Bucket created." \
  || echo "    Bucket already exists — skipping."

echo "    Disabling Block Public Access..."
aws s3api put-public-access-block \
  --bucket "$BUCKET_NAME" \
  --public-access-block-configuration \
    "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false" \
  --region "$REGION"

echo "    Applying public-read bucket policy..."
POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/*"
    }
  ]
}
EOF
)
aws s3api put-bucket-policy \
  --bucket "$BUCKET_NAME" \
  --policy "$POLICY" \
  --region "$REGION"
echo "    Bucket is public-read."
echo ""

# --- 3. Install Python dependencies ------------------------------------------
echo ">>> [3/5] Installing Python dependencies"
pip install chalice --quiet
echo "    chalice installed."
echo ""

# --- 4. Deploy ingestion pipeline --------------------------------------------
echo ">>> [4/5] Deploying ingestion pipeline (chalice deploy)"
cd ingestion
pip install -r requirements.txt --quiet
chalice deploy --stage dev
cd ..
echo ""

# --- 5. Deploy API -----------------------------------------------------------
echo ">>> [5/5] Deploying API (chalice deploy)"
cd api
chalice deploy --stage dev 2>&1 | tee /tmp/api-deploy.txt
API_URL=$(grep "Rest API URL" /tmp/api-deploy.txt | awk '{print $NF}')
cd ..
echo ""

# --- Done --------------------------------------------------------------------
echo "================================================================"
echo " Setup complete!"
echo "================================================================"
echo ""
echo "API URL: ${API_URL:-<check chalice output above>}"
echo ""
echo "Verify the zone apex:"
echo "  curl ${API_URL:-<api-url>}/"
echo ""
echo "Register with Discord (in the #dp3 channel):"
echo "  /register ${PROJECT_ID} <your-discord-username> ${API_URL:-<api-url>}"
echo ""
echo "Test resources:"
echo "  /project ${PROJECT_ID}"
echo "  /project ${PROJECT_ID} current"
echo "  /project ${PROJECT_ID} trend"
echo "  /project ${PROJECT_ID} plot"
echo ""
echo "Note: The first plot will appear after the first ingestion run"
echo "      (~15 min). The trend will be meaningful after a few hours."
