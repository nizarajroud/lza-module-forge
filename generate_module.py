"""
Local module generator — executes the same flow as the Lambda but locally.

Usage:
    # Load config from config.env
    cp config.env.example config.env  # fill in values
    python generate_module.py "Bedrock Guardrail"

    # Or with explicit args
    python generate_module.py "Bedrock Guardrail" \
        --output-dir /home/nizar/HomeWspce/poc-bnc-terraform-modules/modules \
        --profile csna-operations-sso-828

Flow:
    1. Query Bedrock Knowledge Base (RAG) for org context + standards
    2. Invoke Claude Sonnet 4.5 to generate the module
    3. Write files locally to OUTPUT_MODULES_DIR
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Add lambda/src to path so we can import the same modules
sys.path.insert(0, str(Path(__file__).parent / "lambda"))

from src.bedrock_client import generate_terraform, generate_readme
from src.knowledge_base import retrieve_module_definitions
from src.local_writer import write_module_locally, parse_multi_file_response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config():
    """Load configuration from config.env if it exists."""
    config_path = Path(__file__).parent / "config.env"
    if config_path.exists():
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def main():
    parser = argparse.ArgumentParser(description="Generate a Terraform module locally using Bedrock RAG")
    parser.add_argument("service", help="Service name to generate module for (e.g., 'Bedrock Guardrail', 'Bedrock Knowledge Base')")
    parser.add_argument("--output-dir", help="Target modules directory (overrides config.env)")
    parser.add_argument("--profile", help="AWS CLI profile (sets AWS_PROFILE)")
    parser.add_argument("--module-prefix", default=None, help="Module directory prefix (default: terraform-aws)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be generated without writing files")
    args = parser.parse_args()

    # Load config
    load_config()

    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile

    output_dir = args.output_dir or os.environ.get("OUTPUT_MODULES_DIR", "/tmp/generated-modules")
    module_prefix = args.module_prefix or os.environ.get("MODULE_PREFIX", "terraform-aws")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    kb_id = os.environ.get("KNOWLEDGE_BASE_ID", "")
    region = os.environ.get("AWS_REGION", "ca-central-1")

    if not kb_id:
        logger.error("KNOWLEDGE_BASE_ID not set. Configure config.env or set the env var.")
        sys.exit(1)

    # Derive module name from service (e.g., "Bedrock Guardrail" → "bedrock-guardrail")
    module_name = args.service.lower().replace(" ", "-")
    services = [s.strip() for s in args.service.split(",")]

    logger.info("=" * 60)
    logger.info("LZA Module Forge — Local Generator")
    logger.info("=" * 60)
    logger.info("  Service      : %s", args.service)
    logger.info("  Module name  : %s-%s", module_prefix, module_name)
    logger.info("  Output dir   : %s", output_dir)
    logger.info("  Model        : %s", model_id)
    logger.info("  KB ID        : %s", kb_id)
    logger.info("  Region       : %s", region)
    logger.info("=" * 60)

    # Step 1: Query Knowledge Base
    logger.info("[Step 1/3] Querying Knowledge Base for org context + module standards...")
    module_definitions = retrieve_module_definitions(
        knowledge_base_id=kb_id,
        model_id=model_id,
        services=services,
        region=region,
    )
    logger.info("  KB returned %d characters of context", len(module_definitions))

    # Step 2: Generate Terraform module
    logger.info("[Step 2/3] Generating Terraform module via Claude...")
    raw_response = generate_terraform(
        services=services,
        module_definitions=module_definitions,
        model_id=model_id,
        region=region,
    )
    logger.info("  Raw response: %d characters", len(raw_response))

    # Parse multi-file response (v2 prompt uses --- FILE: xxx --- delimiters)
    files = parse_multi_file_response(raw_response)

    if not files:
        # Fallback: if no delimiters found, treat entire response as main.tf (v1 behavior)
        logger.warning("  No file delimiters found — falling back to single main.tf")
        files = {"main.tf": raw_response}
    else:
        logger.info("  Parsed %d files: %s", len(files), list(files.keys()))

    # Step 3: Generate README (only if not already in the parsed files)
    if "README.md" not in files:
        logger.info("[Step 3/3] Generating README...")
        readme_content = generate_readme(
            services=services,
            terraform_code=files.get("main.tf", raw_response),
            model_id=model_id,
            region=region,
        )
        files["README.md"] = readme_content
        logger.info("  Generated README.md: %d characters", len(readme_content))
    else:
        logger.info("[Step 3/3] README already included in generated files — skipping")

    if args.dry_run:
        logger.info("\n=== DRY RUN — would write to: %s/%s-%s/ ===", output_dir, module_prefix, module_name)
        for filename, content in files.items():
            logger.info("\n--- %s (%d chars) ---", filename, len(content))
            print(content[:1500])
            if len(content) > 1500:
                print(f"\n... ({len(content) - 1500} more chars)")
        return

    # Write locally
    result = write_module_locally(
        module_name=module_name,
        files=files,
        output_dir=output_dir,
        module_prefix=module_prefix,
    )

    logger.info("")
    logger.info("=" * 60)
    logger.info("MODULE GENERATED SUCCESSFULLY")
    logger.info("=" * 60)
    logger.info("  Path: %s", result["module_path"])
    logger.info("  Files:")
    for filename, filepath in result["files_created"].items():
        logger.info("    - %s", filepath)
    logger.info("")
    logger.info("  Next steps:")
    logger.info("    1. Review the generated code")
    logger.info("    2. cd %s && terraform init && terraform validate", result["module_path"])
    logger.info("    3. git add && git commit -m 'feat: add %s-%s module'", module_prefix, module_name)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
