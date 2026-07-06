#!/bin/bash
set -euo pipefail

# =============================================================================
# deploy.sh — Full deployment script for the LZA Terraform Generator
#
# Usage:
#   ./deploy.sh --region ca-central-1 --profile your-profile --github-token ghp_xxx
#
# This script:
#   1. Sets up the Bedrock Knowledge Base (S3 + IAM + KB + sync)
#   2. Deploys the Lambda via SAM
#   3. Prints next steps for Bedrock Agent creation
# =============================================================================

REGION="ca-central-1"
PROFILE=""
GITHUB_TOKEN=""
GITHUB_REPO_OWNER=""
GITHUB_REPO_NAME=""
STACK_NAME="lza-terraform-generator"

usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  --region          AWS region (default: ca-central-1)"
    echo "  --profile         AWS CLI profile"
    echo "  --github-token    GitHub PAT with repo scope"
    echo "  --repo-owner      GitHub org/user"
    echo "  --repo-name       Target GitHub repository"
    echo "  --stack-name      CloudFormation stack name (default: lza-terraform-generator)"
    echo ""
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --region) REGION="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --github-token) GITHUB_TOKEN="$2"; shift 2 ;;
        --repo-owner) GITHUB_REPO_OWNER="$2"; shift 2 ;;
        --repo-name) GITHUB_REPO_NAME="$2"; shift 2 ;;
        --stack-name) STACK_NAME="$2"; shift 2 ;;
        *) usage ;;
    esac
done

# Validate required params
if [[ -z "$GITHUB_TOKEN" || -z "$GITHUB_REPO_OWNER" || -z "$GITHUB_REPO_NAME" ]]; then
    echo "ERROR: --github-token, --repo-owner, and --repo-name are required"
    usage
fi

PROFILE_FLAG=""
if [[ -n "$PROFILE" ]]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

echo "=============================================="
echo " LZA Terraform Generator — Full Deployment"
echo "=============================================="
echo " Region     : $REGION"
echo " Stack      : $STACK_NAME"
echo " Repo       : $GITHUB_REPO_OWNER/$GITHUB_REPO_NAME"
echo "=============================================="
echo ""

# ─── Step 1: Set up Knowledge Base ───────────────────────────────────────────
echo ">>> Step 1: Setting up Bedrock Knowledge Base..."
KB_OUTPUT=$(python setup-kb.py --region "$REGION" ${PROFILE_FLAG:-})

# Extract KB ID from output
KB_ID=$(echo "$KB_OUTPUT" | grep "Knowledge Base ID" | awk -F': ' '{print $2}' | tr -d ' ')

if [[ -z "$KB_ID" ]]; then
    echo "ERROR: Failed to extract Knowledge Base ID from setup output"
    echo "$KB_OUTPUT"
    exit 1
fi

echo ">>> Knowledge Base ID: $KB_ID"
echo ""

# ─── Step 2: Deploy Lambda via SAM ──────────────────────────────────────────
echo ">>> Step 2: Building and deploying Lambda..."
cd infrastructure

sam build

sam deploy \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --resolve-s3 \
    --capabilities CAPABILITY_IAM \
    --no-confirm-changeset \
    --parameter-overrides \
        "GitHubToken=$GITHUB_TOKEN" \
        "GitHubRepoOwner=$GITHUB_REPO_OWNER" \
        "GitHubRepoName=$GITHUB_REPO_NAME" \
        "KnowledgeBaseId=$KB_ID" \
    ${PROFILE_FLAG:-}

cd ..

# ─── Step 3: Get Lambda ARN ─────────────────────────────────────────────────
LAMBDA_ARN=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='FunctionArn'].OutputValue" \
    --output text \
    ${PROFILE_FLAG:-})

echo ""
echo "=============================================="
echo " DEPLOYMENT COMPLETE"
echo "=============================================="
echo ""
echo "  Lambda ARN        : $LAMBDA_ARN"
echo "  Knowledge Base ID : $KB_ID"
echo "  Region            : $REGION"
echo ""
echo "  NEXT STEP: Create the Bedrock Agent"
echo ""
echo "  1. Go to: https://$REGION.console.aws.amazon.com/bedrock/home#/agents"
echo "  2. Create Agent → Name: 'LZA Terraform Generator'"
echo "  3. Add Action Group:"
echo "     - Name: GenerateIaC"
echo "     - Lambda: $LAMBDA_ARN"
echo "     - API Schema: use the OpenAPI spec below"
echo ""
echo "  4. Agent Instructions (paste this):"
echo "     'You help users generate Terraform configurations for new AWS accounts."
echo "      Ask them for: account name, customization name, and which AWS services"
echo "      they need. Then invoke the GenerateIaC action group.'"
echo ""
echo "=============================================="
