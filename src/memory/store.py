from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.utils.config import load_config, memory_dir

DAG_MEMORY_FILENAME = "dag_memory.jsonl"
MODELING_MEMORY_FILENAME = "modeling_memory.jsonl"
PARAMETER_MEMORY_FILENAME = "parameter_memory.jsonl"
INDEX_METADATA_FILENAME = "index_metadata.json"

MEMORY_CATEGORIES = {
    "dag": DAG_MEMORY_FILENAME,
    "modeling": MODELING_MEMORY_FILENAME,
    "parameter": PARAMETER_MEMORY_FILENAME,
}

VALID_CONFIDENCE = {"strong", "weak", "needs_confirmation"}


class MemoryValidationError(ValueError):
    pass


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def stable_memory_id(category: str, payload: dict[str, Any]) -> str:
    parts = [
        category,
        str(payload.get("content") or ""),
        str(payload.get("use_case") or ""),
        json.dumps(payload.get("scope") or {}, sort_keys=True, ensure_ascii=False),
    ]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{category}_{digest}"


def memory_enabled(category: str, access: str = "read") -> bool:
    """Categories opt in independently; experiment flags can disable access."""
    return bool(load_config().get("memory", {}).get(category, False)) and all(
        os.getenv(name, "1").strip().lower() not in {"0", "false", "no", "off"}
        for name in ("CATPAW_MEMORY_ENABLED", f"CATPAW_MEMORY_{access.upper()}")
    )


@dataclass
class MemoryRecord:
    memory_id: str
    content: str
    evidence: list[dict[str, Any]]
    use_case: str
    scope: dict[str, Any]
    updated_at: str
    confidence: str
    record_kind: str = "memory"
    category: str = ""
    created_at: str = ""
    keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value not in ("", [], {})}

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, category: str = "") -> MemoryRecord:
        normalized = normalize_memory_payload(payload, category=category)
        return cls(**normalized)


def normalize_memory_payload(payload: dict[str, Any], *, category: str) -> dict[str, Any]:
    if category not in MEMORY_CATEGORIES:
        raise MemoryValidationError(f"Unknown memory category: {category}")
    if not isinstance(payload, dict):
        raise MemoryValidationError("Memory entry must be an object.")

    content = str(payload.get("content") or "").strip()
    use_case = str(payload.get("use_case") or "").strip()
    updated_at = str(payload.get("updated_at") or "").strip()
    confidence = str(payload.get("confidence") or "").strip()
    evidence = dedupe_dicts(normalize_evidence(payload.get("evidence")))
    scope = payload.get("scope")

    missing = [
        name
        for name, value in (
            ("content", content),
            ("evidence", evidence),
            ("use_case", use_case),
            ("scope", scope),
            ("updated_at", updated_at),
            ("confidence", confidence),
        )
        if value in ("", [], {}, None)
    ]
    if missing:
        raise MemoryValidationError(f"Missing required memory field(s): {', '.join(missing)}")
    if not isinstance(scope, dict):
        raise MemoryValidationError("Memory scope must be an object.")
    if confidence not in VALID_CONFIDENCE:
        raise MemoryValidationError(f"Invalid confidence={confidence!r}; expected one of {sorted(VALID_CONFIDENCE)}.")

    normalized = {
        "memory_id": str(payload.get("memory_id") or "").strip(),
        "content": content,
        "evidence": evidence,
        "use_case": use_case,
        "scope": clean_dict(scope),
        "updated_at": updated_at,
        "confidence": confidence,
        "record_kind": "memory",
        "category": category,
        "created_at": str(payload.get("created_at") or updated_at).strip(),
        "keywords": [str(item).strip() for item in payload.get("keywords") or [] if str(item).strip()][:40],
    }
    if not normalized["memory_id"]:
        normalized["memory_id"] = stable_memory_id(category, normalized)
    return normalized


