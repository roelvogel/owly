"""Grok API client using xAI Responses API with x_search."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from owly.config import get_settings
from owly.models import DigestResult, StockDigestResult, StockItem

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_MAX_STOCK_API_CALLS = 3

_PLACEHOLDER_PATTERNS = re.compile(
    r"no (high-signal|significant|recent|matching|relevant|credible)|"
    r"no news|no results|no posts|no updates|nothing found|unable to retrieve|"
    r"absence of|quiet period|zero matching",
    re.IGNORECASE,
)
_X_URL_PATTERN = re.compile(r"https?://(?:x\.com|twitter\.com)/\S+", re.IGNORECASE)


class GrokClient:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.xai_api_key:
            raise ValueError("XAI_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.xai_api_key,
            base_url=settings.xai_base_url,
        )
        self.model = settings.xai_model

    def _date_window(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=self.settings.ingestion_hours)
        return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")

    def _schema_format(self, model: Type[BaseModel], name: str) -> dict[str, Any]:
        schema = model.model_json_schema()
        return {
            "format": {
                "type": "json_schema",
                "name": name,
                "schema": schema,
                "strict": True,
            }
        }

    def _x_search_tools(self) -> list[dict[str, Any]]:
        from_date, to_date = self._date_window()
        return [
            {
                "type": "x_search",
                "from_date": from_date,
                "to_date": to_date,
            }
        ]

    def _extract_output_text(self, response: Any) -> str:
        if hasattr(response, "output_text") and response.output_text:
            return response.output_text
        chunks: list[str] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) == "message":
                for content in getattr(item, "content", []) or []:
                    if getattr(content, "type", None) == "output_text":
                        chunks.append(getattr(content, "text", ""))
        return "".join(chunks)

    def _usage(self, response: Any) -> Tuple[int, int]:
        usage = getattr(response, "usage", None)
        if not usage:
            return 0, 0
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        return input_tokens, output_tokens

    def _call(
        self,
        prompt: str,
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        result_model: Optional[Type[T]] = None,
        schema_name: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Tuple[str, int, int]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": [{"role": "user", "content": prompt}],
            "max_output_tokens": max_output_tokens or self.settings.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if result_model and schema_name:
            kwargs["text"] = self._schema_format(result_model, schema_name)

        response = self.client.responses.create(**kwargs)
        raw = self._extract_output_text(response)
        if not raw:
            raise RuntimeError("Grok returned an empty response")
        in_tok, out_tok = self._usage(response)
        return raw, in_tok, out_tok

    def _parse_structured(self, raw: str, result_model: Type[T]) -> T:
        try:
            return result_model.model_validate_json(raw)
        except Exception:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1:
                raise
            return result_model.model_validate_json(raw[start : end + 1])

    def _is_placeholder(self, item: StockItem) -> bool:
        blob = f"{item.title} {item.summary}"
        if _PLACEHOLDER_PATTERNS.search(blob):
            return True
        if not item.sources:
            return True
        return False

    def _filter_stock_items(self, items: list[StockItem]) -> list[StockItem]:
        filtered: list[StockItem] = []
        for item in items:
            item.sources = [
                url
                for url in item.sources
                if url.startswith(("https://x.com/", "https://twitter.com/"))
            ]
            if not self._is_placeholder(item):
                filtered.append(item)
        return filtered

    def _has_x_post_candidates(self, text: str) -> bool:
        return bool(_X_URL_PATTERN.search(text))

    def _collect_stock_posts_prompt(self, symbol: str, company: str, *, broad: bool) -> str:
        if broad:
            return f"""Use x_search to find the latest X posts mentioning ${symbol}, {symbol}, or {company}.

List up to 20 distinct posts with:
- Post URL (x.com or twitter.com)
- @handle
- Short quote or one-line summary

Cast a wide net: include earnings, guidance, analyst notes, product news, institutional ownership, sentiment, and technical discussion.

Exclude only obvious spam or promo bots. Do not judge quality yet — gather candidates first."""

        hours = self.settings.ingestion_hours
        return f"""Use x_search to find recent X posts about {company} (ticker ${symbol}).

