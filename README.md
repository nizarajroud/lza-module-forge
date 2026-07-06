# Generate Customized Compliant IaC Scripts for AWS Landing Zone using Amazon Bedrock

An automated Terraform code generator that uses **Amazon Bedrock** (Claude Sonnet 4) with **Retrieval Augmented Generation (RAG)** to produce compliant, organization-specific infrastructure-as-code for AWS Landing Zone account customizations.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│   Bedrock   │────▶│  Lambda Function │────▶│  Bedrock Knowledge  │
│    Agent    │     │                  │     │  Base (RAG)         │
└─────────────┘     │  1. Query KB     │     │  - TF modules       │
                    │  2. Generate TF  │     │  - Best practices   │
                    │  3. Generate Doc │     │  - Security configs │
                    │  4. Commit       │     └─────────────────────┘
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  GitHub Repo     │
                    │  (AFT customs)   │
                    │  - main.tf       │
                    │  - README.md     │
                    └──────────────────┘
```

## How It Works

1. A user describes the AWS services they need via the Bedrock Agent
2. The Lambda queries a **Knowledge Base** containing organization-approved Terraform modules
3. Using RAG, Claude generates Terraform code that follows org best practices and uses approved modules
4. A README with cost estimation and Well-Architected review is generated
5. Both files are committed to the AFT account customizations GitHub repository

## Project Structure

```
.
├── lambda/
│   ├── src/
│   │   ├── handler.py          # Lambda entry point
│   │   ├── bedrock_client.py   # Claude invocation (Messages API)
│   │   ├── knowledge_base.py   # KB retrieval (RetrieveAndGenerate)
│   │   └── github_client.py    # GitHub Contents API client
│   ├── tests/
│   │   └── test_handler.py     # Unit tests
│   └── requirements.txt
├── knowledge-base/
│   ├── terraform-modules-kb.json   # KB data source (upload to S3)
│   └── README.md
├── infrastructure/
│   └── template.yaml              # SAM/CloudFormation for deployment
├── .env.example                   # Environment variables template
└── README.md
```

## Prerequisites

- AWS account with **Amazon Bedrock** access (Claude Sonnet 4 model enabled)
- **Python 3.11+**
- AWS CLI configured with appropriate credentials
- GitHub Personal Access Token with `repo` scope

## Setup

### 1. Knowledge Base

```bash
# Upload the module definitions to S3
aws s3 cp knowledge-base/terraform-modules-kb.json s3://your-bucket/kb/

# Create the Knowledge Base in Bedrock console:
# - Data source: S3 bucket above
# - Embeddings: Amazon Titan G1 Embeddings
# - Vector store: Managed (OpenSearch Serverless)
```

### 2. Lambda Deployment

```bash
# Configure environment
cp .env.example .env
# Edit .env with your values

# Package and deploy (using SAM)
cd infrastructure
sam build
sam deploy --guided
```

### 3. Bedrock Agent

Create a Bedrock Agent with an Action Group that accepts:
- `AccountEmail` — email for the new account
- `AccountName` — name identifier
- `CustomizationName` — template name for the customization
- `AwsServices` — comma-separated list of services (e.g., "ec2, s3, rds")

Point the Action Group's Lambda to the deployed function.

## Configuration

| Variable            | Description                              | Example                                 |
|---------------------|------------------------------------------|-----------------------------------------|
| `GITHUB_TOKEN`      | GitHub PAT with repo scope               | `ghp_xxxx`                              |
| `GITHUB_REPO_OWNER` | GitHub org/user                          | `my-org`                                |
| `GITHUB_REPO_NAME`  | Target repository                        | `aft-account-customizations`            |
| `KNOWLEDGE_BASE_ID` | Bedrock KB identifier                    | `ABCDEF1234`                            |
| `BEDROCK_MODEL_ID`  | Model for generation                     | `anthropic.claude-sonnet-4-20250514-v1:0`       |
| `AWS_REGION`        | AWS region                               | `ca-central-1`                          |

## Testing

```bash
cd lambda
pip install pytest
python -m pytest tests/ -v
```

## Lambda IAM Permissions Required

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock:InvokeModel",
    "bedrock:RetrieveAndGenerate"
  ],
  "Resource": "*"
}
```

## Supported Services

The Knowledge Base includes module definitions for: EC2, RDS, S3, VPC, IAM, ELB, Auto Scaling, DynamoDB, Lambda, API Gateway, Security Groups, CloudFront, Route53, SQS, SNS, ECS, EKS, CloudWatch, KMS, and CodeBuild.

Add new modules by updating `knowledge-base/terraform-modules-kb.json` and syncing the KB.

## License

This project is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
