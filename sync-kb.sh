#!/bin/bash
set -euo pipefail

# =============================================================================
# sync-kb.sh — Upload KB documents to S3 and trigger Bedrock KB re-ingestion
#
# Usage:
#   ./sync-kb.sh --profile csna-operations-sso-828 --region ca-central-1
#
# This script:
#   1. Syncs all JSON files from knowledge-base/ to the S3 KB bucket
#   2. Starts a Bedrock Knowledge Base ingestion job
#   3. Polls until ingestion is complete
#
# Prerequisites:
#   - AWS CLI configured with appropriate profile
#   - Knowledge Base already created (run setup-kb.py first)
# =============================================================================

REGION="ca-central-1"
PROFILE=""
KB_DIR="$(dirname "$0")/knowledge-base"

# These can be overridden via env vars or flags
BUCKET_NAME="${KB_BUCKET_NAME:-}"
KNOWLEDGE_BASE_ID="${KB_ID:-}"
DATA_SOURCE_ID="${KB_DATA_SOURCE_ID:-}"

usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  --region       AWS region (default: ca-central-1)"
    echo "  --profile      AWS CLI profile"
    echo "  --bucket       S3 bucket name (auto-detected if not set)"
    echo "  --kb-id        Knowledge Base ID (auto-detected if not set)"
    echo "  --ds-id        Data Source ID (auto-detected if not set)"
    echo "  --kb-dir       Local KB directory (default: ./knowledge-base/)"
    echo ""
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --region) REGION="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --bucket) BUCKET_NAME="$2"; shift 2 ;;
        --kb-id) KNOWLEDGE_BASE_ID="$2"; shift 2 ;;
        --ds-id) DATA_SOURCE_ID="$2"; shift 2 ;;
        --kb-dir) KB_DIR="$2"; shift 2 ;;
        *) usage ;;
    esac
done

PROFILE_FLAG=""
if [[ -n "$PROFILE" ]]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

# ─── Auto-detect bucket name ────────────────────────────────────────────────
if [[ -z "$BUCKET_NAME" ]]; then
    ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text --region "$REGION" $PROFILE_FLAG)
    BUCKET_NAME="lza-terraform-kb-data-${ACCOUNT_ID}"
fi

# ─── Auto-detect Knowledge Base ID ──────────────────────────────────────────
if [[ -z "$KNOWLEDGE_BASE_ID" ]]; then
    KNOWLEDGE_BASE_ID=$(aws bedrock-agent list-knowledge-bases \
        --region "$REGION" $PROFILE_FLAG \
        --query "knowledgeBaseSummaries[?name=='lza-terraform-modules'].knowledgeBaseId | [0]" \
        --output text)
    if [[ "$KNOWLEDGE_BASE_ID" == "None" || -z "$KNOWLEDGE_BASE_ID" ]]; then
        echo "ERROR: Could not auto-detect Knowledge Base ID. Use --kb-id flag."
        exit 1
    fi
fi

# ─── Auto-detect Data Source ID ─────────────────────────────────────────────
if [[ -z "$DATA_SOURCE_ID" ]]; then
    DATA_SOURCE_ID=$(aws bedrock-agent list-data-sources \
        --knowledge-base-id "$KNOWLEDGE_BASE_ID" \
        --region "$REGION" $PROFILE_FLAG \
        --query "dataSourceSummaries[0].dataSourceId" \
        --output text)
    if [[ "$DATA_SOURCE_ID" == "None" || -z "$DATA_SOURCE_ID" ]]; then
        echo "ERROR: Could not auto-detect Data Source ID. Use --ds-id flag."
        exit 1
    fi
fi

echo "=============================================="
echo " KB Sync"
echo "=============================================="
echo " Region         : $REGION"
echo " Bucket         : $BUCKET_NAME"
echo " KB ID          : $KNOWLEDGE_BASE_ID"
echo " Data Source ID : $DATA_SOURCE_ID"
echo " Local dir      : $KB_DIR"
echo " Files          : $(ls "$KB_DIR"/*.json 2>/dev/null | wc -l) JSON files"
echo "=============================================="
echo ""

# ─── Step 1: Upload to S3 ───────────────────────────────────────────────────
echo ">>> Step 1: Syncing knowledge-base/ to S3..."
aws s3 sync "$KB_DIR" "s3://${BUCKET_NAME}/kb/" \
    --exclude "*" --include "*.json" \
    --delete \
    --region "$REGION" $PROFILE_FLAG

echo "    ✓ Uploaded $(ls "$KB_DIR"/*.json | wc -l) files to s3://${BUCKET_NAME}/kb/"
echo ""

# ─── Step 2: Trigger re-ingestion ───────────────────────────────────────────
echo ">>> Step 2: Starting ingestion job..."
INGESTION_JOB_ID=$(aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "$KNOWLEDGE_BASE_ID" \
    --data-source-id "$DATA_SOURCE_ID" \
    --region "$REGION" $PROFILE_FLAG \
    --query "ingestionJob.ingestionJobId" \
    --output text)

echo "    Ingestion job: $INGESTION_JOB_ID"
echo ""

# ─── Step 3: Poll until complete ────────────────────────────────────────────
echo ">>> Step 3: Waiting for ingestion to complete..."
while true; do
    STATUS=$(aws bedrock-agent get-ingestion-job \
        --knowledge-base-id "$KNOWLEDGE_BASE_ID" \
        --data-source-id "$DATA_SOURCE_ID" \
        --ingestion-job-id "$INGESTION_JOB_ID" \
        --region "$REGION" $PROFILE_FLAG \
        --query "ingestionJob.status" \
        --output text)

    case "$STATUS" in
        COMPLETE)
            STATS=$(aws bedrock-agent get-ingestion-job \
                --knowledge-base-id "$KNOWLEDGE_BASE_ID" \
                --data-source-id "$DATA_SOURCE_ID" \
                --ingestion-job-id "$INGESTION_JOB_ID" \
                --region "$REGION" $PROFILE_FLAG \
                --query "ingestionJob.statistics" \
                --output json)
            echo "    ✓ Ingestion COMPLETE!"
            echo "    Stats: $STATS"
            break
            ;;
        FAILED|STOPPED)
            echo "    ✗ Ingestion FAILED (status: $STATUS)"
            aws bedrock-agent get-ingestion-job \
                --knowledge-base-id "$KNOWLEDGE_BASE_ID" \
                --data-source-id "$DATA_SOURCE_ID" \
                --ingestion-job-id "$INGESTION_JOB_ID" \
                --region "$REGION" $PROFILE_FLAG
            exit 1
            ;;
        *)
            echo "    Status: $STATUS — waiting..."
            sleep 10
            ;;
    esac
done

echo ""
echo "=============================================="
echo " SYNC COMPLETE"
echo "=============================================="
echo " KB is now up-to-date with $(ls "$KB_DIR"/*.json | wc -l) documents."
echo " Test with: aws bedrock-agent-runtime retrieve --knowledge-base-id $KNOWLEDGE_BASE_ID ..."
echo "=============================================="
