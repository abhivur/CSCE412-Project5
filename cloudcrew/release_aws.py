"""Small AWS boundary for release operations. The build uses its service role."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .core import Audit, redact
from .aws_input import cli_input
from .release_config import ReleaseConfig, object_path


ALLOWED = {
    "s3api": {"get-object", "head-object", "put-object", "get-bucket-tagging", "get-bucket-policy",
              "get-public-access-block", "get-bucket-policy-status", "get-bucket-encryption"},
    "cloudfront": {"get-distribution", "get-distribution-config", "list-tags-for-resource",
                   "get-origin-access-control", "update-distribution", "create-invalidation", "get-invalidation"},
    "codepipeline": {"get-pipeline-state", "get-pipeline-execution", "list-pipeline-executions", "list-action-executions"},
    "codebuild": {"batch-get-builds"},
}


class ReleaseAwsError(RuntimeError):
    def __init__(self, operation, message):
        self.missing = any(s in message for s in ("NoSuchKey", "Not Found", "(404)"))
        self.conflict = any(s in message for s in ("PreconditionFailed", "ConditionalRequestConflict", "(412)"))
        super().__init__(redact(f"{operation}: {message[:1800]}"))


class ReleaseAWS:
    def __init__(self, config: ReleaseConfig, audit: Audit, mutable=False, profile=None):
        self.config, self.audit, self.mutable, self.profile = config, audit, mutable, profile

    def call(self, service, operation, params=None, *, output_file=None, body=None):
        params = params or {}
        if operation not in ALLOWED.get(service, set()):
            raise PermissionError("Operation is outside the release allowlist")
        if operation.startswith(("put-", "update-", "create-")) and not self.mutable:
            raise PermissionError("This release tool is read-only")
        if service == "s3api":
            if params.get("Bucket") not in (self.config['site_bucket'], self.config['artifact_bucket']):
                raise PermissionError("Bucket is outside this deployment")
            if "Key" in params:
                object_path(params['Key'])
                if operation == "put-object":
                    prefixes = ("releases/",) if params['Bucket'] == self.config['site_bucket'] else ("releases/", "evidence/", "control/")
                    if not params['Key'].startswith(prefixes):
                        raise PermissionError("Write is outside the release prefixes")
        if service == "cloudfront":
            if operation in ('get-distribution', 'get-distribution-config', 'update-distribution') and params.get('Id') != self.config['distribution_id']:
                raise PermissionError("Distribution is outside this deployment")
            if "DistributionId" in params and params['DistributionId'] != self.config['distribution_id']:
                raise PermissionError("Distribution is outside this deployment")
            if "Resource" in params and params['Resource'] != self.config.distribution_arn:
                raise PermissionError("Resource is outside this deployment")
        command = ["aws", service, operation, "--region", self.config['region'], "--output", "json", "--no-cli-pager",
                   "--cli-connect-timeout", "10", "--cli-read-timeout", "60"]
        download = service == 's3api' and operation == 'get-object'
        download_options = {'Bucket': '--bucket', 'Key': '--key', 'VersionId': '--version-id',
                            'IfMatch': '--if-match', 'ExpectedBucketOwner': '--expected-bucket-owner'}
        if download and (output_file is None or 'Key' not in params or set(params) - set(download_options)):
            raise ValueError('S3 downloads require an output file and supported bucket/key arguments')
        if output_file is not None and not download:
            raise PermissionError('Output files are only supported for S3 downloads')
        if self.profile and not os.environ.get("CODEBUILD_BUILD_ID"):
            command += ["--profile", self.profile]
        if body:
            if service != 's3api' or operation != 'put-object':
                raise PermissionError('File bodies are only supported for S3 uploads')
            command += ["--body", str(body)]
        self.audit.emit("release_tool", "aws_request", {"service": service, "operation": operation, "parameters": params})
        env = dict(os.environ, AWS_PAGER="", AWS_MAX_ATTEMPTS="3", AWS_CLI_AUTO_PROMPT="off")
        if os.environ.get('CODEBUILD_BUILD_ID'):
            env.pop('AWS_PROFILE', None)
            env.pop('AWS_DEFAULT_PROFILE', None)
        if download:
            # AWS CLI does not support JSON input for commands with a streaming output file.
            for key, value in params.items():
                command += [download_options[key], str(value)]
            result = subprocess.run(command + [str(output_file)], stdin=subprocess.DEVNULL,
                                    text=True, capture_output=True, timeout=100, env=env)
        else:
            with cli_input(params) as request:
                result = subprocess.run(command + ['--cli-input-json', request], stdin=subprocess.DEVNULL,
                                        text=True, capture_output=True, timeout=100, env=env)
        if result.returncode:
            self.audit.emit("release_tool", "aws_error", {"operation": operation, "error": result.stderr})
            raise ReleaseAwsError(operation, result.stderr)
        value = json.loads(result.stdout) if result.stdout.strip() else {}
        self.audit.emit("release_tool", "aws_result", {"operation": operation, "result": value})
        return value

    def get(self, bucket, key):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "object"
            result = self.call("s3api", "get-object", {"Bucket": bucket, "Key": key}, output_file=path)
            return path.read_bytes(), result.get("ETag")

    def get_json(self, bucket, key, *, optional=False):
        try:
            body, etag = self.get(bucket, key)
            return json.loads(body), etag
        except ReleaseAwsError as exc:
            if optional and exc.missing:
                return None, None
            raise

    def put(self, bucket, key, body, *, content_type="application/json", immutable=False, etag=None, cache_control="no-store"):
        params = {"Bucket": bucket, "Key": key, "ContentType": content_type,
                  "ServerSideEncryption": "AES256", "CacheControl": cache_control}
        if immutable:
            params['IfNoneMatch'] = "*"
        if etag:
            params['IfMatch'] = etag
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body"
            path.write_bytes(body)
            return self.call("s3api", "put-object", params, body=path)

    def put_json(self, bucket, key, value, **kwargs):
        return self.put(bucket, key, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(), **kwargs)