List up to 20 distinct posts from the last {hours} hours (extend to 48h only if results are sparse).
For each post provide:
- Post URL (x.com or twitter.com)
- @handle
- Short quote or one-line summary

Include posts about earnings, guidance, analyst takes, product or company news, institutional ownership, notable sentiment, and credible market discussion.

Exclude only obvious spam bots and generic trading-group promos. Do NOT filter for "high signal" yet.
If posts exist, list them. Do not say "no posts found" without searching."""

    def _curate_stock_posts_prompt(
        self,
        symbol: str,
        company: str,
        candidates: str,
        *,
        permissive: bool,
    ) -> str:
        if permissive:
            return f"""Curate a stock digest for {company} (${symbol}) from these collected X posts:

{candidates}

Select up to 10 useful items for a personal investor. Include sentiment shifts, technical levels, and community discussion when that is what the posts contain.

Rules:
- Every item must cite at least one X post URL from the candidates above in sources (internal only).
- Summaries must state concrete who/what/when; avoid generic filler.
- If 3 or more distinct substantive posts exist, return at least 3 items.
- Skip only pure spam. Never return a "no news" placeholder item."""

        return f"""Curate a stock digest for {company} (${symbol}) from these collected X posts:

{candidates}

Select up to 10 distinct items for an e-ink reader. Prioritize:
1. Company or catalyst news (earnings, guidance, products, deals)
2. Analyst commentary
3. Credible market analysis
4. Notable sentiment shifts (institutional ownership, mainstream mentions)

Rules:
- Every item must cite at least one X post URL from the candidates above in sources (internal only).
- Summaries must state concrete who/what/when; avoid generic filler.
- If the candidates contain substantive discussion about {company} or ${symbol}, return at least one item.
- Never return a "no news" placeholder item."""

    def _recover_stock_prompt(self, symbol: str, company: str) -> str:
        return f"""Search X for recent posts about {company} (ticker ${symbol}).

Return up to 10 digest items from any non-spam posts in the last {self.settings.ingestion_hours} hours, including sentiment and technical discussion if needed.

Every source must be a real x.com or twitter.com post URL in the sources field (internal only). Summaries must be concrete; no filler. If posts exist, return at least one item. No placeholder "no news" entries."""

    def _collect_stock_posts(
        self,
        symbol: str,
        company: str,
        *,
        broad: bool = False,
    ) -> Tuple[str, int, int]:
        tools = [{"type": "x_search"}] if broad else self._x_search_tools()
        return self._call(
            self._collect_stock_posts_prompt(symbol, company, broad=broad),
            tools=tools,
            max_output_tokens=3000,
        )

    def _curate_stock_posts(
        self,
        symbol: str,
        company: str,
        candidates: str,
        *,
        permissive: bool = False,
    ) -> Tuple[StockDigestResult, int, int]:
        result, input_tokens, output_tokens = self._call_structured(
            self._curate_stock_posts_prompt(symbol, company, candidates, permissive=permissive),
            StockDigestResult,
            f"stock_{symbol}_{'permissive' if permissive else 'curate'}",
            tools=None,
        )
        result.ticker = symbol
        result.items = self._filter_stock_items(result.items)
        return result, input_tokens, output_tokens

    def _recover_stock_digest(
        self,
        symbol: str,
        company: str,
    ) -> Tuple[StockDigestResult, int, int]:
        result, input_tokens, output_tokens = self._call_structured(
            self._recover_stock_prompt(symbol, company),
            StockDigestResult,
            f"stock_{symbol}_recover",
            tools=self._x_search_tools(),
        )
        result.ticker = symbol
        result.items = self._filter_stock_items(result.items)
        return result, input_tokens, output_tokens

    def _call_structured(
        self,
        prompt: str,
        result_model: Type[T],
        schema_name: str,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> Tuple[T, int, int]:
        raw, in_tok, out_tok = self._call(
            prompt,
            tools=tools,
            result_model=result_model,
            schema_name=schema_name,
        )
        return self._parse_structured(raw, result_model), in_tok, out_tok

    def generate_main_digest(
        self,
        rss_context: str,
        topics: list[str],
    ) -> Tuple[DigestResult, int, int]:
        topic_list = ", ".join(topics) if topics else "technology and business"
        from_date, to_date = self._date_window()
        prompt = f"""You are a personal news curator. Produce exactly 10 highest-signal items for an e-ink reader.

