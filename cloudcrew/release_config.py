"""Project 6 settings contain resource names, never credentials."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .core import ROOT, digest


@dataclass(frozen=True)
class ReleaseConfig:
    values: dict

    @classmethod
    def load(cls, path: Path | None = None):
        config = cls(json.loads((path or ROOT / "config/cicd.production.json").read_text()))
        config.validate()
        return config

    def __getitem__(self, name):
        return self.values[name]

    @property
    def binding(self):
        return digest({k: self.values[k] for k in (
            "project", "account_id", "region", "domain", "site_bucket", "artifact_bucket", "distribution_id")})

    @property
    def distribution_arn(self):
        return f"arn:aws:cloudfront::{self['account_id']}:distribution/{self['distribution_id']}"

    def validate(self):
        patterns = {
            "project": r"[a-z][a-z0-9-]{2,39}", "account_id": r"\d{12}",
            "region": r"[a-z]{2}-[a-z]+-\d", "distribution_id": r"[A-Z0-9]{5,32}",
            "site_bucket": r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]",
            "artifact_bucket": r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]",
            "ownership_binding": r"[a-f0-9]{64}",
            "repository_owner": r"[A-Za-z0-9][A-Za-z0-9-]{0,38}",
            "repository_name": r"[A-Za-z0-9_.-]{1,100}",
            "branch": r"[A-Za-z0-9][A-Za-z0-9_./-]{0,99}",
        }
        allowed = set(patterns) | {'domain', 'source_provider', 'connection_arn', 'connection_approved', 'image',
                                  'verify_attempts', 'verify_delay_seconds', 'cloudfront_timeout_seconds'}
        if set(self.values) - allowed:
            raise ValueError('Unknown CI/CD setting; credentials belong in the environment, not configuration JSON')
        for key, pattern in patterns.items():
            if not isinstance(self.values.get(key), str) or not re.fullmatch(pattern, self[key]):
                raise ValueError(f"Invalid Project 6 setting: {key}")
        domain = self.values.get("domain", "")
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
            raise ValueError("Set a lowercase custom domain without a URL path")
        if self['site_bucket'] == self['artifact_bucket']:
            raise ValueError("Release control data must use a separate private artifact bucket")
        if '..' in self['branch'] or '//' in self['branch'] or self['branch'].endswith(('/', '.', '.lock')):
            raise ValueError('Invalid deployment branch')
        if self.values.get('image') != 'aws/codebuild/standard:7.0':
            raise ValueError('Use the reviewed CodeBuild managed image')
        for key, low, high in (("verify_attempts", 1, 12), ("verify_delay_seconds", 0, 60),
                               ("cloudfront_timeout_seconds", 30, 1800)):
            value = self.values.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"Invalid bounded wait: {key}")
        if self.values.get("source_provider") not in ("github_oauth", "connection"):
            raise ValueError("Unsupported GitHub source provider")
        if self['source_provider'] == "connection":
            arn = self.values.get("connection_arn", "") or ""
            if not self.values.get("connection_approved") or not re.fullmatch(
                    rf"arn:aws:code(?:star-connections|connections):{self['region']}:{self['account_id']}:connection/[a-f0-9-]+", arn):
                raise ValueError("Connections require instructor approval and an existing connection ARN")


def release_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64}|bootstrap-[a-f0-9]{24})", value):
        raise ValueError("Use a full source commit hash or a recorded bootstrap release ID")
    return value


def object_path(value: str) -> str:
    if (not isinstance(value, str) or not value or value.startswith(("/", "."))
            or any(p in ("", ".", "..") for p in value.split("/"))
            or re.search(r"[^A-Za-z0-9_./-]", value)):
        raise ValueError("Unsafe release object path")
    return value
