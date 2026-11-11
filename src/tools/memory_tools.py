from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Sequence
from typing import Any, ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import Tool, ToolAnnotations, ToolExecutor, register_tool
from pydantic import Field

from src.memory.store import (
    MEMORY_CATEGORIES,
    MemoryStore,
    MemoryValidationError,
    memory_enabled,
)


class SearchMemoryAction(Action):
    category: str = Field(
        description="Memory category to search: dag, modeling, or parameter.",
    )
    query: str = Field(
        description=(
            "Lexical query used to fetch entries from the selected CatPaw memory JSONL file. "
            "Whitespace-separated terms are matched as OR; explicit regex alternation with | is preserved."
        ),
    )
    limit: int = Field(default=10, description="Maximum matching lines to return.")


class SearchMemoryObservation(Observation):
    message: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


def memory_search_pattern(query: str) -> str:
    text = str(query or "").strip()
    if not text or "|" in text:
        return text
    terms = [term for term in re.split(r"\s+", text) if term]
    if len(terms) <= 1:
        return text
    return "|".join(re.escape(term) for term in terms)


class UpdateMemoryAction(Action):
    category: str = Field(
        description="Memory category to update: dag, modeling, or parameter.",
    )
    memories: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Reusable memory entries. Each entry must include content, evidence, "
            "use_case, scope, updated_at, and confidence."
        ),
    )
    evidence_paths: list[str] = Field(
        default_factory=list,
        description=("Optional additional evidence artifact paths. They are appended to each submitted memory entry."),
    )


class UpdateMemoryObservation(Observation):
    message: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


class SearchMemoryExecutor(ToolExecutor[SearchMemoryAction, SearchMemoryObservation]):
    def __call__(self, action: SearchMemoryAction, conversation=None) -> SearchMemoryObservation:
        category = str(action.category or "").strip().lower()
        query = str(action.query or "").strip()
        search_pattern = memory_search_pattern(query)
        limit = max(1, min(int(action.limit or 10), 50))
        if category not in MEMORY_CATEGORIES:
            payload = {
                "status": "error",
                "message": f"Invalid memory category {category!r}; expected one of {sorted(MEMORY_CATEGORIES)}.",
            }
            return SearchMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))
        if not query:
            payload = {"status": "error", "category": category, "message": "Memory fetch query must be non-empty."}
            return SearchMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))
        if not memory_enabled(category, "read"):
            payload = {
                "status": "disabled",
                "category": category,
                "message": "Memory reading is disabled for this category.",
            }
            return SearchMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))

        store = MemoryStore()
        path = store.path_for_category(category)
        if not path.is_file():
            payload = {
                "status": "success",
                "category": category,
                "query": query,
                "search_pattern": search_pattern,
                "memory_file": str(path),
                "matches": [],
                "message": "Memory file does not exist yet.",
            }
            return SearchMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))

        try:
            completed = subprocess.run(
                ["rg", "-n", "-i", "--", search_pattern, str(path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            payload = {
                "status": "error",
                "category": category,
                "query": query,
                "search_pattern": search_pattern,
                "memory_file": str(path),
                "message": "rg is not available in PATH.",
            }
            return SearchMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))
        except subprocess.TimeoutExpired:
            payload = {
                "status": "error",
                "category": category,
                "query": query,
                "search_pattern": search_pattern,
                "memory_file": str(path),
                "message": "Memory grep timed out.",
            }
            return SearchMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))

        if completed.returncode not in {0, 1}:
            payload = {
                "status": "error",
                "category": category,
                "query": query,
                "search_pattern": search_pattern,
                "memory_file": str(path),
                "message": completed.stderr.strip() or f"rg exited with status {completed.returncode}.",
            }
            return SearchMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))

        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        payload = {
            "status": "success",
            "category": category,
            "query": query,
            "search_pattern": search_pattern,
            "memory_file": str(path),
            "matches": lines[:limit],
            "truncated": len(lines) > limit,
        }
        return SearchMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))


def dedupe_nonempty(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def json_env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if str(item)]


def merge_action_evidence(memories: list[dict[str, Any]], evidence_paths: list[str]) -> list[dict[str, Any]]:
    if not evidence_paths:
        return memories
    merged: list[dict[str, Any]] = []
    extra = [{"path": path} for path in evidence_paths]
    for memory in memories:
        item = dict(memory)
        existing = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        item["evidence"] = [*existing, *extra]
        merged.append(item)
    return merged


