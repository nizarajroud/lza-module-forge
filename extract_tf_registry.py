"""
Terraform Registry Extractor — Extracts module definitions from a Terraform
private registry OR local module directories and produces a KB-ready JSON document.

Compatible with:
- Local module directories (parses .tf files directly) ← NEW in v2
- Terraform Cloud / Terraform Enterprise (API v2)
- GitLab Terraform Module Registry (API v4)
- Generic git-based module repos (reads from a manifest file)

v2 additions:
- --source local: reads modules from a local directory, parses variables.tf,
  outputs.tf, main.tf, versions.tf, README.md, and steering/*.md
- Extracts: variables (with types, defaults, validations), outputs, resources,
  provider constraints, examples, best practices, security notes
- Also ingests steering files as "module standards" documents

Usage:
    # Local module directory (parses .tf files)
    python extract_tf_registry.py --source local \
        --modules-dir /path/to/poc-bnc-terraform-modules \
        --output-dir ./knowledge-base

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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ─── Local Module Directory Extractor (v2) ──────────────────────────────────

def extract_from_local(modules_dir: str) -> tuple[list[dict], list[dict]]:
    """
    Extract modules from a local directory by parsing .tf and .md files.

    Scans for directories matching `modules/terraform-aws-*` pattern,
    then parses each module's variables.tf, outputs.tf, main.tf, versions.tf,
    and README.md to produce structured KB entries.

    Also reads steering/*.md files as "module standards" documents.

    Returns:
        (modules, standards) — list of module dicts + list of standards dicts
    """
    modules_path = Path(modules_dir)
    modules = []
    standards = []

    # ─── Parse modules ───────────────────────────────────────────────────
    modules_subdir = modules_path / "modules"
    if modules_subdir.exists():
        for module_dir in sorted(modules_subdir.iterdir()):
            if not module_dir.is_dir() or not module_dir.name.startswith("terraform-aws-"):
                continue
            logger.info("  Parsing module: %s", module_dir.name)
            module = _parse_module_directory(module_dir, modules_dir)
            modules.append(module)

    # ─── Parse steering files as standards ───────────────────────────────
    steering_dir = modules_path / "steering"
    if steering_dir.exists():
        for md_file in sorted(steering_dir.glob("*.md")):
            logger.info("  Parsing standard: %s", md_file.name)
            standards.append({
                "documentType": "module-standard",
                "name": md_file.stem,
                "filename": md_file.name,
                "content": md_file.read_text(encoding="utf-8"),
            })

    return modules, standards


def _parse_module_directory(module_dir: Path, repo_root: str) -> dict:
    """Parse a single module directory and extract structured metadata."""
    module_name = module_dir.name

    # Parse variables.tf
    variables = _parse_variables(module_dir / "variables.tf")

    # Parse outputs.tf
    outputs = _parse_outputs(module_dir / "outputs.tf")

    # Parse main.tf — extract resource types
    resources = _parse_resources(module_dir / "main.tf")

    # Parse versions.tf — extract provider constraints
    provider_constraints = _parse_versions(module_dir / "versions.tf")

    # Read README.md
    readme_content = ""
    readme_path = module_dir / "README.md"
    if readme_path.exists():
        readme_content = readme_path.read_text(encoding="utf-8")

    # Read example if exists
    example_content = ""
    example_path = module_dir / "examples" / "complete" / "main.tf"
    if example_path.exists():
        example_content = example_path.read_text(encoding="utf-8")

    # Build source path
    relative_path = module_dir.relative_to(Path(repo_root))
    source = f"github.com/nizarajroud/poc-bnc-terraform-modules//{relative_path}"

    return {
        "ModuleName": module_name,
        "ModuleSource": source,
        "Description": _extract_description_from_readme(readme_content),
        "LatestVersion": "latest",
        "Variables": variables,
        "RequiredParameters": [v["name"] for v in variables if v.get("required")],
        "OptionalParameters": [v["name"] for v in variables if not v.get("required")],
        "Outputs": outputs,
        "Resources": resources,
        "ProviderConstraints": provider_constraints,
        "Example": example_content,
        "README": readme_content,
        "BestPractices": "",
        "SecurityNotes": "",
        "RegistryType": "local",
    }


def _parse_variables(filepath: Path) -> list[dict]:
    """Parse variables.tf and extract variable definitions."""
    if not filepath.exists():
        return []

    content = filepath.read_text(encoding="utf-8")
    variables = []
    # Regex to match variable blocks
    var_pattern = re.compile(
        r'variable\s+"(\w+)"\s*\{(.*?)\n\}',
        re.DOTALL,
    )

    for match in var_pattern.finditer(content):
        var_name = match.group(1)
        var_body = match.group(2)

        var_info = {"name": var_name}

        # Extract description
        desc_match = re.search(r'description\s*=\s*"([^"]*)"', var_body)
        if desc_match:
            var_info["description"] = desc_match.group(1)

        # Extract type
        type_match = re.search(r'type\s*=\s*(\S+)', var_body)
        if type_match:
            var_info["type"] = type_match.group(1)

        # Check if has default (= not required)
        has_default = "default" in var_body and re.search(r'^\s*default\s*=', var_body, re.MULTILINE)
        var_info["required"] = not has_default

        # Extract default value (simple cases)
        default_match = re.search(r'default\s*=\s*(".*?"|true|false|null|\d+|\[\]|\{\})', var_body)
        if default_match:
            var_info["default"] = default_match.group(1)

        # Check if has validation
        var_info["has_validation"] = "validation {" in var_body or "validation{" in var_body

        variables.append(var_info)

    return variables


def _parse_outputs(filepath: Path) -> list[dict]:
    """Parse outputs.tf and extract output definitions."""
    if not filepath.exists():
        return []

    content = filepath.read_text(encoding="utf-8")
    outputs = []

    out_pattern = re.compile(
        r'output\s+"(\w+)"\s*\{(.*?)\n\}',
        re.DOTALL,
    )

    for match in out_pattern.finditer(content):
        out_name = match.group(1)
        out_body = match.group(2)

        out_info = {"name": out_name}

        desc_match = re.search(r'description\s*=\s*"([^"]*)"', out_body)
        if desc_match:
            out_info["description"] = desc_match.group(1)

        value_match = re.search(r'value\s*=\s*(.*)', out_body)
        if value_match:
            out_info["value_expression"] = value_match.group(1).strip()

        outputs.append(out_info)

    return outputs


def _parse_resources(filepath: Path) -> list[dict]:
    """Parse main.tf and extract resource types and names."""
    if not filepath.exists():
        return []

    content = filepath.read_text(encoding="utf-8")
    resources = []

    res_pattern = re.compile(r'resource\s+"(\w+)"\s+"(\w+)"')
    for match in res_pattern.finditer(content):
        resources.append({
            "type": match.group(1),
            "name": match.group(2),
        })

    return resources


def _parse_versions(filepath: Path) -> dict:
    """Parse versions.tf and extract provider/terraform constraints."""
    if not filepath.exists():
        return {}

    content = filepath.read_text(encoding="utf-8")
    constraints = {}

    tf_version = re.search(r'required_version\s*=\s*"([^"]*)"', content)
    if tf_version:
        constraints["terraform"] = tf_version.group(1)

    provider_version = re.search(r'version\s*=\s*"([^"]*)"', content)
    if provider_version:
        constraints["aws_provider"] = provider_version.group(1)

    return constraints


def _extract_description_from_readme(readme: str) -> str:
    """Extract the first paragraph after the title as description."""
    lines = readme.strip().split("\n")
    # Skip title line (# ...)
    desc_lines = []
    started = False
    for line in lines[1:]:
        stripped = line.strip()
        if not started and stripped:
            started = True
        if started:
            if stripped.startswith("#") or stripped.startswith("|"):
                break
            if stripped:
                desc_lines.append(stripped)
            else:
                break
    return " ".join(desc_lines)


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


def write_output(modules: list[dict], output_dir: str, standards: list[dict] = None):
    """Write the modules and standards to KB-ready JSON files."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Write modules
    filepath = output_path / "terraform-modules-kb.json"
    document = {
        "documentType": "terraform-module-registry",
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "totalModules": len(modules),
        "description": (
            "Organization-approved Terraform modules. When generating a NEW module, "
            "follow the exact same structure, naming conventions, variable patterns, "
            "validation blocks, and security practices as these existing modules. "
            "Use them as the REFERENCE PATTERN to replicate."
        ),
        "TerraformModules": modules,
    }

    with open(filepath, "w") as f:
        json.dump(document, f, indent=2)
    logger.info("Written: %s (%d modules, %d bytes)", filepath, len(modules), filepath.stat().st_size)

    # Write standards (if provided)
    if standards:
        standards_filepath = output_path / "terraform-module-standards-kb.json"
        standards_doc = {
            "documentType": "terraform-module-standards",
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "totalDocuments": len(standards),
            "description": (
                "Organization standards for Terraform module development. "
                "ALL generated modules MUST comply with these standards. "
                "These define: file structure, naming conventions, variable patterns, "
                "security requirements, testing discipline, and versioning rules."
            ),
            "standards": standards,
        }
        with open(standards_filepath, "w") as f:
            json.dump(standards_doc, f, indent=2)
        logger.info("Written: %s (%d standards, %d bytes)",
                    standards_filepath, len(standards), standards_filepath.stat().st_size)


# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract Terraform module definitions from a private registry or local modules into KB-ready JSON"
    )
    parser.add_argument(
        "--source", required=True, choices=["local", "tfc", "gitlab", "manifest"],
        help="Registry source type: local (parse .tf files), tfc (Terraform Cloud/Enterprise), gitlab, or manifest (local YAML)"
    )
    parser.add_argument("--modules-dir", help="Path to the modules repository root (for local source)")
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

    standards = None

    if args.source == "local":
        if not args.modules_dir:
            parser.error("--modules-dir is required for local source")
        modules, standards = extract_from_local(args.modules_dir)

    elif args.source == "tfc":
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

    write_output(modules, args.output_dir, standards)
    logger.info("=== Extraction complete: %d modules ===", len(modules))
    if standards:
        logger.info("=== Standards extracted: %d documents ===", len(standards))


if __name__ == "__main__":
    main()
