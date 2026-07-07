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
    """Lazy-initialize the Bedrock Runtime client with extended timeout."""
    global _bedrock_runtime
    if _bedrock_runtime is None:
        from botocore.config import Config
        config = Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 2})
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=region, config=config)
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
    system_prompt = """You are a senior Terraform engineer specializing in AWS Landing Zone Accelerator (LZA) configurations.
You generate production-ready Terraform code that is FULLY ALIGNED with the organization's deployed LZA environment.

CRITICAL RULES:
- NEVER use placeholder values. Use ONLY real values from the organization context provided.
- Default region is ALWAYS ca-central-1 (the organization's home region). NEVER use us-east-1.
- Use the REAL CIDR blocks from the network topology (10.x.x.x ranges provided in context).
- Use the REAL subnet tier names: Web, App, Data, Mgmt, TgwAttach.
- All resources MUST be encrypted (EBS, S3, RDS) — this is enforced by SCPs.
- EC2 instances MUST use IMDSv2 (enforced by Config rules).
- S3 buckets MUST block public access and enforce HTTPS (enforced by SCPs).
- RDS MUST use storage encryption (enforced by SCPs).
- Reference existing KMS keys, S3 buckets, and IAM policies from the organization when available.
- Include the organization's mandatory tags: ClientName, ClientProjectName, CostCenter, MaintainersTeam.
- Use the organization's module sources when provided. Otherwise use standard resource blocks.
- NEVER create VPCs in Sandbox accounts (blocked by SCP). Reference shared VPC subnets instead.
- Stay within the allowed regions (ca-central-1 primary, us-east-1 for global services only).

OUTPUT FORMAT:
- Output ONLY valid HCL code. No markdown fences, no explanations.
- Start with terraform {} block with required providers.
- Include variables with REAL default values from the organization context.
- Add comments explaining architectural decisions."""

    prompt = f"""Generate a complete Terraform configuration for these AWS services: {', '.join(services)}

ORGANIZATION CONTEXT (from deployed LZA configuration — use these REAL values):
{module_definitions}

REQUIREMENTS:
1. Region: ca-central-1 (hardcode as default, this is the org's home region)
2. Use the REAL CIDR blocks from the context above for any network references
3. Use data sources to reference existing shared VPCs/subnets when in Sandbox accounts
4. All encryption enabled by default (SCP-enforced — deployment will fail without it)
5. Include the organization's mandatory tags in the provider default_tags block:
   - ClientName = "Alithya"
   - MaintainersTeam = "IO"
   - ManagedBy = "Terraform"
   - Environment = var.environment
6. Use organization module sources if provided in the context. Otherwise create resource blocks.
7. Include outputs for important resource attributes (ARNs, endpoints, IDs)
8. Add variables with sensible defaults derived from the organization context"""

    return _invoke_model(prompt, system_prompt, model_id, region)


def generate_readme(services: list[str], terraform_code: str, model_id: str, region: str) -> str:
    """Generate a README documenting the Terraform configuration."""
    system_prompt = """You are a technical writer creating documentation for AWS infrastructure deployed in a Landing Zone Accelerator environment.
Write clear, actionable README files in Markdown format. Reference the organization's specific setup (ca-central-1 region, Alithya org conventions)."""

    prompt = f"""Generate a detailed README.md for a Terraform configuration that deploys these AWS services: {', '.join(services)}

The Terraform code is:
```hcl
{terraform_code}
```

Include these sections:
1. **Overview** — What this configuration deploys and the architecture
2. **Prerequisites** — Required tools, permissions, and setup steps (mention LZA account setup)
3. **Usage** — How to apply the configuration (init, plan, apply) in ca-central-1
4. **Variables** — Table of all input variables with descriptions and defaults
5. **Outputs** — Table of all outputs
6. **Security** — Security controls implemented (encryption, IAM, network isolation) and how they align with LZA SCPs
7. **Cost Estimation** — Approximate monthly cost breakdown by service (use on-demand pricing for ca-central-1)
8. **Well-Architected Review** — How this aligns with AWS Well-Architected pillars
9. **Compliance** — How this configuration satisfies the organization's SCP requirements"""

    return _invoke_model(prompt, system_prompt, model_id, region)
