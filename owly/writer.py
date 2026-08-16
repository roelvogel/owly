"""Cursor agent writer for digest curation (no X search)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

from owly.config import PROJECT_ROOT, get_settings
from owly.models import DigestResult, StockDigestResult
from owly.structured import parse_structured

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_WRITE_LOCK = threading.Lock()
_SCRIPT = PROJECT_ROOT / "scripts" / "cursor_write.mjs"
PromptFn = Callable[[str], Tuple[str, int, int]]


class CursorWriter:
    def __init__(self, prompt_fn: Optional[PromptFn] = None) -> None:
        settings = get_settings()
        if prompt_fn is None and not settings.cursor_api_key:
            raise ValueError(
                "CURSOR_API_KEY is not set. Create a key at https://cursor.com/dashboard/api "
                "and add it to .env (or export it in the environment)."
            )
        self.settings = settings
        self._prompt_fn = prompt_fn or self._agent_prompt

    def _node_bin(self) -> str:
        found = shutil.which("node")
        if found:
            return found
        fallback = Path(r"C:\Program Files\nodejs\node.exe")
        if fallback.exists():
            return str(fallback)
        raise RuntimeError(
            "Node.js is required for Cursor writing (the Python SDK needs 3.10+). "
            "Install Node 22+ and ensure `node` is on PATH."
        )

    def _agent_prompt(self, message: str) -> Tuple[str, int, int]:
        if not _SCRIPT.exists():
            raise RuntimeError(f"Missing Cursor writer script: {_SCRIPT}")
        env = os.environ.copy()
        env["CURSOR_API_KEY"] = self.settings.cursor_api_key
        env["CURSOR_MODEL"] = self.settings.cursor_model
        workspace = self.settings.data_dir / "cursor-writer"
        workspace.mkdir(parents=True, exist_ok=True)
        env["OWLY_ROOT"] = str(workspace)
        with _WRITE_LOCK:
            proc = subprocess.run(
                [self._node_bin(), str(_SCRIPT)],
                input=message,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=str(PROJECT_ROOT),
                timeout=self.settings.cursor_timeout_seconds,
            )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Cursor writer failed (exit {proc.returncode}): {err[:2000]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Cursor writer returned invalid envelope: {proc.stdout[:500]}") from exc
        status = payload.get("status")
        text = payload.get("text") or ""
        if status and status != "finished":
            raise RuntimeError(f"Cursor writer run status={status}: {text[:500]}")
        if not text.strip():
            raise RuntimeError("Cursor writer returned empty text")
        return text, int(payload.get("input_tokens") or 0), int(payload.get("output_tokens") or 0)

    def _complete(self, prompt: str, result_model: Type[T]) -> Tuple[T, int, int]:
        raw, in_tok, out_tok = self._prompt_fn(prompt)
        try:
            return parse_structured(raw, result_model), in_tok, out_tok
        except Exception as exc:
            logger.warning("Invalid Cursor JSON, retrying once: %s", exc)
            raw, in_tok, out_tok = self._prompt_fn(prompt)
            return parse_structured(raw, result_model), in_tok, out_tok

    def write_main_digest(
        self,
        rss_context: str,
        topics: list[str],
        x_candidates: str,
        hours_label: str,
    ) -> Tuple[DigestResult, int, int]:
        topic_list = ", ".join(topics) if topics else "technology and business"
        topic_mix = ""
        if topics:
            topic_mix = (
                f"Enabled topics: {topic_list}. Include at least one item per topic when "
                "credible signal exists. Do not let a single topic occupy more than half "
                "the edition.\n"
            )
        schema = json.dumps(DigestResult.model_json_schema(), indent=2)
        prompt = f"""You are a personal news curator for an e-ink reader. Produce 6-12 highest-signal items (prefer 8-10). Never pad with weak items.

Do not search the web or X. Do not use tools. Use only the RSS articles and X post candidates below.

