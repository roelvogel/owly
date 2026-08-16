"""Parse JSON objects from model text, including markdown fences."""

from __future__ import annotations

import re
from typing import Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_structured(raw: str, result_model: Type[T]) -> T:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _FENCE.sub("", text).strip()
    try:
        return result_model.model_validate_json(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise
        return result_model.model_validate_json(text[start : end + 1])
