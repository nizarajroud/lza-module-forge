# LZA Module Forge

Generate **compliant Terraform modules** for AWS Landing Zone environments using **Amazon Bedrock** (Claude Sonnet 4.5) with **Retrieval Augmented Generation (RAG)**.

The generated modules follow organization standards, respect LZA constraints (SCPs, encryption, tagging, region lock), and produce HashiCorp-standard multi-file structure ready for review and deployment.

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  User/CLI    │────▶│  generate_module │────▶│  Bedrock Knowledge  │
│              │     │  .py (local)     │     │  Base (RAG)         │
└──────────────┘     │                  │     │  - LZA constraints  │
                     │  OR              │     │  - Module standards │
┌──────────────┐     │                  │     │  - Existing modules │
│ Bedrock Agent│────▶│  Lambda (AWS)    │     └─────────────────────┘
└──────────────┘     └────────┬─────────┘
                              │
                              ▼
                     ┌──────────────────┐
                     │  Local Filesystem │
                     │  modules/         │
                     │  terraform-aws-*/ │
                     │  ├── main.tf      │
                     │  ├── variables.tf │
                     │  ├── outputs.tf   │
                     │  ├── versions.tf  │
                     │  ├── examples/    │
                     │  └── README.md    │
                     └──────────────────┘
```

## Quick Start

```bash
# 1. Configure
cp config.env.example config.env
# Edit config.env with your KB ID, output dir, AWS profile

# 2. Generate a module
python generate_module.py "Bedrock Knowledge Base" --profile csna-operations-sso-828

# 3. Review and validate
cd /path/to/output/modules/terraform-aws-bedrock-knowledge-base
terraform init && terraform validate

# 4. Commit
git add . && git commit -m "feat: add terraform-aws-bedrock-knowledge-base module"
```

## Full Workflow

```bash
# Step 1: Extract LZA constraints into KB-ready JSON
python extract_lza_config.py /path/to/aws-accelerator-config

# Step 2: Extract existing module patterns into KB-ready JSON
python extract_tf_registry_v2.py --source local --modules-dir /path/to/terraform-modules

# Step 3: Upload to S3 and sync the Knowledge Base
./sync-kb.sh --profile csna-operations-sso-828

# Step 4: Generate a new module (augmented by KB context)
python generate_module.py "Bedrock Knowledge Base" --profile csna-operations-sso-828
```

## Project Structure

```
lza-module-forge/
├── generate_module.py              # Local CLI — generate modules on filesystem
├── extract_lza_config.py           # Parse LZA YAML → KB JSON (org context)
├── extract_tf_registry.py          # v1: TFC/GitLab/manifest → KB JSON
├── extract_tf_registry_v2.py       # v2: parse local .tf modules → KB JSON
├── sync-kb.sh                      # Upload KB JSONs to S3 + trigger ingestion
├── deploy.sh                       # Full AWS deployment (KB + Lambda + SAM)
├── setup-kb.py                     # Create Bedrock KB (S3 + AOSS + IAM)
├── config.env.example              # Configuration template
├── prompts/
│   ├── v1.md                       # Prompt v1 (single-file output)
│   └── v2.md                       # Prompt v2 (multi-file, HashiCorp standard)
├── lambda/
│   ├── src/
│   │   ├── handler.py              # Lambda entry point (Agent Action Group)
│   │   ├── bedrock_client.py       # Claude invocation (loads prompts from prompts/)
│   │   ├── knowledge_base.py       # KB retrieval (RetrieveAndGenerate)
│   │   ├── github_client.py        # GitHub commit (OUTPUT_MODE=github)
│   │   └── local_writer.py         # Local filesystem write (OUTPUT_MODE=local)
│   ├── tests/
│   │   └── test_handler.py         # Unit tests (6 passing)
│   └── requirements.txt
├── knowledge-base/                 # KB data source (generated JSONs)
│   ├── org-accounts.json           # Organization structure + accounts
│   ├── network-topology.json       # VPCs, CIDRs, endpoints
│   ├── security-constraints.json   # SCPs (raw + summarized)
│   ├── iam-available.json          # IAM policies, permission sets
│   ├── existing-resources.json     # Already-deployed customizations
│   ├── naming-conventions.json     # Naming patterns + mandatory tags
│   ├── terraform-modules-kb.json   # Existing module patterns
│   └── terraform-module-standards-kb.json  # Module structure standards
├── infrastructure/
│   ├── template.yaml               # SAM template (Lambda + IAM)
│   └── openapi-schema.json         # Bedrock Agent Action Group schema
├── docs/
│   └── architecture.drawio         # Architecture diagrams (4 tabs)
└── modules-manifest.yaml.example   # Example manifest for manual module registry
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `OUTPUT_MODE` | `local` (filesystem) or `github` (API commit) | `local` |
| `OUTPUT_MODULES_DIR` | Target directory for generated modules | `/tmp/generated-modules` |
| `PROMPT_VERSION` | Prompt version (`v1` = single-file, `v2` = multi-file) | `v2` |
| `KNOWLEDGE_BASE_ID` | Bedrock KB identifier | — |
| `BEDROCK_MODEL_ID` | Model for generation | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `AWS_REGION` | AWS region | `ca-central-1` |
| `MODULE_PREFIX` | Directory prefix for modules | `terraform-aws` |
| `LZA_CONFIG_DIR` | Path to LZA config repo | — |
| `TF_MODULES_DIR` | Path to existing TF modules repo | — |

## How RAG Augmentation Works

```
User prompt: "Generate a module for Bedrock Knowledge Base"
                    ↓
KB Query → returns: LZA constraints + module standards + existing patterns
                    ↓
Augmented prompt = user request + org context (CIDRs, SCPs, tags, KMS, naming)
                    ↓
Claude Sonnet 4.5 → generates module compliant with ALL org constraints
                    ↓
Output: 6 files (main.tf, variables.tf, outputs.tf, versions.tf, example, README)
```

## Deployed Infrastructure (Operations Account)

| Resource | Value |
|----------|-------|
| Lambda | `arn:aws:lambda:ca-central-1:026991214828:function:lza-terraform-generator` |
| Knowledge Base | `7NGH4NNDFC` |
| AOSS Collection | `jppv04jl6kfllnrzdf44` |
| S3 Bucket | `lza-terraform-kb-data-026991214828` |
| Bedrock Agent | `7R6JXTKLR6` (agent-lztf) |
| Model | Claude Sonnet 4.5 via `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |

## 3-Repos Ecosystem

```
poc-bnc-terraform-modules/          ← Module Library (source of truth)
    │
    ├── extract_tf_registry_v2.py reads existing modules
    │         ↓
lza-module-forge/                   ← THIS REPO (Generator)
    │   KB + Claude → generates new modules
    │         ↓
    ├── writes to poc-bnc-terraform-modules/modules/
    │
poc-bnc-bedrock-deployment/         ← Consumer (deploys via TF Registry)
alth-poc-bnc-bedrock-deployment/    ← Consumer (Alithya LZ simulation)
```

## Testing

```bash
cd lambda
python -m pytest tests/ -v
```

## License

MIT-0 — See [LICENSE](LICENSE)
