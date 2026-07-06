"""
Bedrock Knowledge Base retrieval for Terraform module definitions.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_bedrock_agent = None


def _get_client(region: str):
    """Lazy-initialize the Bedrock Agent Runtime client."""
    global _bedrock_agent
    if _bedrock_agent is None:
        _bedrock_agent = boto3.client("bedrock-agent-runtime", region_name=region)
    return _bedrock_agent


def retrieve_module_definitions(
    knowledge_base_id: str,
    model_id: str,
    services: list[str],
    region: str,
) -> str:
    """
    Query the Bedrock Knowledge Base for Terraform module definitions
    matching the requested AWS services.

    Uses RetrieveAndGenerate to get a synthesized response with module
    sources, parameters, and best practices.

    Note: Uses Claude 3 Sonnet (direct foundation model) for KB queries
    as it's faster and sufficient for retrieval synthesis.
    """
    client = _get_client(region)

    query_text = (
        f"Retrieve Terraform module definitions, sources, required parameters, "
        f"dependencies, and security best practices for these AWS services: "
        f"{', '.join(services)}"
    )

    # Use Claude 3 Sonnet directly for KB queries — faster than cross-region inference profiles
    kb_model_arn = f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0"

    try:
        response = client.retrieve_and_generate(
            input={"text": query_text},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": knowledge_base_id,
                    "modelArn": kb_model_arn,
                },
            },
        )

        output_text = response.get("output", {}).get("text", "")
        citations = response.get("citations", [])

        logger.info(
            "Knowledge Base query successful: %d characters, %d citations",
            len(output_text),
            len(citations),
        )

        return output_text

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        logger.error("Knowledge Base query failed [%s]: %s", error_code, e)
        raise
    except Exception as e:
        logger.error("Unexpected error querying Knowledge Base: %s", e)
        raise
