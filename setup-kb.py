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


AOSS_COLLECTION_NAME = "lza-terraform-kb"
AOSS_INDEX_NAME = "bedrock-knowledge-base-default-index"


def create_opensearch_collection(session, account_id, region):
    """Create an OpenSearch Serverless collection for the KB vector store."""
    aoss = session.client("opensearchserverless")

    # Create encryption policy
    enc_policy_name = "lza-terraform-kb-enc"
    try:
        aoss.create_security_policy(
            name=enc_policy_name,
            type="encryption",
            policy=json.dumps({
                "Rules": [{"ResourceType": "collection", "Resource": [f"collection/{AOSS_COLLECTION_NAME}"]}],
                "AWSOwnedKey": True,
            }),
        )
        logger.info("Created encryption policy: %s", enc_policy_name)
    except ClientError as e:
        if "ConflictException" in str(type(e).__name__) or "already exists" in str(e).lower() or e.response["Error"]["Code"] == "ConflictException":
            logger.info("Encryption policy already exists")
        else:
            raise

    # Create network policy (allow public access for Bedrock)
    net_policy_name = "lza-terraform-kb-net"
    try:
        aoss.create_security_policy(
            name=net_policy_name,
            type="network",
            policy=json.dumps([{
                "Rules": [
                    {"ResourceType": "collection", "Resource": [f"collection/{AOSS_COLLECTION_NAME}"]},
                    {"ResourceType": "dashboard", "Resource": [f"collection/{AOSS_COLLECTION_NAME}"]},
                ],
                "AllowFromPublic": True,
            }]),
        )
        logger.info("Created network policy: %s", net_policy_name)
    except ClientError as e:
        if "ConflictException" in str(type(e).__name__) or "already exists" in str(e).lower() or e.response["Error"]["Code"] == "ConflictException":
            logger.info("Network policy already exists")
        else:
            raise

    # Create data access policy
    dap_name = "lza-terraform-kb-dap"
    principal_arn = f"arn:aws:iam::{account_id}:role/{KB_ROLE_NAME}"
    caller_arn = session.client("sts").get_caller_identity()["Arn"]
    try:
        aoss.create_access_policy(
            name=dap_name,
            type="data",
            policy=json.dumps([{
                "Rules": [
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{AOSS_COLLECTION_NAME}"],
                        "Permission": ["aoss:CreateCollectionItems", "aoss:DeleteCollectionItems",
                                       "aoss:UpdateCollectionItems", "aoss:DescribeCollectionItems"],
                    },
                    {
                        "ResourceType": "index",
                        "Resource": [f"index/{AOSS_COLLECTION_NAME}/*"],
                        "Permission": ["aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex",
                                       "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument"],
                    },
                ],
                "Principal": [principal_arn, caller_arn],
            }]),
        )
        logger.info("Created data access policy: %s", dap_name)
    except ClientError as e:
        if "ConflictException" in str(type(e).__name__) or "already exists" in str(e).lower() or e.response["Error"]["Code"] == "ConflictException":
            logger.info("Data access policy already exists")
        else:
            raise

    # Create the collection
    try:
        response = aoss.create_collection(
            name=AOSS_COLLECTION_NAME,
            type="VECTORSEARCH",
            description="Vector store for LZA Terraform KB",
        )
        collection_id = response["createCollectionDetail"]["id"]
        logger.info("Created AOSS collection: %s (ID: %s)", AOSS_COLLECTION_NAME, collection_id)
    except ClientError as e:
        if "ConflictException" in str(type(e).__name__) or "already exists" in str(e).lower() or e.response["Error"]["Code"] == "ConflictException":
            # Get existing collection
            batch = aoss.batch_get_collection(names=[AOSS_COLLECTION_NAME])
            collection_id = batch["collectionDetails"][0]["id"]
            logger.info("Collection already exists: %s", collection_id)
        else:
            raise

    # Wait for collection to become ACTIVE
    collection_arn = f"arn:aws:aoss:{region}:{account_id}:collection/{collection_id}"
    logger.info("Waiting for collection to become ACTIVE...")
    while True:
        batch = aoss.batch_get_collection(ids=[collection_id])
        status = batch["collectionDetails"][0]["status"]
        if status == "ACTIVE":
            logger.info("Collection is ACTIVE")
            break
        elif status in ("FAILED",):
            logger.error("Collection creation failed!")
            sys.exit(1)
        else:
            logger.info("Collection status: %s — waiting...", status)
            time.sleep(15)

    # Wait for data access policy to propagate
    endpoint = batch["collectionDetails"][0]["collectionEndpoint"]
    logger.info("Waiting 30s for data access policy propagation...")
    time.sleep(30)

    # Create the vector index via the OpenSearch API (with retries)
    _create_vector_index(endpoint, region, session)

    return collection_arn


