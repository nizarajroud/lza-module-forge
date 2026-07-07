"""
Bedrock model invocation using the Messages API (Claude 3.5+).
"""

import json
import logging
import os
from pathlib import Path

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



def _load_prompt(prompt_file: Path) -> tuple[str, str]:
    """
    Load system prompt and user prompt template from a markdown file.

    Expected format in the .md file:
        ## System Prompt
        ```
        <system prompt content>
        ```

        ## User Prompt
        ```
        <user prompt template with {services} and {module_definitions} placeholders>
        ```
    """
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

    content = prompt_file.read_text(encoding="utf-8")

    # Extract system prompt (between first ``` pair after "## System Prompt")
    system_prompt = _extract_code_block(content, "## System Prompt")
    user_prompt = _extract_code_block(content, "## User Prompt")

    if not system_prompt or not user_prompt:
        raise ValueError(f"Could not parse system/user prompts from {prompt_file}")

    return system_prompt, user_prompt


def _extract_code_block(content: str, section_header: str) -> str:
    """Extract the content of the first ``` code block after a section header."""
    import re
    # Find the section
    section_idx = content.find(section_header)
    if section_idx == -1:
        return ""

    # Find the first ``` block after the section header
    remaining = content[section_idx:]
    match = re.search(r'```\n?(.*?)```', remaining, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""

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
    # Load prompts from external files
    prompt_version = os.environ.get("PROMPT_VERSION", "v2")
    prompts_dir = Path(__file__).parent.parent.parent / "prompts"
    system_prompt, user_prompt_template = _load_prompt(prompts_dir / f"{prompt_version}.md")

    prompt = user_prompt_template.replace("{services}", ', '.join(services)).replace("{module_definitions}", module_definitions)

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
