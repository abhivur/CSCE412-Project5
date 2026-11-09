"""Only four approved AWS services are reachable; no shell or arbitrary command tool."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from .core import Audit, Config

ALLOWED = {
    "s3api": {"head-bucket", "create-bucket", "get-bucket-tagging", "put-bucket-tagging",
              "put-public-access-block", "get-public-access-block", "put-bucket-ownership-controls",
              "put-bucket-encryption", "get-bucket-encryption", "put-bucket-policy", "get-bucket-policy",
              "get-bucket-policy-status", "put-object", "head-object"},
    "cloudfront": {"list-origin-access-controls", "get-origin-access-control", "create-origin-access-control",
                   "list-distributions", "get-distribution", "get-distribution-config", "list-tags-for-resource",
                   "create-distribution-with-tags", "update-distribution", "create-invalidation", "get-invalidation"},
    "route53": {"list-hosted-zones-by-name", "get-hosted-zone", "create-hosted-zone", "change-tags-for-resource",
                "change-resource-record-sets", "list-resource-record-sets"},
    "acm": {"list-certificates", "describe-certificate", "list-tags-for-certificate", "request-certificate"},
}
WRITES = {"create-", "put-", "update-", "change-", "request-"}


class AwsError(RuntimeError):
    def __init__(self, service: str, operation: str, message: str):
        super().__init__(f"{service} {operation}: {message[:2000]}")
        self.message = message


class AWS:
    def __init__(self, config: Config, audit: Audit, mutable: bool = False):
        self.config, self.audit, self.mutable = config, audit, mutable

    def call(self, service: str, operation: str, params: dict | None = None,
             region: str | None = None, body: str | None = None) -> dict:
        self.config.validate(cloud=True)
        if operation not in ALLOWED.get(service, set()):
            raise PermissionError("AWS operation is outside the explicit allowlist")
        if any(operation.startswith(prefix) for prefix in WRITES) and not self.mutable:
            raise PermissionError("Cloud writes require an approved deployment plan")
        executable = shutil.which("aws")
        if not executable:
            raise RuntimeError("Install AWS CLI v2 before running cloud operations")
        command = [executable, service, operation, "--profile", self.config["aws_profile"],
                   "--region", region or self.config["region"], "--output", "json", "--no-cli-pager",
                   "--cli-connect-timeout", "10", "--cli-read-timeout", "45"]
        if params:
            command += ["--cli-input-json", json.dumps(params)]
        if body:
            if service != "s3api" or operation != "put-object":
                raise PermissionError("File bodies are only allowed for website uploads")
            command += ["--body", body]
        self.audit.emit("aws_tool", "aws_request", {"service": service, "operation": operation,
                                                     "parameters": params, "body": body})
        env = dict(os.environ, AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off", AWS_MAX_ATTEMPTS="2")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=100, env=env, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            self.audit.emit("aws_tool", "aws_error", {"service": service, "operation": operation, "error": str(exc)})
            raise AwsError(service, operation, str(exc)) from exc
        if result.returncode:
            self.audit.emit("aws_tool", "aws_error", {"service": service, "operation": operation,
                                                     "exit_code": result.returncode, "error": result.stderr})
            raise AwsError(service, operation, result.stderr)
        value = json.loads(result.stdout) if result.stdout.strip() else {}
        self.audit.emit("aws_tool", "aws_result", {"service": service, "operation": operation, "result": value})
        return value
