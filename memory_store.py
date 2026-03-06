import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import process

MEMORY_FILE = "assistant_memory.json"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_memory() -> Dict[str, Any]:
    return {
        "facts": [],
        "updated_at": _now_str(),
    }


def load_memory() -> Dict[str, Any]:
    if not os.path.exists(MEMORY_FILE):
        return _default_memory()
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_memory()
        if "facts" not in data or not isinstance(data["facts"], list):
            data["facts"] = []
        if "updated_at" not in data:
            data["updated_at"] = _now_str()
        return data
    except (json.JSONDecodeError, OSError):
        return _default_memory()


def save_memory(memory: Dict[str, Any]) -> None:
    memory["updated_at"] = _now_str()
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)


def _normalize(text: str) -> str:
    return " ".join(text.lower().strip().split())


def remember_fact(text: str) -> Tuple[bool, str]:
    fact_text = text.strip()
    if not fact_text:
        return False, ""

    memory = load_memory()
    normalized = _normalize(fact_text)

    for item in memory["facts"]:
        existing_text = item.get("text", "")
        if _normalize(existing_text) == normalized:
            item["last_seen_at"] = _now_str()
            save_memory(memory)
            return False, existing_text

    memory["facts"].append(
        {
            "text": fact_text,
            "created_at": _now_str(),
            "last_seen_at": _now_str(),
        }
    )
    save_memory(memory)
    return True, fact_text


def recall_facts(query: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    memory = load_memory()
    facts = memory.get("facts", [])
    if not facts:
        return []

    safe_limit = max(1, min(limit, 20))
    if not query or not query.strip():
        return facts[-safe_limit:][::-1]

    choices = [item.get("text", "") for item in facts]
    matches = process.extract(query.strip(), choices, limit=min(safe_limit, len(choices)))
    matched = []
    for _text, score, idx in matches:
        if score >= 55:
            matched.append(facts[idx])
    return matched


def forget_fact(query: str) -> Tuple[bool, str]:
    target = query.strip()
    if not target:
        return False, ""

    memory = load_memory()
    facts = memory.get("facts", [])
    if not facts:
        return False, ""

    choices = [item.get("text", "") for item in facts]
    match = process.extractOne(target, choices)
    if not match or match[1] < 60:
        return False, ""

    idx = match[2]
    removed = facts.pop(idx)
    save_memory(memory)
    return True, removed.get("text", "")


def render_memory_context(limit: int = 8) -> str:
    facts = recall_facts(limit=limit)
    if not facts:
        return "No saved user memory yet."
    lines = ["Saved user memory:"]
    for item in facts:
        lines.append(f"- {item.get('text', '')}")
    return "\n".join(lines)
