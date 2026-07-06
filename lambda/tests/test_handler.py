"""
Unit tests for the Lambda handler.
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

# Set required env vars before importing handler
os.environ["GITHUB_TOKEN"] = "ghp_test_token"
os.environ["GITHUB_REPO_OWNER"] = "test-owner"
os.environ["GITHUB_REPO_NAME"] = "test-repo"
os.environ["KNOWLEDGE_BASE_ID"] = "TESTkb123"
os.environ["BEDROCK_MODEL_ID"] = "anthropic.claude-sonnet-4-20250514-v1:0"
os.environ["AWS_REGION"] = "ca-central-1"

# Import after env vars are set
from src.handler import lambda_handler, _extract_properties, _build_response


def _make_event(services="ec2, s3", account_name="test-account", customization_name="web-app"):
    """Build a minimal Bedrock Agent Action Group event."""
    return {
        "actionGroup": "GenerateIaC",
        "apiPath": "/generate",
        "httpMethod": "POST",
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": "AccountEmail", "value": "test@example.com"},
                        {"name": "AccountName", "value": account_name},
                        {"name": "CustomizationName", "value": customization_name},
                        {"name": "AwsServices", "value": services},
                    ]
                }
            }
        },
        "sessionAttributes": {},
        "promptSessionAttributes": {},
    }


class TestExtractProperties:
    def test_extracts_all_properties(self):
        event = _make_event()
        props = _extract_properties(event)
        assert props["AccountName"] == "test-account"
        assert props["AwsServices"] == "ec2, s3"
        assert props["CustomizationName"] == "web-app"

    def test_handles_empty_properties(self):
        event = {"requestBody": {"content": {"application/json": {"properties": []}}}}
        props = _extract_properties(event)
        assert props == {}


class TestBuildResponse:
    def test_success_response_structure(self):
        event = _make_event()
        response = _build_response(event, 200, {"message": "ok"})
        assert response["messageVersion"] == "1.0"
        assert response["response"]["httpStatusCode"] == 200
        body = json.loads(response["response"]["responseBody"]["application/json"]["body"])
        assert body["message"] == "ok"

    def test_error_response(self):
        event = _make_event()
        response = _build_response(event, 500, {"error": "boom"})
        assert response["response"]["httpStatusCode"] == 500


class TestLambdaHandler:
    @patch("src.handler.commit_file")
    @patch("src.handler.generate_readme")
    @patch("src.handler.generate_terraform")
    @patch("src.handler.retrieve_module_definitions")
    def test_happy_path(self, mock_kb, mock_tf, mock_readme, mock_commit):
        mock_kb.return_value = "module defs here"
        mock_tf.return_value = "resource aws_instance {}"
        mock_readme.return_value = "# README"
        mock_commit.return_value = {"content": {"html_url": "https://github.com/..."}}

        event = _make_event()
        result = lambda_handler(event, None)

        assert result["response"]["httpStatusCode"] == 200
        assert mock_kb.called
        assert mock_tf.called
        assert mock_readme.called
        assert mock_commit.call_count == 2  # main.tf + README.md

    def test_missing_property_returns_400(self):
        event = {
            "actionGroup": "GenerateIaC",
            "apiPath": "/generate",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": [
                            {"name": "AccountEmail", "value": "test@example.com"},
                            # Missing AccountName, CustomizationName, AwsServices
                        ]
                    }
                }
            },
        }
        result = lambda_handler(event, None)
        assert result["response"]["httpStatusCode"] == 400