def _create_vector_index(endpoint, region, session):
    """Create the vector index in the OpenSearch Serverless collection (with retries)."""
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    index_url = f"{endpoint}/{AOSS_INDEX_NAME}"
    credentials = session.get_credentials().get_frozen_credentials()

    # Check if index exists
    check_req = AWSRequest(method="HEAD", url=index_url)
    SigV4Auth(credentials, "aoss", region).add_auth(check_req)
    try:
        req = Request(index_url, method="HEAD", headers=dict(check_req.headers))
        with urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                logger.info("Vector index already exists: %s", AOSS_INDEX_NAME)
                return
    except HTTPError as e:
        if e.code != 404:
            logger.info("Index HEAD check returned %d — will attempt creation", e.code)
    except Exception:
        pass

    # Create index with retries (data access policy can take up to 60s)
    index_body = json.dumps({
        "settings": {
            "index": {"knn": True, "knn.algo_param.ef_search": 512}
        },
        "mappings": {
            "properties": {
                "bedrock-knowledge-base-default-vector": {
                    "type": "knn_vector",
                    "dimension": 1024,
                    "method": {"engine": "faiss", "space_type": "l2", "name": "hnsw"},
                },
                "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
                "AMAZON_BEDROCK_METADATA": {"type": "text"},
            }
        },
    }).encode("utf-8")

    max_retries = 5
    for attempt in range(1, max_retries + 1):
        aws_request = AWSRequest(
            method="PUT",
            url=index_url,
            data=index_body,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(credentials, "aoss", region).add_auth(aws_request)

        try:
            req = Request(index_url, data=index_body, method="PUT", headers=dict(aws_request.headers))
            with urlopen(req, timeout=60) as resp:
                logger.info("Created vector index: %s (status: %d)", AOSS_INDEX_NAME, resp.status)
                return
        except HTTPError as e:
            if e.code in (401, 403) and attempt < max_retries:
                logger.info("Index creation attempt %d/%d got %d — retrying in 15s (policy propagation)...",
                            attempt, max_retries, e.code)
                time.sleep(15)
            else:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                logger.error("Index creation failed after %d attempts: %d - %s", attempt, e.code, body)
                raise
        except Exception as e:
            if attempt < max_retries:
                logger.info("Index creation attempt %d/%d failed: %s — retrying...", attempt, max_retries, e)
                time.sleep(15)
            else:
                raise





def create_knowledge_base(bedrock_agent, collection_arn, role_arn, region):
    """Create the Bedrock Knowledge Base pointing to the AOSS collection."""
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
                    "collectionArn": collection_arn,
                    "vectorIndexName": AOSS_INDEX_NAME,
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
        if "already exists" in str(e).lower() or "ConflictException" in str(e):
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
    logger.info("=== Step 3/6: Creating IAM role ===")
    role_arn = create_kb_role(iam, account_id, args.region, bucket_name)

    # Step 4: OpenSearch Serverless collection
    logger.info("=== Step 4/6: Creating OpenSearch Serverless collection ===")
    collection_arn = create_opensearch_collection(session, account_id, args.region)

    # Step 5: Knowledge Base
    logger.info("=== Step 5/6: Creating Knowledge Base ===")
    kb_id = create_knowledge_base(bedrock_agent, collection_arn, role_arn, args.region)

    # Step 6: Data source + sync
    logger.info("=== Step 6/6: Creating data source and syncing ===")
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
