"""
Setup script for the Bedrock Knowledge Base.

Creates:
1. S3 bucket for KB data source
2. Uploads terraform-modules-kb.json
3. Creates the Bedrock Knowledge Base with OpenSearch Serverless vector store
4. Creates and syncs the data source

Usage:
    python setup-kb.py --region ca-central-1 --profile your-profile

Requires: boto3, AWS credentials with permissions for S3, Bedrock, AOSS, IAM
"""

import argparse
import json
import logging
import time
import sys

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KB_NAME = "lza-terraform-modules"
KB_DESCRIPTION = "Terraform module definitions for LZA account customizations"
EMBEDDING_MODEL_ARN = "arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0"
S3_KEY = "kb/terraform-modules-kb.json"
KB_DATA_FILE = "knowledge-base/terraform-modules-kb.json"

# IAM role for the Knowledge Base
KB_ROLE_NAME = "BedrockKnowledgeBaseRole-lza-terraform"
KB_ROLE_POLICY_NAME = "BedrockKBPolicy-lza-terraform"


def get_account_id(session):
    return session.client("sts").get_caller_identity()["Account"]


def create_s3_bucket(s3, bucket_name, region):
    """Create an encrypted S3 bucket for KB data."""
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        logger.info("Created S3 bucket: %s", bucket_name)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            logger.info("S3 bucket already exists: %s", bucket_name)
        else:
            raise

    # Enable encryption
    s3.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )

    # Block public access
    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )


def upload_kb_data(s3, bucket_name):
    """Upload the Terraform modules KB JSON to S3."""
    s3.upload_file(KB_DATA_FILE, bucket_name, S3_KEY)
    logger.info("Uploaded %s to s3://%s/%s", KB_DATA_FILE, bucket_name, S3_KEY)