def normalize_evidence(value: Any) -> list[dict[str, Any]]:
    raw_items = value if isinstance(value, list) else []
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if isinstance(item, str):
            text = item.strip()
            if text:
                normalized.append({"path": text})
            continue
        if not isinstance(item, dict):
            continue
        cleaned = clean_dict(
            {
                "path": item.get("path"),
                "summary": item.get("summary"),
                "pointer": item.get("pointer"),
            }
        )
        if cleaned:
            normalized.append(cleaned)
    return normalized


def clean_dict(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            cleaned[key] = [item for item in value if item not in (None, "", [], {})]
        else:
            cleaned[key] = value
    return cleaned


class MemoryStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser().resolve() if root else memory_dir()

    @property
    def dag_path(self) -> Path:
        return self.root / DAG_MEMORY_FILENAME

    @property
    def modeling_path(self) -> Path:
        return self.root / MODELING_MEMORY_FILENAME

    @property
    def parameter_path(self) -> Path:
        return self.root / PARAMETER_MEMORY_FILENAME

    @property
    def metadata_path(self) -> Path:
        return self.root / INDEX_METADATA_FILENAME

    def path_for_category(self, category: str) -> Path:
        if category not in MEMORY_CATEGORIES:
            raise MemoryValidationError(f"Unknown memory category: {category}")
        return self.root / MEMORY_CATEGORIES[category]

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def append_memories(self, category: str, memories: Iterable[dict[str, Any] | MemoryRecord]) -> int:
        self.ensure()
        lock_path = self.root / ".memory.lock"
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self.append_memories_locked(category, memories)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def append_memories_locked(self, category: str, memories: Iterable[dict[str, Any] | MemoryRecord]) -> int:
        path = self.path_for_category(category)
        existing = self.load_memories(category)
        by_id = {record.memory_id: record for record in existing}
        before = len(by_id)

        for item in memories:
            record = item if isinstance(item, MemoryRecord) else MemoryRecord.from_dict(item, category=category)
            prior = by_id.get(record.memory_id)
            if prior:
                record = self.merge_record(prior, record)
            by_id[record.memory_id] = record

        text = "".join(
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n" for record in by_id.values()
        )
        path.write_text(text, encoding="utf-8")
        self.write_index_metadata()
        return max(len(by_id) - before, 0)

    def load_memories(self, category: str) -> list[MemoryRecord]:
        path = self.path_for_category(category)
        records: list[MemoryRecord] = []
        for payload in self.read_jsonl(path):
            try:
                records.append(MemoryRecord.from_dict(payload, category=category))
            except MemoryValidationError:
                continue
        return records

    def load_all_memories(self) -> dict[str, list[MemoryRecord]]:
        return {category: self.load_memories(category) for category in MEMORY_CATEGORIES}

    @staticmethod
    def merge_record(prior: MemoryRecord, new: MemoryRecord) -> MemoryRecord:
        evidence = dedupe_dicts([*prior.evidence, *new.evidence])
        keywords = sorted(set(prior.keywords) | set(new.keywords))
        created_at = prior.created_at or new.created_at
        return MemoryRecord(
            memory_id=prior.memory_id,
            content=new.content or prior.content,
            evidence=evidence,
            use_case=new.use_case or prior.use_case,
            scope=clean_dict({**prior.scope, **new.scope}),
            updated_at=new.updated_at or prior.updated_at,
            confidence=new.confidence or prior.confidence,
            record_kind="memory",
            category=new.category or prior.category,
            created_at=created_at,
            keywords=keywords,
        )

    def write_index_metadata(self) -> None:
        self.ensure()
        counts = {category: len(self.read_jsonl(self.path_for_category(category))) for category in MEMORY_CATEGORIES}
        payload = {
            "updated_at": utcnow(),
            "memory_records": counts,
            "files": {category: str(self.path_for_category(category)) for category in MEMORY_CATEGORIES},
        }
        self.metadata_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records


def dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output
