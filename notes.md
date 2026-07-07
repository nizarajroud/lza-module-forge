# LZA Config Extractor — Project Notes

## Decisions (2026-07-07)

### KB Document Strategy
- **7 output files** (one per category): org-accounts, network-topology, security-constraints, iam-available, existing-resources, naming-conventions, terraform-modules (enriched)
- Start simple, split further only if RAG retrieval quality is poor

### Data Handling
- **Resolve replacements**: extract_lza_config.py resolves all `{{VAR}}` from replacements-config.yaml into actual values. Claude needs real CIDRs/names to generate valid Terraform.
- **SCP parsing**: output BOTH raw JSON policy statements AND human-readable summaries. Raw for precision, summary for RAG recall.

### Source
- LZA config: `/home/nizar/HomeWspce/aws-accelerator-config-957201034306`
- Organization: Alithya (account 957201034306 = Management)
- Region: ca-central-1

### Output Target
- Upload to: `s3://lza-terraform-kb-data-026991214828/kb/`
- KB ID: `7NGH4NNDFC`
- After upload, trigger re-ingestion to sync

### What remains to build
- [ ] `extract_lza_config.py` — the parser script
- [ ] Test extraction locally
- [ ] Upload to S3 + re-sync KB
- [ ] Update Lambda prompts to leverage new context docs
- [ ] (Future) GitLab CI pipeline for auto-sync on config push
