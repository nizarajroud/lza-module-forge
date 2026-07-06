"""
Lambda handler for LZA Terraform code generation.

Receives a request from a Bedrock Agent Action Group, queries a Knowledge Base
for Terraform module definitions, invokes Claude to generate IaC code + README,
and commits both to a GitHub repository.
"""

import json
import logging
import os

from src.bedrock_client import generate_terraform, generate_readme
from src.knowledge_base import retrieve_module_definitions
from src.github_client import commit_file

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration from environment variables
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO_OWNER = os.environ["GITHUB_REPO_OWNER"]
GITHUB_REPO_NAME = os.environ["GITHUB_REPO_NAME"]
KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "ca-central-1")


def lambda_handler(event, context):
    """Main Lambda entry point invoked by Bedrock Agent Action Group."""
    logger.info("Received event: %s", json.dumps(event))

    try:
        # Extract properties from the Bedrock Agent event
        properties = _extract_properties(event)

        account_name = properties["AccountName"]
        customization_name = properties["CustomizationName"]
        aws_services = [s.strip() for s in properties["AwsServices"].split(",")]

        # Directory in the target repo
        directory_path = f"{customization_name}-{account_name}"

        # Step 1: Retrieve relevant module definitions from Knowledge Base
        logger.info("Querying Knowledge Base for services: %s", aws_services)
        module_definitions = retrieve_module_definitions(
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            model_id=BEDROCK_MODEL_ID,
            services=aws_services,
            region=AWS_REGION,
        )

        # Step 2: Generate Terraform configuration
        logger.info("Generating Terraform configuration")
        main_tf_content = generate_terraform(
            services=aws_services,
            module_definitions=module_definitions,
            model_id=BEDROCK_MODEL_ID,
            region=AWS_REGION,
        )

        # Step 3: Generate README
        logger.info("Generating README")
        readme_content = generate_readme(
            services=aws_services,
            terraform_code=main_tf_content,
            model_id=BEDROCK_MODEL_ID,
            region=AWS_REGION,
        )

        # Step 4: Commit both files to GitHub
        main_tf_path = f"{directory_path}/main.tf"
        readme_path = f"{directory_path}/README.md"

        commit_file(
            owner=GITHUB_REPO_OWNER,
            repo=GITHUB_REPO_NAME,
            path=main_tf_path,
            token=GITHUB_TOKEN,
            message=f"feat: add Terraform config for {account_name}",
            content=main_tf_content,
        )

        commit_file(
            owner=GITHUB_REPO_OWNER,
            repo=GITHUB_REPO_NAME,
            path=readme_path,
            token=GITHUB_TOKEN,
            message=f"docs: add README for {account_name}",
            content=readme_content,
        )

        main_tf_url = f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/blob/main/{main_tf_path}"
        readme_url = f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/blob/main/{readme_path}"

        return _build_response(event, 200, {
            "message": f"main.tf and README.md successfully created in {directory_path}/",
            "main_tf_url": main_tf_url,
            "readme_url": readme_url,
        })

    except KeyError as e:
        logger.error("Missing required property: %s", e)
        return _build_response(event, 400, {"error": f"Missing required property: {e}"})
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        return _build_response(event, 500, {"error": str(e)})


def _extract_properties(event: dict) -> dict:
    """Extract properties from the Bedrock Agent Action Group event format."""
    raw_properties = (
        event.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("properties", [])
    )
    return {prop["name"]: prop["value"] for prop in raw_properties}


def _build_response(event: dict, status_code: int, body: dict) -> dict:
    """Build a Bedrock Agent Action Group response."""
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", ""),
            "apiPath": event.get("apiPath", ""),
            "httpMethod": event.get("httpMethod", ""),
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body)
                }
            },
            "sessionAttributes": event.get("sessionAttributes", {}),
            "promptSessionAttributes": event.get("promptSessionAttributes", {}),
        },
    }