def create_kb_role(iam, account_id, region, bucket_name):
    """Create the IAM role for the Bedrock Knowledge Base."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                },
            }
        ],
    }

    try:
        response = iam.create_role(
            RoleName=KB_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="IAM role for Bedrock Knowledge Base - LZA Terraform modules",
        )
        role_arn = response["Role"]["Arn"]
        logger.info("Created IAM role: %s", role_arn)
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = f"arn:aws:iam::{account_id}:role/{KB_ROLE_NAME}"
            logger.info("IAM role already exists: %s", role_arn)
        else:
            raise

    # Attach inline policy for S3 + Bedrock embeddings
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel"],
                "Resource": [
                    f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0"
                ],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "aoss:APIAccessAll",
                ],
                "Resource": ["*"],
            },
        ],
    }

    iam.put_role_policy(
        RoleName=KB_ROLE_NAME,
        PolicyName=KB_ROLE_POLICY_NAME,
        PolicyDocument=json.dumps(policy_document),
    )
    logger.info("Attached inline policy to role")

    # Wait for role propagation
    time.sleep(10)
    return role_arn


def create_knowledge_base(bedrock_agent, role_arn, region):
    """Create the Bedrock Knowledge Base."""
    embedding_model_arn = EMBEDDING_MODEL_ARN.format(region=region)

    try:
        response = bedrock_agent.create_knowledge_base(
            name=KB_NAME,
            description=KB_DESCRIPTION,
            roleArn=role_arn,
            knowledgeBaseConfiguration={
                "type": "VECTOR",
                "vectorKnowledgeBaseConfiguration": {
                    "embeddingModelArn": embedding_model_arn,
                },
            },
            storageConfiguration={
                "type": "OPENSEARCH_SERVERLESS",
                "opensearchServerlessConfiguration": {
                    "collectionArn": "auto",  # Bedrock manages the collection
                    "vectorIndexName": "bedrock-knowledge-base-default-index",
                    "fieldMapping": {
                        "vectorField": "bedrock-knowledge-base-default-vector",
                        "textField": "AMAZON_BEDROCK_TEXT_CHUNK",
                        "metadataField": "AMAZON_BEDROCK_METADATA",
                    },
                },
            },
        )
        kb_id = response["knowledgeBase"]["knowledgeBaseId"]
        logger.info("Created Knowledge Base: %s (ID: %s)", KB_NAME, kb_id)
        return kb_id
    except ClientError as e:
        if "already exists" in str(e).lower():
            # List existing KBs to find the ID
            existing = bedrock_agent.list_knowledge_bases()
            for kb in existing.get("knowledgeBaseSummaries", []):
                if kb["name"] == KB_NAME:
                    logger.info("Knowledge Base already exists: %s", kb["knowledgeBaseId"])
                    return kb["knowledgeBaseId"]
        raise


def create_data_source(bedrock_agent, kb_id, bucket_name):
    """Create an S3 data source for the Knowledge Base."""
    try:
        response = bedrock_agent.create_data_source(
            knowledgeBaseId=kb_id,
            name="terraform-modules-s3",
            description="S3 data source with Terraform module definitions",
            dataSourceConfiguration={
                "type": "S3",
                "s3Configuration": {
                    "bucketArn": f"arn:aws:s3:::{bucket_name}",
                    "inclusionPrefixes": ["kb/"],
                },
            },
        )
        ds_id = response["dataSource"]["dataSourceId"]
        logger.info("Created data source: %s", ds_id)
        return ds_id
    except ClientError as e:
        if "already exists" in str(e).lower() or "ConflictException" in str(type(e)):
            # List existing data sources
            existing = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id)
            for ds in existing.get("dataSourceSummaries", []):
                logger.info("Data source already exists: %s", ds["dataSourceId"])
                return ds["dataSourceId"]
        raise


def sync_data_source(bedrock_agent, kb_id, ds_id):
    """Trigger a sync of the data source."""
    response = bedrock_agent.start_ingestion_job(
        knowledgeBaseId=kb_id,
        dataSourceId=ds_id,
    )
    job_id = response["ingestionJob"]["ingestionJobId"]
    logger.info("Started ingestion job: %s", job_id)

    # Poll until complete
    while True:
        status_response = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            ingestionJobId=job_id,
        )
        status = status_response["ingestionJob"]["status"]
        if status == "COMPLETE":
            logger.info("Ingestion complete!")
            break
        elif status in ("FAILED", "STOPPED"):
            logger.error("Ingestion failed: %s", status_response["ingestionJob"])
            sys.exit(1)
        else:
            logger.info("Ingestion status: %s — waiting...", status)
            time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description="Set up Bedrock Knowledge Base for LZA Terraform generator")
    parser.add_argument("--region", default="ca-central-1", help="AWS region")
    parser.add_argument("--profile", default=None, help="AWS CLI profile name")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account_id = get_account_id(session)
    bucket_name = f"lza-terraform-kb-data-{account_id}"

    logger.info("Account: %s | Region: %s | Bucket: %s", account_id, args.region, bucket_name)

    s3 = session.client("s3")
    iam = session.client("iam")
    bedrock_agent = session.client("bedrock-agent")

    # Step 1: S3 bucket
    logger.info("=== Step 1/5: Creating S3 bucket ===")
    create_s3_bucket(s3, bucket_name, args.region)

    # Step 2: Upload KB data
    logger.info("=== Step 2/5: Uploading KB data ===")
    upload_kb_data(s3, bucket_name)

    # Step 3: IAM role
    logger.info("=== Step 3/5: Creating IAM role ===")
    role_arn = create_kb_role(iam, account_id, args.region, bucket_name)

    # Step 4: Knowledge Base
    logger.info("=== Step 4/5: Creating Knowledge Base ===")
    kb_id = create_knowledge_base(bedrock_agent, role_arn, args.region)

    # Step 5: Data source + sync
    logger.info("=== Step 5/5: Creating data source and syncing ===")
    ds_id = create_data_source(bedrock_agent, kb_id, bucket_name)
    sync_data_source(bedrock_agent, kb_id, ds_id)

    # Output
    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    print(f"  Knowledge Base ID : {kb_id}")
    print(f"  S3 Bucket         : {bucket_name}")
    print(f"  IAM Role          : {role_arn}")
    print(f"  Region            : {args.region}")
    print()
    print("Next steps:")
    print(f"  1. Deploy the Lambda:")
    print(f"     cd infrastructure && sam build && sam deploy --guided")
    print(f"  2. Use KNOWLEDGE_BASE_ID={kb_id} as a SAM parameter")
    print(f"  3. Create the Bedrock Agent in the console")
    print("=" * 60)


if __name__ == "__main__":
    main()