X posts below were already collected from {hours_label}. Treat them as discovery, not as a license to invent facts.
{topic_mix}
RSS articles (downloaded locally; source of truth for RSS-grounded items):

{rss_context or "(No RSS items available this run.)"}

Collected X posts:

{x_candidates or "(No X posts collected this run.)"}

For each item:
- RSS-sourced (origin=rss): rewrite a 3-6 paragraph digest FROM the provided article text. Do not paste the source. Do not invent facts that are not in the text. Put the article URL in sources. Never write a full article from a headline or a one-line RSS blurb.
- Mixed (origin=mixed): RSS supplies the facts; X supplies discovery. Cite both URLs.
- X-only (origin=x): only when no RSS article grounds the story. Quote the posts. Do not invent context, numbers, or motives beyond the posts. Keep it shorter than RSS items.

Selection criteria:
- Prioritize actionable business, technology, and market-moving news
- Prefer primary sources and credible reporting
- Avoid duplicate stories covering the same event
- Populate each item's sources field with the RSS and/or X URLs you used (internal tracking only)

Return ONLY valid JSON matching this schema (no markdown fences, no commentary):

{schema}"""
        result, in_tok, out_tok = self._complete(prompt, DigestResult)
        logger.info(
            "Cursor main digest selected %d items: %s",
            len(result.items),
            "; ".join(item.title for item in result.items),
        )
        return result, in_tok, out_tok

    def write_stock_digest(
        self,
        symbol: str,
        company: str,
        candidates: str,
        *,
        permissive: bool = False,
        rss_context: str = "",
    ) -> Tuple[StockDigestResult, int, int]:
        rss_block = ""
        if (rss_context or "").strip():
            rss_block = (
                "\nAlso consider these locally downloaded RSS articles that mention the company "
                "or ticker. You may cite their URLs in sources alongside X posts.\n\n"
                f"{rss_context}\n"
            )
        schema = json.dumps(StockDigestResult.model_json_schema(), indent=2)
        if permissive:
            body = f"""Curate a stock digest for {company} (${symbol}) from these collected X posts:

{candidates}
{rss_block}
Select up to 10 useful items for a personal investor. Include sentiment shifts, technical levels, and community discussion when that is what the posts contain. Also keep real company moves such as ad/marketing campaigns and new state/market launches.

Rules:
- Every item must cite at least one URL from the candidates or RSS articles above in sources (internal only).
- Summaries may be up to one paragraph; state concrete who/what/when; avoid generic filler.
- If 3 or more distinct substantive posts exist, return at least 3 items.
- Skip only pure spam. Never return a "no news" placeholder item.
- Set ticker to "{symbol}"."""
        else:
            body = f"""Curate a stock digest for {company} (${symbol}) from these collected X posts:

{candidates}
{rss_block}
Select up to 10 distinct items for an e-ink reader. Prioritize:
1. Company or catalyst news (earnings, guidance, products, deals, ad/marketing campaigns, brand campaigns, geographic expansion, new state/market launches)
2. Analyst commentary
3. Credible market analysis
4. Notable sentiment shifts (institutional ownership, mainstream mentions)

Treat new ad campaigns and new state/market launches as real company news — do not drop them as fluff.

Rules:
- Every item must cite at least one URL from the candidates or RSS articles above in sources (internal only).
- Summaries may be up to one paragraph; state concrete who/what/when; avoid generic filler.
- If the candidates or RSS articles contain substantive discussion about {company} or ${symbol}, return at least one item.
- Never return a "no news" placeholder item.
- Set ticker to "{symbol}"."""
        prompt = f"""{body}

Do not search X or the web. Use only the material above.
Return ONLY valid JSON matching this schema (no markdown fences, no commentary):

{schema}"""
        result, in_tok, out_tok = self._complete(prompt, StockDigestResult)
        result.ticker = symbol
        return result, in_tok, out_tok
