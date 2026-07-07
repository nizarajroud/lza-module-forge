"""
Local file writer — writes generated Terraform modules to the filesystem.

Replaces github_client.py when OUTPUT_MODE=local.
Generates the full module directory structure expected by the org standards.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def write_module_locally(
    module_name: str,
    files: dict[str, str],
    output_dir: str = None,
    module_prefix: str = "terraform-aws",
) -> dict:
    """
    Write generated module files to the local filesystem.

    Args:
        module_name: Service name (e.g., "bedrock-kb", "vpc-endpoints-ai")
        files: Dict of filename → content (e.g., {"main.tf": "...", "variables.tf": "..."})
        output_dir: Absolute path to the modules directory
        module_prefix: Prefix for the module directory name

    Returns:
        Dict with paths of created files
    """
    if output_dir is None:
        output_dir = os.environ.get("OUTPUT_MODULES_DIR", "/tmp/generated-modules")

    # Build the full module directory path
    module_dir_name = f"{module_prefix}-{module_name}"
    module_path = Path(output_dir) / module_dir_name

    # Create directory structure
    module_path.mkdir(parents=True, exist_ok=True)
    (module_path / "examples" / "complete").mkdir(parents=True, exist_ok=True)
    (module_path / "tests").mkdir(parents=True, exist_ok=True)

    created_files = {}

    for filename, content in files.items():
        # Handle nested paths (e.g., "examples/complete/main.tf")
        file_path = module_path / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)

        file_path.write_text(content, encoding="utf-8")
        created_files[filename] = str(file_path)
        logger.info("Written: %s", file_path)

    logger.info(
        "Module '%s' written to: %s (%d files)",
        module_dir_name,
        module_path,
        len(created_files),
    )

    return {
        "module_name": module_dir_name,
        "module_path": str(module_path),
        "files_created": created_files,
    }
