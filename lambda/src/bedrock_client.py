"""
Bedrock model invocation using the Messages API (Claude 3.5+).
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_bedrock_runtime = None


def _get_client(region: str):
    """Lazy-initialize the Bedrock Runtime client."""
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=region)
    return _bedrock_runtime


def _invoke_model(prompt: str, system_prompt: str, model_id: str, region: str) -> str:
    """Invoke a Bedrock model using the Messages API."""
    client = _get_client(region)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8192,
        "temperature": 0,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
        response_body = json.loads(response["body"].read())
        # Messages API returns content as a list of blocks
        text_blocks = [block["text"] for block in response_body["content"] if block["type"] == "text"]
        result = "\n".join(text_blocks).strip()
        logger.info("Model invocation successful (tokens: input=%d, output=%d)",
                    response_body.get("usage", {}).get("input_tokens", 0),
                    response_body.get("usage", {}).get("output_tokens", 0))
        return result
    except ClientError as e:
        logger.error("Bedrock model invocation failed: %s", e)
        raise


def generate_terraform(services: list[str], module_definitions: str, model_id: str, region: str) -> str:
    """Generate Terraform configuration for the requested AWS services."""
    system_prompt = """You are a senior Terraform engineer specializing in AWS Landing Zone configurations.
You generate production-ready Terraform code following these principles:
- Use modules from the organization's registry when available
- Follow least-privilege IAM policies
- Enable encryption at rest and in transit by default
- Include meaningful comments explaining architectural decisions
- Use variables with sensible defaults for environment-specific values
- Output valid HCL that passes `terraform validate`"""

    prompt = f"""Generate a complete Terraform configuration for the following AWS services: {', '.join(services)}

Use these module definitions from our organization's registry:
{module_definitions}

For any service not covered by the module definitions above, create standard Terraform resource blocks.

Requirements:
- Include a `versions.tf` block at the top with required providers
- Use variables for environment-specific values (instance types, CIDR blocks, etc.)
- Add outputs for important resource attributes (ARNs, endpoints, IDs)
- Follow security best practices (encryption, least privilege, private subnets)
- Add comments explaining the architecture and each major resource group"""

    return _invoke_model(prompt, system_prompt, model_id, region)


def generate_readme(services: list[str], terraform_code: str, model_id: str, region: str) -> str:
    """Generate a README documenting the Terraform configuration."""
    system_prompt = """You are a technical writer creating documentation for AWS infrastructure.
Write clear, actionable README files in Markdown format."""

    prompt = f"""Generate a detailed README.md for a Terraform configuration that deploys these AWS services: {', '.join(services)}

The Terraform code is:
```hcl
{terraform_code}
```

Include these sections:
1. **Overview** — What this configuration deploys and the architecture
2. **Prerequisites** — Required tools, permissions, and setup steps
3. **Usage** — How to apply the configuration (init, plan, apply)
4. **Variables** — Table of all input variables with descriptions and defaults
5. **Outputs** — Table of all outputs
6. **Security** — Security controls implemented (encryption, IAM, network isolation)
7. **Cost Estimation** — Approximate monthly cost breakdown by service (use on-demand pricing for ca-central-1)
8. **Well-Architected Review** — How this aligns with AWS Well-Architected pillars"""

    return _invoke_model(prompt, system_prompt, model_id, region)
