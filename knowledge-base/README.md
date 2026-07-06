# Terraform Modules Knowledge Base for LZA Account Customizations

This JSON file is the data source for the Amazon Bedrock Knowledge Base.
It contains Terraform module definitions used by the code generation Lambda
to produce compliant infrastructure-as-code following organizational standards.

## Structure

Each module entry includes:
- `ModuleName` — identifier for the module
- `ModuleSource` — Terraform registry source path
- `Description` — what the module provisions
- `RequiredParameters` — mandatory input variables
- `Dependencies` — other modules this depends on
- `ConditionalLogic` — when to apply specific configurations
- `BestPractices` — recommended patterns
- `SecurityNotes` — security considerations
- `Environments` (optional) — environment-specific defaults

## Updating

1. Edit `terraform-modules-kb.json`
2. Upload to the S3 bucket configured as the KB data source
3. Sync the Knowledge Base in the Bedrock console (or via API)

## Adding a New Module

Add a new object to the `TerraformModules` array following the schema above.
Ensure the module source points to your organization's private registry.