Use the x_search tool to find real-time posts on X (Twitter) from the last {self.settings.ingestion_hours} hours about: {topic_list}.

RSS articles below were already downloaded locally (excerpts only). Use them for selection and summaries. Do not use tools to fetch or re-read those article URLs.

{rss_context or "(No RSS items available this run.)"}

Selection criteria:
- Prioritize actionable business, technology, and market-moving news
- Prefer primary sources and credible reporting
- Avoid duplicate stories covering the same event
- Summaries must state concrete who/what/when; avoid generic filler
- Populate each item's sources field with the RSS or X URL you used (internal tracking only; not shown to the reader)

Return JSON matching the schema with exactly 10 items when possible; fewer only if insufficient signal exists."""

        return self._call_structured(
            prompt,
            DigestResult,
            "main_digest",
            tools=[{"type": "x_search", "from_date": from_date, "to_date": to_date}],
        )

    def generate_stock_digest(
        self,
        ticker: str,
        company_name: Optional[str] = None,
    ) -> Tuple[StockDigestResult, int, int]:
        symbol = ticker.upper().lstrip("$")
        company = company_name or symbol
        total_in = 0
        total_out = 0
        calls = 0

        candidates, in_tok, out_tok = self._collect_stock_posts(symbol, company)
        total_in += in_tok
        total_out += out_tok
        calls += 1
        logger.info(
            "Stock %s call %d/%d: collect (%d candidate URLs)",
            symbol,
            calls,
            _MAX_STOCK_API_CALLS,
            len(_X_URL_PATTERN.findall(candidates)),
        )

        if not self._has_x_post_candidates(candidates) and calls < _MAX_STOCK_API_CALLS:
            candidates, in_tok, out_tok = self._collect_stock_posts(symbol, company, broad=True)
            total_in += in_tok
            total_out += out_tok
            calls += 1
            logger.info(
                "Stock %s call %d/%d: broad collect (%d candidate URLs)",
                symbol,
                calls,
                _MAX_STOCK_API_CALLS,
                len(_X_URL_PATTERN.findall(candidates)),
            )

        result = StockDigestResult(ticker=symbol, items=[])

        if self._has_x_post_candidates(candidates) and calls < _MAX_STOCK_API_CALLS:
            result, in_tok, out_tok = self._curate_stock_posts(symbol, company, candidates)
            total_in += in_tok
            total_out += out_tok
            calls += 1
            logger.info(
                "Stock %s call %d/%d: curate (%d items)",
                symbol,
                calls,
                _MAX_STOCK_API_CALLS,
                len(result.items),
            )

            if not result.items and calls < _MAX_STOCK_API_CALLS:
                result, in_tok, out_tok = self._curate_stock_posts(
                    symbol,
                    company,
                    candidates,
                    permissive=True,
                )
                total_in += in_tok
                total_out += out_tok
                calls += 1
                logger.info(
                    "Stock %s call %d/%d: permissive curate (%d items)",
                    symbol,
                    calls,
                    _MAX_STOCK_API_CALLS,
                    len(result.items),
                )

        elif calls < _MAX_STOCK_API_CALLS:
            result, in_tok, out_tok = self._recover_stock_digest(symbol, company)
            total_in += in_tok
            total_out += out_tok
            calls += 1
            logger.info(
                "Stock %s call %d/%d: recover (%d items)",
                symbol,
                calls,
                _MAX_STOCK_API_CALLS,
                len(result.items),
            )

        if not result.items:
            logger.warning("Stock digest for %s is empty after %d API call(s)", symbol, calls)

        return result, total_in, total_out
