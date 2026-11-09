from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def load_env(path: Path = ROOT / ".env") -> None:
    """Read simple KEY=value entries without shell evaluation or overriding the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if re.search(r"secret|password|api.?key|access.?key|session.?token|authorization", k, re.I)
                else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for key, secret in os.environ.items():
            if secret and len(secret) > 7 and re.search(r"SECRET|PASSWORD|API_KEY|ACCESS_KEY|SESSION_TOKEN", key):
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", value)
    return value


class Audit:
    def __init__(self, purpose: str, root: Path = ROOT / "runtime" / "runs"):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.path = root / self.run_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        self.emit("system", "run_started", {"purpose": purpose, "evidence_type": "actual_execution"})

    def emit(self, agent: str, event: str, data: Any) -> None:
        self.seq += 1
        record = {"timestamp": now(), "sequence": self.seq, "run_id": self.run_id,
                  "agent": agent, "event": event, "data": redact(data)}
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def artifact(self, name: str, value: Any) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+\.json", name):
            raise ValueError("Invalid artifact name")
        path = self.path / name
        write_json(path, redact(value))
        return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


@dataclass
class Config:
    values: dict

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        load_env()
        selected = path or ROOT / "config" / "project.json"
        if not selected.exists():
            selected = ROOT / "config" / "project.example.json"
        values = json.loads(selected.read_text())
        values["aws_profile"] = os.environ.get("AWS_PROFILE") or values.get("aws_profile", "default")
        values["model"] = os.environ.get("OPENAI_MODEL") or values.get("model", "gpt-4.1-mini")
        config = cls(values)
        config.validate()
        return config

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def validate(self, cloud: bool = False) -> None:
        v = self.values
        if not re.fullmatch(r"[a-z0-9-]{3,40}", v.get("project_id", "")):
            raise ValueError("project_id must contain 3–40 lowercase letters, digits, or hyphens")
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", v.get("region", "")):
            raise ValueError("Invalid AWS region")
        if not 1 <= v.get("max_model_calls_per_agent", 8) <= 12:
            raise ValueError("max_model_calls_per_agent must be 1–12")
        if not 500 <= v.get("max_output_tokens", 2200) <= 5000:
            raise ValueError("max_output_tokens must be 500–5000")
        if cloud:
            domain = v.get("domain", "")
            bucket = v.get("bucket", "")
            if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
                raise ValueError("Set a real lowercase domain in config/project.json")
            if "yournetid" in domain or "change-me" in bucket.lower():
                raise ValueError("Replace the example domain and bucket before AWS operations")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
                raise ValueError("Use a 3–63 character lowercase bucket name without dots")
            if v.get("hosted_zone_id") and not re.fullmatch(r"(?:/hostedzone/)?Z[A-Z0-9]+", v["hosted_zone_id"]):
                raise ValueError("Invalid hosted zone ID")

    @property
    def binding(self) -> str:
        return digest({k: self.values.get(k) for k in ("project_id", "domain", "region", "bucket", "aws_profile", "hosted_zone_id")})


class State:
    def __init__(self, config: Config, path: Path = ROOT / "runtime" / "state.json"):
        self.path = path
        self.config = config
        self.data = json.loads(path.read_text()) if path.exists() else {"binding": config.binding}
        if self.data.get("binding") != config.binding:
            raise ValueError("Configuration differs from recorded deployment. Use a separate state file; do not repoint existing state.")

    def save(self) -> None:
        write_json(self.path, self.data)

    def require(self, *keys: str) -> None:
        missing = [key for key in keys if not self.data.get(key)]
        if missing:
            raise ValueError("Complete earlier deployment stages first: " + ", ".join(missing))