def coerce_confidence(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"strong", "weak", "needs_confirmation"}:
        return text
    if text in {"high", "certain", "confident"}:
        return "strong"
    if text in {"medium", "moderate", "normal"}:
        return "weak"
    if text in {"low", "uncertain", "tentative"}:
        return "needs_confirmation"
    try:
        score = float(text)
    except ValueError:
        return text
    if score >= 0.75:
        return "strong"
    if score >= 0.45:
        return "weak"
    return "needs_confirmation"


def coerce_memory_entries(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    coerced: list[dict[str, Any]] = []
    for memory in memories:
        item = dict(memory)

        scope = item.get("scope")
        if isinstance(scope, str):
            text = scope.strip()
            if text:
                item["scope"] = {"query": text}

        evidence = item.get("evidence")
        if isinstance(evidence, str):
            text = evidence.strip()
            if text:
                item["evidence"] = [{"summary": text}]
        elif isinstance(evidence, dict):
            item["evidence"] = [evidence]

        if "confidence" in item:
            item["confidence"] = coerce_confidence(item.get("confidence"))

        coerced.append(item)
    return coerced


class UpdateMemoryExecutor(ToolExecutor[UpdateMemoryAction, UpdateMemoryObservation]):
    def __call__(self, action: UpdateMemoryAction, conversation=None) -> UpdateMemoryObservation:
        category = str(action.category or "").strip().lower()
        if category not in MEMORY_CATEGORIES:
            payload = {
                "status": "error",
                "message": f"Invalid memory category {category!r}; expected one of {sorted(MEMORY_CATEGORIES)}.",
            }
            return UpdateMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))

        if not memory_enabled(category, "write"):
            payload = {
                "status": "disabled",
                "message": "Memory writing is disabled for this category.",
                "category": category,
            }
            return UpdateMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))

        evidence_paths = dedupe_nonempty(
            [
                *json_env_list("CATPAW_MEMORY_EVIDENCE_PATHS"),
                *list(action.evidence_paths or []),
            ]
        )
        memories = coerce_memory_entries(list(action.memories or []))
        memories = merge_action_evidence(memories, evidence_paths)

        try:
            store = MemoryStore()
            accepted = store.append_memories(category, memories)
            payload = {
                "status": "success",
                "category": category,
                "submitted": len(memories),
                "accepted_or_updated": len(memories),
                "new_records": accepted,
                "memory_file": str(store.path_for_category(category)),
            }
        except MemoryValidationError as exc:
            payload = {"status": "error", "category": category, "message": str(exc)}
        except Exception as exc:
            payload = {"status": "error", "category": category, "message": str(exc)}
        return UpdateMemoryObservation(message=json.dumps(payload, indent=2, ensure_ascii=False))


class UpdateMemoryTool(ToolDefinition[UpdateMemoryAction, UpdateMemoryObservation]):
    name: ClassVar[str] = "UpdateMemory"
    DESCRIPTION: ClassVar[str] = (
        "Validate and merge categorized CatPaw memory into dag_memory.jsonl, "
        "modeling_memory.jsonl, or parameter_memory.jsonl. Use this only for "
        "verified, evidence-linked lessons; do not edit memory files manually."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=UpdateMemoryAction,
                observation_type=UpdateMemoryObservation,
                annotations=ToolAnnotations(
                    title="update_memory",
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=UpdateMemoryExecutor(),
            )
        ]


register_tool("UpdateMemory", UpdateMemoryTool)


class SearchMemoryTool(ToolDefinition[SearchMemoryAction, SearchMemoryObservation]):
    name: ClassVar[str] = "search_memory"
    DESCRIPTION: ClassVar[str] = (
        "Run rg over one categorized CatPaw memory JSONL file and return matching lines. "
        "This is read-only lexical retrieval; results are prior guidance only."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=SearchMemoryAction,
                observation_type=SearchMemoryObservation,
                annotations=ToolAnnotations(
                    title="search_memory",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=SearchMemoryExecutor(),
            )
        ]


register_tool("search_memory", SearchMemoryTool)


def get_supervisor_memory_tools_config() -> list[Tool]:
    return get_memory_read_tools_config("dag") + get_memory_update_tools_config("dag")


def get_memory_read_tools_config(category: str) -> list[Tool]:
    return [Tool(name="search_memory")] if memory_enabled(category, "read") else []


def get_memory_update_tools_config(category: str) -> list[Tool]:
    return [Tool(name="UpdateMemory")] if memory_enabled(category, "write") else []
