"""Responses API transport. API secrets remain in process memory and request headers."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class ResponsesModel:
    def __init__(self, model: str):
        self.model = model
        self.key = os.environ.get("OPENAI_API_KEY", "")
        if not self.key:
            raise RuntimeError("OPENAI_API_KEY is not configured. Put it in your local .env; never paste it into logs or chat.")

    def respond(self, instructions: str, conversation: list, tools: list, max_tokens: int) -> dict:
        payload = {"model": self.model, "instructions": instructions, "input": conversation,
                   "tools": tools, "tool_choice": "required", "parallel_tool_calls": False,
                   "max_output_tokens": max_tokens, "store": False}
        request = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            # No headers, request objects, or API keys are included in the exception.
            message = exc.read(4000).decode(errors="replace").replace(self.key, "[REDACTED]")
            raise RuntimeError(f"Model API HTTP {exc.code}: {message}") from None
        if data.get("status") != "completed":
            raise RuntimeError("Model response did not complete: " + str(data.get("incomplete_details") or data.get("status")))
        return data


def validate(value, schema: dict, location: str = "arguments") -> None:
    """Validate the deliberately small JSON Schema subset used by all approved tools."""
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if expected in types and (not isinstance(value, types[expected]) or expected == "integer" and isinstance(value, bool)):
        raise ValueError(f"{location} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location} is not an allowed value")
    if expected == "object":
        if set(value) - set(schema.get("properties", {})):
            raise ValueError(f"{location} contains unexpected fields")
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{location}.{key} is required")
        for key, item in value.items():
            validate(item, schema["properties"][key], f"{location}.{key}")
    if expected == "array":
        if len(value) > 40:
            raise ValueError(f"{location} has too many items")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{location}[{index}]")
    if expected == "string" and len(value) > 6000:
        raise ValueError(f"{location} is too long")


def object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
FINISH = object_schema({"status": {"type": "string", "enum": ["complete", "blocked"]}, "summary": STRING,
    "findings": STRINGS, "evidence": STRINGS,
    "tasks": {"type": "array", "items": object_schema({
        "agent": {"type": "string", "enum": ["website", "infrastructure", "verification", "diagnosis"]}, "task": STRING})}})
