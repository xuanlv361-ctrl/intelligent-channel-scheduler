"""Fail-closed loader and schema validation for canonical formal Skills."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "formal_agent_skill_registry_v1.json"
SAFE_SKILL_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class FormalAgentSkillError(ValueError):
    pass


def validate_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Validate the deliberately small JSON-Schema subset used by the registry."""
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []
    matches = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if types and not any(matches.get(kind, False) for kind in types):
        raise FormalAgentSkillError(f"schema_type_mismatch:{path}")
    if "const" in schema and value != schema["const"]:
        raise FormalAgentSkillError(f"schema_const_mismatch:{path}")
    if "enum" in schema and value not in schema["enum"]:
        raise FormalAgentSkillError(f"schema_enum_mismatch:{path}")
    if isinstance(value, Mapping):
        required = set(schema.get("required", []))
        if not required.issubset(value):
            raise FormalAgentSkillError(f"schema_required_missing:{path}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise FormalAgentSkillError(f"schema_additional_property:{path}")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}")
    if isinstance(value, list):
        if len(value) > int(schema.get("maxItems", len(value))):
            raise FormalAgentSkillError(f"schema_array_too_large:{path}")
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            raise FormalAgentSkillError(f"schema_array_not_unique:{path}")
        for index, item in enumerate(value):
            validate_schema(item, schema.get("items", {}), f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise FormalAgentSkillError(f"schema_string_too_short:{path}")
        if "pattern" in schema and not re.fullmatch(str(schema["pattern"]), value):
            raise FormalAgentSkillError(f"schema_pattern_mismatch:{path}")
        if len(value) > int(schema.get("maxLength", len(value))):
            raise FormalAgentSkillError(f"schema_string_too_long:{path}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise FormalAgentSkillError(f"schema_number_too_small:{path}")
        if "minimum" in schema and value < schema["minimum"]:
            raise FormalAgentSkillError(f"schema_number_too_small:{path}")
        if "maximum" in schema and value > schema["maximum"]:
            raise FormalAgentSkillError(f"schema_number_too_large:{path}")


class FormalAgentSkillRegistry:
    def __init__(self, path: str | Path = DEFAULT_REGISTRY, *, root: str | Path = ROOT):
        self.root = Path(root).resolve()
        self.path = Path(path)
        try:
            self.document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FormalAgentSkillError("formal_skill_registry_unreadable") from exc
        self.invocation_schema = self._load_schema("invocation_schema")
        self.result_schema = self._load_schema("result_schema")
        rows = self.document.get("skills")
        minimum = self.document.get("minimum_skill_count")
        maximum = self.document.get("maximum_skill_count")
        if (not isinstance(rows, list) or isinstance(minimum, bool)
                or not isinstance(minimum, int) or isinstance(maximum, bool)
                or not isinstance(maximum, int) or not 1 <= minimum <= maximum <= 64
                or not minimum <= len(rows) <= maximum):
            raise FormalAgentSkillError("formal_skill_registry_count_out_of_bounds")
        self.skills: dict[str, dict[str, Any]] = {}
        for raw in rows:
            self._register(dict(raw))

    def _safe_path(self, relative: str) -> Path:
        target = (self.root / relative).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise FormalAgentSkillError("formal_skill_path_escape") from exc
        if "evidence" in {part.casefold() for part in target.parts}:
            raise FormalAgentSkillError("formal_skill_protected_evidence_path")
        return target

    def _load_schema(self, key: str) -> dict[str, Any]:
        try:
            return json.loads(self._safe_path(str(self.document[key])).read_text(encoding="utf-8"))
        except (KeyError, OSError, json.JSONDecodeError) as exc:
            raise FormalAgentSkillError(f"formal_skill_{key}_invalid") from exc

    def _register(self, row: dict[str, Any]) -> None:
        skill_id = str(row.get("skill_id", ""))
        if not SAFE_SKILL_ID.fullmatch(skill_id) or skill_id in self.skills:
            raise FormalAgentSkillError("formal_skill_id_invalid_or_duplicate")
        if row.get("read_only") is not True or not str(row.get("binding", "")).endswith(".read"):
            raise FormalAgentSkillError("formal_skill_binding_not_read_only")
        artifact = self._safe_path(str(row.get("artifact", "")))
        openai_yaml = artifact.parent / "agents" / "openai.yaml"
        try:
            content = artifact.read_bytes()
            text = content.decode("utf-8")
            metadata = openai_yaml.read_text(encoding="utf-8")
        except OSError as exc:
            raise FormalAgentSkillError("formal_skill_artifact_missing") from exc
        if not text.startswith("---\n") or f"name: {skill_id}\n" not in text or "description:" not in text:
            raise FormalAgentSkillError("formal_skill_frontmatter_invalid")
        if "default_prompt:" not in metadata or f"${skill_id}" not in metadata:
            raise FormalAgentSkillError("formal_skill_openai_metadata_invalid")
        canonical_content = text.replace("\r\n", "\n").encode("utf-8")
        digest = hashlib.sha256(canonical_content).hexdigest()
        if digest != row.get("artifact_sha256"):
            raise FormalAgentSkillError("formal_skill_artifact_hash_mismatch")
        if not isinstance(row.get("input_schema"), Mapping) or not isinstance(row.get("output_schema"), Mapping):
            raise FormalAgentSkillError("formal_skill_schema_missing")
        self.skills[skill_id] = row

    def get(self, skill_id: str) -> dict[str, Any]:
        try:
            return dict(self.skills[skill_id])
        except KeyError as exc:
            raise FormalAgentSkillError("formal_skill_not_registered") from exc

    def discover(self) -> list[dict[str, Any]]:
        return [{k: row[k] for k in ("skill_id", "version", "binding", "read_only", "triggers")}
                for row in self.skills.values()]

    def validate_arguments(self, skill_id: str, arguments: Mapping[str, Any]) -> None:
        validate_schema(dict(arguments), self.get(skill_id)["input_schema"], "$.arguments")
