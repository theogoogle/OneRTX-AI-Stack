"""
title: Adaptive Long-Term Memory
author: Local AI Stack
description: Privately injects relevant long-term user memories into chats and asynchronously extracts durable preferences on always-on CPU qwen3.5-4b with zero response latency and secret filtering.
version: 1.0.0
license: MIT
"""

import asyncio
import json
import logging
import re
import urllib.request
from typing import Any, Optional

from open_webui.models.memories import Memories
from pydantic import BaseModel, Field

log = logging.getLogger("adaptive_memory")

_SECRET_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9]{16,}|tvly-[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_-]{20,}|"
    r"GB\d{2}[A-Z]{4}\d{14}|\bpassword\s*[:=]\s*\S+|\bapi[_ ]?key\s*[:=]\s*\S+)"
)
_EXTRACT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "memories",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["memories"],
            "properties": {"memories": {"type": "array", "items": {"type": "string"}}},
        },
    },
}


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c.strip()
            if isinstance(c, list):
                return "\n".join(str(p.get("text") or "") for p in c if isinstance(p, dict) and p.get("type") == "text").strip()
    return ""


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())}


def _rank_memories(memories: list[Any], query: str, limit: int, max_chars: int = 1500) -> list[str]:
    q_toks = _tokens(query)
    scored = []
    for m in memories:
        txt = str(getattr(m, "content", "") or "").strip()
        if not txt or _SECRET_RE.search(txt):
            continue
        m_toks = _tokens(txt)
        overlap = len(q_toks & m_toks)
        updated = int(getattr(m, "updated_at", 0) or 0)
        scored.append((overlap, updated, txt))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picked = []
    used = 0
    for _, _, txt in scored[: max(1, int(limit or 15))]:
        if used + len(txt) + 4 > max_chars:
            break
        picked.append(txt)
        used += len(txt) + 4
    return picked


def _is_duplicate(fact: str, existing: list[str]) -> bool:
    f_low = re.sub(r"\s+", " ", fact.lower()).strip(" .")
    f_toks = _tokens(f_low)
    for ex in existing:
        e_low = re.sub(r"\s+", " ", ex.lower()).strip(" .")
        if f_low in e_low or e_low in f_low:
            return True
        e_toks = _tokens(e_low)
        if f_toks and e_toks:
            jacc = len(f_toks & e_toks) / max(1, len(f_toks | e_toks))
            if jacc >= 0.65:
                return True
    return False


def _extract_facts_sync(user_text: str, lmstudio_url: str, model: str) -> list[str]:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 220,
        "reasoning_effort": "none",
        "response_format": _EXTRACT_SCHEMA,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract ONLY durable, high-value personal preferences, family/location/hardware/workflow facts "
                    "explicitly stated by the user in their latest message (e.g. 'User prefers concise British English', "
                    "'User prefers metric units', 'User dislikes X'). "
                    "Return an empty list `[]` for ordinary questions, one-off requests, pasted articles/code, URLs, "
                    "or anything containing passwords, API keys, bank details, or secrets. Max 2 concise items."
                ),
            },
            {"role": "user", "content": user_text[:1800]},
        ],
    }
    req = urllib.request.Request(
        lmstudio_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or "{}"
    raw = re.sub(r"(?is)<think>.*?</think>", "", raw).strip()
    obj = json.loads(raw)
    out = []
    for item in obj.get("memories") or []:
        s = re.sub(r"\s+", " ", str(item or "")).strip()
        if 8 <= len(s) <= 240 and not _SECRET_RE.search(s):
            out.append(s)
    return out[:2]


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=10, description="Filter execution priority.")
        max_injected_memories: int = Field(default=15, description="Maximum stored user memories injected per chat.")
        auto_extract: bool = Field(default=True, description="Asynchronously extract durable preferences using CPU qwen3.5-4b.")
        extract_model: str = Field(default="qwen3.5-4b", description="Always-on CPU model identifier in LM Studio.")
        lmstudio_url: str = Field(default="http://127.0.0.1:1234/v1/chat/completions", description="Local LM Studio chat endpoint.")

    def __init__(self):
        self.valves = self.Valves()
        self._bg_tasks: set[asyncio.Task] = set()

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not isinstance(body, dict) or not isinstance(__user__, dict) or not __user__.get("id"):
            return body
        msgs = body.get("messages") or []
        latest = _last_user_text(msgs)
        if not latest or latest.startswith("### Task:"):
            return body

        try:
            memories = await Memories.get_memories_by_user_id(__user__["id"])
        except Exception as e:
            log.debug("Memory fetch skipped: %s", e)
            return body
        if not memories:
            return body

        picked = _rank_memories(memories, latest, self.valves.max_injected_memories)
        if not picked:
            return body

        mem_block = "USER LONG-TERM MEMORY (apply silently as background preferences; never recite unless asked):\n" + "\n".join(
            f"- {m}" for m in picked
        )
        if msgs and msgs[0].get("role") == "system":
            if "USER LONG-TERM MEMORY" not in str(msgs[0].get("content") or ""):
                msgs[0]["content"] = (str(msgs[0].get("content") or "") + "\n\n" + mem_block).strip()
        else:
            msgs.insert(0, {"role": "system", "content": mem_block})
        body["messages"] = msgs
        return body

    async def _extract_and_store(self, user_id: str, user_text: str) -> None:
        try:
            facts = await asyncio.to_thread(
                _extract_facts_sync,
                user_text,
                self.valves.lmstudio_url,
                self.valves.extract_model,
            )
            if not facts:
                return
            existing_objs = await Memories.get_memories_by_user_id(user_id) or []
            existing = [str(getattr(m, "content", "") or "") for m in existing_objs]
            for fact in facts:
                if not _is_duplicate(fact, existing):
                    await Memories.insert_new_memory(user_id=user_id, content=fact)
                    existing.append(fact)
        except Exception as e:
            log.debug("Adaptive memory background extraction skipped: %s", e)

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.auto_extract or not isinstance(body, dict) or not isinstance(__user__, dict):
            return body
        user_id = __user__.get("id")
        if not user_id:
            return body
        latest = _last_user_text(body.get("messages") or [])
        if len(latest) < 20 or latest.startswith("### Task:") or _SECRET_RE.search(latest):
            return body

        task = asyncio.create_task(self._extract_and_store(user_id, latest))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return body
