"""
Terraform Registry Extractor — Extracts module definitions from a Terraform
private registry and produces a KB-ready JSON document.

Compatible with:
- Terraform Cloud / Terraform Enterprise (API v2)
- GitLab Terraform Module Registry (API v4)
- Generic git-based module repos (reads from a manifest file)

This script is GENERIC — it works with any registry that exposes module metadata.
The output JSON follows the same schema as terraform-modules-kb.json and is
designed to be ingested by the Bedrock Knowledge Base alongside LZA config documents.

Usage:
    # Terraform Cloud / Enterprise
    python extract_tf_registry.py --source tfc \
        --endpoint https://app.terraform.io \
        --organization my-org \
        --token $TFC_TOKEN \
        --output-dir ./knowledge-base

    # GitLab Module Registry
    python extract_tf_registry.py --source gitlab \
        --endpoint https://gitlab.example.com \
        --group-id 42 \
        --token $GITLAB_TOKEN \
        --output-dir ./knowledge-base

    # Local manifest file (for orgs without a registry API)
    python extract_tf_registry.py --source manifest \
        --manifest-file ./modules-manifest.yaml \
        --output-dir ./knowledge-base

Output:
    knowledge-base/terraform-modules-kb.json — Module definitions for RAG retrieval
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ─── Terraform Cloud / Enterprise Extractor ─────────────────────────────────

def extract_from_tfc(endpoint: str, organization: str, token: str) -> list[dict]:
    """
    Extract modules from Terraform Cloud / Enterprise Registry API.

    API: GET /api/v2/organizations/:org/registry-modules
    Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/private-registry/modules
    """
    modules = []
    url = f"{endpoint.rstrip('/')}/api/v2/organizations/{organization}/registry-modules?page[size]=100"

    while url:
        data = _api_get(url, headers={"Authorization": f"Bearer {token}"})
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            module = _build_module_entry(
                name=attrs.get("name", ""),
                source=f"{endpoint.rstrip('/')}/{organization}/{attrs.get('name', '')}/{attrs.get('provider', 'aws')}",
                description=attrs.get("description", ""),
                version=attrs.get("version-statuses", [{}])[0].get("version", "latest") if attrs.get("version-statuses") else "latest",
                registry_type="terraform-cloud",
            )
            modules.append(module)
            logger.info("  Found module: %s (v%s)", module["ModuleName"], module["LatestVersion"])

        # Pagination
        url = data.get("links", {}).get("next")

    return modules


# ─── GitLab Module Registry Extractor ───────────────────────────────────────

def extract_from_gitlab(endpoint: str, group_id: str, token: str) -> list[dict]:
    """
    Extract modules from GitLab Terraform Module Registry.

    API: GET /api/v4/groups/:id/packages?package_type=terraform_module
    Docs: https://docs.gitlab.com/ee/user/packages/terraform_module_registry/
    """
    modules = []
    url = f"{endpoint.rstrip('/')}/api/v4/groups/{group_id}/packages?package_type=terraform_module&per_page=100"

    page = 1
    while url:
        data = _api_get(url, headers={"PRIVATE-TOKEN": token})
        if not data:
            break

        for pkg in data:
            name = pkg.get("name", "")
            version = pkg.get("version", "latest")
            # GitLab module source format
            source = f"{endpoint.rstrip('/')}/{name}"

            module = _build_module_entry(
                name=name.split("/")[-1] if "/" in name else name,
                source=source,
                description=f"Terraform module: {name}",
                version=version,
                registry_type="gitlab",
            )
            modules.append(module)
            logger.info("  Found module: %s (v%s)", module["ModuleName"], module["LatestVersion"])

        # GitLab uses Link header for pagination
        if len(data) < 100:
            break
        page += 1
        url = f"{endpoint.rstrip('/')}/api/v4/groups/{group_id}/packages?package_type=terraform_module&per_page=100&page={page}"

    return modules


# ─── Manifest File Extractor (for orgs without API) ─────────────────────────

def extract_from_manifest(manifest_file: str) -> list[dict]:
    """
    Extract modules from a local YAML manifest file.

    This is for organizations that don't have a registry API but maintain
    an internal list of approved modules. The manifest format:

    modules:
      - name: vpc
        source: "git::https://gitlab.example.com/tf-modules/vpc.git?ref=v2.3.0"
        description: "Creates a VPC with standard subnet tiers"
        version: "2.3.0"
        required_parameters: [cidr_block, environment, project_name]
        optional_parameters: [enable_flow_logs, enable_dns_hostnames]
        dependencies: []
        best_practices: "Always use private subnets for workloads. Attach to TGW."
        security_notes: "Enable VPC flow logs. Use NACLs for network segmentation."
        outputs: [vpc_id, private_subnet_ids, public_subnet_ids]
    """
    filepath = Path(manifest_file)
    if not filepath.exists():
        logger.error("Manifest file not found: %s", manifest_file)
        sys.exit(1)

    with open(filepath) as f:
        manifest = yaml.safe_load(f)

    modules = []
    for entry in manifest.get("modules", []):
        module = {
            "ModuleName": entry["name"],
            "ModuleSource": entry.get("source", ""),
            "Description": entry.get("description", ""),
            "LatestVersion": entry.get("version", "latest"),
            "RequiredParameters": entry.get("required_parameters", []),
            "OptionalParameters": entry.get("optional_parameters", []),
            "Dependencies": entry.get("dependencies", []),
            "BestPractices": entry.get("best_practices", ""),
            "SecurityNotes": entry.get("security_notes", ""),
            "Outputs": entry.get("outputs", []),
            "RegistryType": "manifest",
        }
        modules.append(module)
        logger.info("  Found module: %s (v%s)", module["ModuleName"], module["LatestVersion"])

    return modules


# ─── Helpers ────────────────────────────────────────────────────────────────

def _api_get(url: str, headers: dict) -> dict | list:
    """Make a GET request to a registry API."""
    req = Request(url, headers={**headers, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logger.error("API error %d from %s: %s", e.code, url, body[:200])
        return {} if e.code != 404 else {}


def _build_module_entry(name: str, source: str, description: str, version: str, registry_type: str) -> dict:
    """Build a standardized module entry for the KB."""
    return {
        "ModuleName": name,
        "ModuleSource": source,
        "Description": description,
        "LatestVersion": version,
        "RequiredParameters": [],  # Would need per-module API call to get these
        "OptionalParameters": [],
        "Dependencies": [],
        "BestPractices": "",
        "SecurityNotes": "",
        "Outputs": [],
        "RegistryType": registry_type,
    }


def write_output(modules: list[dict], output_dir: str):
    """Write the modules to the KB-ready JSON file."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    filepath = output_path / "terraform-modules-kb.json"

    document = {
        "documentType": "terraform-module-registry",
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "totalModules": len(modules),
        "description": (
            "Organization-approved Terraform modules. When generating IaC, "
            "prefer these modules over raw resource blocks. Use the ModuleSource "
            "as the 'source' argument in module blocks."
        ),
        "TerraformModules": modules,
    }

    with open(filepath, "w") as f:
        json.dump(document, f, indent=2)
    logger.info("Written: %s (%d modules, %d bytes)", filepath, len(modules), filepath.stat().st_size)


# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract Terraform module definitions from a private registry into KB-ready JSON"
    )
    parser.add_argument(
        "--source", required=True, choices=["tfc", "gitlab", "manifest"],
        help="Registry source type: tfc (Terraform Cloud/Enterprise), gitlab, or manifest (local YAML)"
    )
    parser.add_argument("--endpoint", help="Registry API endpoint URL (for tfc/gitlab)")
    parser.add_argument("--organization", help="Organization name (for tfc)")
    parser.add_argument("--group-id", help="GitLab group ID (for gitlab)")
    parser.add_argument("--token", help="API token (for tfc/gitlab)")
    parser.add_argument("--manifest-file", help="Path to manifest YAML (for manifest source)")
    parser.add_argument(
        "--output-dir", default=None,
        help="Output directory for JSON (default: ./knowledge-base/)"
    )
    args = parser.parse_args()

    if args.output_dir is None:
        args.output_dir = str(Path(__file__).parent / "knowledge-base")

    logger.info("Extracting modules from: %s", args.source)

    if args.source == "tfc":
        if not all([args.endpoint, args.organization, args.token]):
            parser.error("--endpoint, --organization, and --token are required for tfc source")
        modules = extract_from_tfc(args.endpoint, args.organization, args.token)

    elif args.source == "gitlab":
        if not all([args.endpoint, args.group_id, args.token]):
            parser.error("--endpoint, --group-id, and --token are required for gitlab source")
        modules = extract_from_gitlab(args.endpoint, args.group_id, args.token)

    elif args.source == "manifest":
        if not args.manifest_file:
            parser.error("--manifest-file is required for manifest source")
        modules = extract_from_manifest(args.manifest_file)

    if not modules:
        logger.warning("No modules found!")
        sys.exit(1)

    write_output(modules, args.output_dir)
    logger.info("=== Extraction complete: %d modules ===", len(modules))


if __name__ == "__main__":
    main()
