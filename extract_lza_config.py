"""
LZA Config Extractor — Parses AWS Landing Zone Accelerator configuration files
and produces KB-ready JSON documents for the Bedrock Knowledge Base.

Usage:
    python extract_lza_config.py /path/to/lza-config-repo [--output-dir ./knowledge-base]

Produces 7 JSON files optimized for RAG retrieval:
1. org-accounts.json        — Organization structure, accounts, OUs
2. network-topology.json    — VPCs, subnets, CIDRs, transit gateways
3. security-constraints.json — SCPs (raw + summarized), Config rules
4. iam-available.json       — Roles, policies, permission sets, boundaries
5. existing-resources.json  — Already-deployed customizations
6. naming-conventions.json  — Patterns extracted from config
7. terraform-modules-kb.json — (Existing, enriched with LZA context)
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class LZAConfigExtractor:
    """Extracts structured KB documents from LZA configuration files."""

    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.replacements = self._load_replacements()
        self.accounts = self._load_yaml("accounts-config.yaml")
        self.organization = self._load_yaml("organization-config.yaml")
        self.network = self._load_yaml("network-config.yaml")
        self.security = self._load_yaml("security-config.yaml")
        self.iam = self._load_yaml("iam-config.yaml")
        self.customizations = self._load_yaml("customizations-config.yaml")

    def _load_yaml(self, filename: str) -> dict:
        """Load and parse a YAML file, resolving replacement variables."""
        filepath = self.config_dir / filename
        if not filepath.exists():
            logger.warning("File not found: %s", filepath)
            return {}
        with open(filepath) as f:
            raw = f.read()
        # Resolve {{ Variable }} replacements
        resolved = self._resolve_replacements(raw)
        # Remove duplicate YAML anchors (common in LZA configs)
        resolved = self._deduplicate_anchors(resolved)
        try:
            return yaml.safe_load(resolved) or {}
        except yaml.YAMLError as e:
            logger.error("YAML parse error in %s: %s", filename, e)
            return {}

    def _deduplicate_anchors(self, text: str) -> str:
        """Remove duplicate YAML anchor definitions (keep first occurrence only)."""
        seen_anchors = set()
        lines = text.split("\n")
        result = []
        for line in lines:
            match = re.search(r"&(\w+)\s", line)
            if match:
                anchor = match.group(1)
                if anchor in seen_anchors:
                    # Replace the anchor definition with just the value
                    line = re.sub(r"\s*&\w+\s*", " ", line)
                else:
                    seen_anchors.add(anchor)
            result.append(line)
        return "\n".join(result)

    def _load_replacements(self) -> dict:
        """Load the replacements config and build a lookup dict."""
        filepath = self.config_dir / "replacements-config.yaml"
        if not filepath.exists():
            return {}
        with open(filepath) as f:
            raw = yaml.safe_load(f.read()) or {}

        replacements = {}
        for item in raw.get("globalReplacements", []):
            key = item["key"]
            value = item["value"]
            replacements[key] = value
        return replacements

    def _resolve_replacements(self, text: str) -> str:
        """Replace {{ VarName }} with actual values from replacements-config."""
        def _replace(match):
            key = match.group(1).strip()
            if key in self.replacements:
                val = self.replacements[key]
                if isinstance(val, list):
                    # For YAML list context, return JSON-like representation
                    return json.dumps(val)
                return str(val)
            return match.group(0)  # Leave unresolved

        return re.sub(r"\{\{\s*(\w+)\s*\}\}", _replace, text)

    def _load_json(self, relative_path: str) -> dict:
        """Load a JSON file from the config directory, handling LZA variables."""
        filepath = self.config_dir / relative_path
        if not filepath.exists():
            logger.warning("JSON file not found: %s", filepath)
            return {}
        with open(filepath) as f:
            raw = f.read()
        # Replace LZA-specific variables with placeholder strings so JSON is valid
        raw = raw.replace("${PARTITION}", "aws")
        raw = raw.replace("${ACCELERATOR_PREFIX}", self.replacements.get("AcceleratorPrefix", "AWSAccelerator"))
        raw = raw.replace("${MANAGEMENT_ACCOUNT_ACCESS_ROLE}", "AWSControlTowerExecution")
        # Handle ${ACCEL_LOOKUP::CUSTOM:*} references
        raw = re.sub(
            r'\$\{ACCEL_LOOKUP::CUSTOM:(\w+)\}',
            lambda m: json.dumps(self.replacements.get(m.group(1), [])),
            raw,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("JSON parse error in %s: %s", relative_path, e)
            return {"_parseError": str(e), "_rawFile": relative_path}

    def extract_all(self, output_dir: str):
        """Extract all KB documents and write to output directory."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        documents = {
            "org-accounts.json": self._extract_org_accounts(),
            "network-topology.json": self._extract_network(),
            "security-constraints.json": self._extract_security(),
            "iam-available.json": self._extract_iam(),
            "existing-resources.json": self._extract_existing(),
            "naming-conventions.json": self._extract_naming(),
        }

        for filename, doc in documents.items():
            filepath = output_path / filename
            with open(filepath, "w") as f:
                json.dump(doc, f, indent=2, default=str)
            logger.info("Written: %s (%d bytes)", filepath, filepath.stat().st_size)

        return documents

    # ─── Extractor: Organization & Accounts ─────────────────────────────────

    def _extract_org_accounts(self) -> dict:
        """Extract organization structure with accounts mapped to OUs."""
        # Build OU → SCP mapping from organization config
        ou_scps = self._build_ou_scp_mapping()

        # Build OU → accounts mapping
        ou_accounts = {}

        for account in self.accounts.get("mandatoryAccounts", []):
            ou = account.get("organizationalUnit", "Root")
            ou_accounts.setdefault(ou, []).append({
                "name": account["name"],
                "email": account.get("email", ""),
                "description": account.get("description", "").strip(),
                "type": "mandatory",
            })

        for account in self.accounts.get("workloadAccounts", []):
            ou = account.get("organizationalUnit", "Workloads")
            ou_accounts.setdefault(ou, []).append({
                "name": account["name"],
                "email": account.get("email", ""),
                "description": account.get("description", "").strip(),
                "type": "workload",
            })

        # Build the final structure
        org_units = []
        for ou_entry in self.organization.get("organizationalUnits", []):
            ou_name = ou_entry["name"]
            if ou_entry.get("ignore"):
                continue
            org_units.append({
                "name": ou_name,
                "path": f"Root/{ou_name}",
                "accounts": ou_accounts.get(ou_name, []),
                "appliedSCPs": ou_scps.get(ou_name, []),
            })

        return {
            "documentType": "organization-structure",
            "extractedFrom": "accounts-config.yaml + organization-config.yaml",
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "homeRegion": self.replacements.get("AcceleratorHomeRegion", "ca-central-1"),
            "acceleratorPrefix": self.replacements.get("AcceleratorPrefix", "AWSAccelerator"),
            "organizationalUnits": org_units,
            "totalAccounts": sum(len(ou.get("accounts", [])) for ou in org_units),
        }

    def _build_ou_scp_mapping(self) -> dict:
        """Build a mapping of OU name → list of SCP names."""
        ou_scps = {}
        for scp in self.organization.get("serviceControlPolicies", []):
            scp_name = self._resolve_replacements(scp.get("name", ""))
            targets = scp.get("deploymentTargets", {})
            for ou in targets.get("organizationalUnits", []):
                ou_scps.setdefault(ou, []).append(scp_name)
        return ou_scps

    # ─── Extractor: Network Topology ────────────────────────────────────────

    def _extract_network(self) -> dict:
        """Extract VPC topology with resolved CIDRs."""
        vpcs = []

        for vpc_config in self.network.get("vpcs", []):
            vpc_name = vpc_config.get("name", "")
            cidrs = vpc_config.get("cidrs", [])
            # Resolve CIDR references
            resolved_cidrs = []
            for cidr in cidrs:
                if isinstance(cidr, dict):
                    resolved_cidrs.append(cidr.get("value", cidr.get("cidr", str(cidr))))
                else:
                    resolved_cidrs.append(str(cidr))

            subnets = []
            for subnet in vpc_config.get("subnets", []):
                subnet_name = subnet.get("name", "")
                for az_item in subnet.get("availabilityZones", []):
                    if isinstance(az_item, dict):
                        subnets.append({
                            "name": f"{subnet_name}-{az_item.get('availabilityZone', '')}",
                            "cidr": az_item.get("cidr", ""),
                            "availabilityZone": az_item.get("availabilityZone", ""),
                            "type": "private" if "TgwAttach" not in subnet_name and "Nat" not in subnet_name else "transit",
                        })

            # VPC endpoints
            endpoints = []
            for gw_ep in vpc_config.get("gatewayEndpoints", {}).get("endpoints", []):
                if isinstance(gw_ep, dict):
                    endpoints.append(gw_ep.get("service", ""))
                else:
                    endpoints.append(str(gw_ep))
            for iface_ep in vpc_config.get("interfaceEndpoints", {}).get("endpoints", []):
                if isinstance(iface_ep, dict):
                    endpoints.append(iface_ep.get("service", ""))
                else:
                    endpoints.append(str(iface_ep))

            vpcs.append({
                "name": vpc_name,
                "account": vpc_config.get("account", ""),
                "region": vpc_config.get("region", self.replacements.get("AcceleratorHomeRegion", "ca-central-1")),
                "cidrs": resolved_cidrs,
                "subnets": subnets,
                "vpcEndpoints": endpoints,
                "transitGatewayAttached": bool(vpc_config.get("transitGatewayAttachments")),
            })

        # Transit gateways
        transit_gateways = []
        for tgw in self.network.get("transitGateways", []):
            transit_gateways.append({
                "name": tgw.get("name", ""),
                "account": tgw.get("account", ""),
                "region": tgw.get("region", ""),
                "asn": tgw.get("asn"),
                "routeTables": [rt.get("name", "") for rt in tgw.get("routeTables", [])],
            })

        return {
            "documentType": "network-topology",
            "extractedFrom": "network-config.yaml",
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "vpcs": vpcs,
            "transitGateways": transit_gateways,
            "resolvedCidrs": {k: v for k, v in self.replacements.items() if "Cidr" in k or "Subnet" in k},
        }

    # ─── Extractor: Security Constraints ────────────────────────────────────

    def _extract_security(self) -> dict:
        """Extract SCPs (raw + summarized) and Config rules."""
        scps = []
        for scp in self.organization.get("serviceControlPolicies", []):
            scp_name = self._resolve_replacements(scp.get("name", ""))
            policy_path = scp.get("policy", "")
            targets = scp.get("deploymentTargets", {})

            # Load raw policy
            raw_policy = self._load_json(policy_path) if policy_path else {}

            # Generate human-readable summary
            summary = self._summarize_scp(raw_policy, scp_name)

            scps.append({
                "name": scp_name,
                "description": scp.get("description", "").strip(),
                "appliesTo": {
                    "organizationalUnits": targets.get("organizationalUnits", []),
                    "accounts": targets.get("accounts", []),
                },
                "summary": summary,
                "rawPolicy": raw_policy,
            })

        # Config rules from security-config
        config_rules = []
        for rule_set in self.security.get("awsConfig", {}).get("ruleSets", []):
            for rule in rule_set.get("rules", []):
                config_rules.append({
                    "name": rule.get("name", ""),
                    "complianceResourceTypes": rule.get("complianceResourceTypes", []),
                    "remediation": bool(rule.get("remediation")),
                })

        return {
            "documentType": "security-constraints",
            "extractedFrom": "organization-config.yaml + service-control-policies/ + security-config.yaml",
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "serviceControlPolicies": scps,
            "configRules": config_rules,
            "allowedRegions": self.replacements.get("SandboxAllowedRegions", ["ca-central-1"]),
        }

    def _summarize_scp(self, policy: dict, name: str) -> list[str]:
        """Generate human-readable summary of an SCP's deny statements."""
        summaries = []
        for statement in policy.get("Statement", []):
            if statement.get("Effect") != "Deny":
                continue

            sid = statement.get("Sid", "")
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]

            conditions = statement.get("Condition", {})

            if "ec2:Encrypted" in str(conditions):
                summaries.append(f"[{sid}] Deny unencrypted EBS/EFS/RDS resources")
            elif "aws:RequestedRegion" in str(conditions):
                summaries.append(f"[{sid}] Deny actions outside allowed regions")
            elif actions == ["*"] and "root" in str(conditions).lower():
                summaries.append(f"[{sid}] Deny all actions by root user")
            else:
                # Summarize by action group
                action_services = set()
                for a in actions[:5]:
                    service = a.split(":")[0] if ":" in a else a
                    action_services.add(service)
                if action_services:
                    summaries.append(f"[{sid}] Deny: {', '.join(sorted(action_services))}")

        return summaries

    # ─── Extractor: IAM Resources ───────────────────────────────────────────

    def _extract_iam(self) -> dict:
        """Extract IAM policies, permission sets, and role conventions."""
        # Permission sets from Identity Center
        permission_sets = []
        ic_config = self.iam.get("identityCenter", {})
        for ps in ic_config.get("identityCenterPermissionSets", []):
            policies = ps.get("policies", {})
            permission_sets.append({
                "name": ps["name"],
                "awsManagedPolicies": policies.get("awsManaged", []),
                "customerManagedPolicies": policies.get("customerManaged", []),
            })

        # Identity Center assignments
        assignments = []
        for assignment in ic_config.get("identityCenterAssignments", []):
            assignments.append({
                "name": assignment["name"],
                "permissionSet": assignment["permissionSetName"],
                "principals": assignment.get("principals", []),
                "targets": assignment.get("deploymentTargets", {}),
            })

        # Managed policies from policySets
        managed_policies = []
        for policy_set in self.iam.get("policySets", []):
            targets = policy_set.get("deploymentTargets", {})
            for policy in policy_set.get("policies", []):
                policy_name = self._resolve_replacements(policy.get("name", ""))
                managed_policies.append({
                    "name": policy_name,
                    "policyFile": policy.get("policy", ""),
                    "deployedTo": targets,
                })

        # Role sets
        role_sets = []
        for role_set in self.iam.get("roleSets", []):
            for role in role_set.get("roles", []):
                role_sets.append({
                    "name": role.get("name", ""),
                    "policies": role.get("policies", {}),
                    "boundaryPolicy": role.get("boundaryPolicy", ""),
                    "deployedTo": role_set.get("deploymentTargets", {}),
                })

        return {
            "documentType": "iam-resources",
            "extractedFrom": "iam-config.yaml + iam-policies/",
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "identityCenter": {
                "delegatedAdmin": ic_config.get("delegatedAdminAccount", ""),
                "permissionSets": permission_sets,
                "assignments": assignments,
            },
            "managedPolicies": managed_policies,
            "roleSets": role_sets,
        }

    # ─── Extractor: Existing Resources ──────────────────────────────────────

    def _extract_existing(self) -> dict:
        """Extract already-deployed customizations (CloudFormation stacks)."""
        stacks = []
        for stack in self.customizations.get("customizations", {}).get("cloudFormationStacks", []):
            targets = stack.get("deploymentTargets", {})
            params = {p["name"]: p["value"] for p in stack.get("parameters", [])}
            stacks.append({
                "name": stack.get("name", ""),
                "description": stack.get("description", "").strip(),
                "template": stack.get("template", ""),
                "targets": {
                    "organizationalUnits": targets.get("organizationalUnits", []),
                    "accounts": targets.get("accounts", []),
                },
                "regions": stack.get("regions", []),
                "parameters": params,
            })

        return {
            "documentType": "existing-deployments",
            "extractedFrom": "customizations-config.yaml",
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "cloudFormationStacks": stacks,
        }

    # ─── Extractor: Naming Conventions ──────────────────────────────────────

    def _extract_naming(self) -> dict:
        """Extract naming patterns and mandatory tags from config analysis."""
        # Extract tag values from customizations parameters
        tags = {}
        for stack in self.customizations.get("customizations", {}).get("cloudFormationStacks", []):
            if "Tags" in stack.get("name", "") or "tags" in stack.get("template", "").lower():
                for param in stack.get("parameters", []):
                    tags[param["name"]] = param["value"]

        # Infer naming patterns from accounts
        account_names = [a["name"] for a in self.accounts.get("workloadAccounts", [])]

        return {
            "documentType": "naming-conventions",
            "extractedFrom": "pattern analysis across all config files",
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "patterns": {
                "accounts": "Purpose + UserLastName for sandboxes, Purpose for workloads",
                "vpcs": "{Environment}-VPC (e.g., Central, Endpoint, Perimeter, Dev, Test, Prod)",
                "subnets": "{VpcName}-{Tier}-{AZ} (e.g., Subnet-Dev-App-A, Subnet-Prod-Data-B)",
                "tiers": ["Web", "App", "Data", "Mgmt", "TgwAttach", "Nat"],
                "scps": "AWSAccelerator-Guardrails-{Scope}",
                "iamPolicies": "{Org}{Service}{Scope}Access (e.g., AlithyaEksFullAccess)",
            },
            "mandatoryTags": tags,
            "regions": {
                "primary": self.replacements.get("AcceleratorHomeRegion", "ca-central-1"),
                "allowedInSandbox": self.replacements.get("SandboxAllowedRegions", []),
                "otherEnabled": self.replacements.get("OtherEnabledRegions", []),
            },
            "acceleratorPrefix": self.replacements.get("AcceleratorPrefix", "AWSAccelerator"),
            "organization": "Alithya",
            "accountNamingExamples": account_names[:10],
        }


def main():
    parser = argparse.ArgumentParser(
        description="Extract LZA config into KB-ready JSON documents"
    )
    parser.add_argument("config_dir", help="Path to the LZA configuration repository")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for JSON files (default: <project>/knowledge-base/)",
    )
    args = parser.parse_args()

    if args.output_dir is None:
        # Default: write to the knowledge-base/ dir of the generator project
        script_dir = Path(__file__).parent
        args.output_dir = str(script_dir / "knowledge-base")

    logger.info("Extracting LZA config from: %s", args.config_dir)
    logger.info("Output directory: %s", args.output_dir)

    extractor = LZAConfigExtractor(args.config_dir)
    documents = extractor.extract_all(args.output_dir)

    logger.info("=== Extraction complete: %d documents ===", len(documents))
    for name, doc in documents.items():
        doc_type = doc.get("documentType", "unknown")
        logger.info("  %s → %s", name, doc_type)


if __name__ == "__main__":
    main()
