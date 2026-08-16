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
    r"^(?:no (?:high-signal|significant|recent|matching|relevant|credible)|"
    r"no news|no results|no posts|no updates|nothing found|unable to retrieve|"
    r"absence of|quiet period|zero matching)\b",
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

    def _hours_label(self) -> str:
        hours = self.settings.ingestion_hours
        return f"the last {hours} hours (ignore older posts even if they fall on the same calendar day)"

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

    def _call_structured(
        self,
        prompt: str,
        result_model: Type[T],
        schema_name: str,
        tools: Optional[list[dict[str, Any]]] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Tuple[T, int, int]:
        raw, in_tok, out_tok = self._call(
            prompt,
            tools=tools,
            result_model=result_model,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
        )
        try:
            return self._parse_structured(raw, result_model), in_tok, out_tok
        except Exception as exc:
            logger.warning("Invalid Grok JSON, retrying once: %s", exc)
            raw, in_tok, out_tok = self._call(
                prompt,
                tools=tools,
                result_model=result_model,
                schema_name=schema_name,
                max_output_tokens=max_output_tokens,
            )
            return self._parse_structured(raw, result_model), in_tok, out_tok

    def _is_placeholder(self, item: StockItem) -> bool:
        if not item.sources:
            return True
        return bool(_PLACEHOLDER_PATTERNS.match(item.title.strip()))

    def _filter_stock_items(self, items: list[StockItem]) -> list[StockItem]:
        filtered: list[StockItem] = []
        for item in items:
            item.sources = [
                url.strip()
                for url in item.sources
                if url.strip().startswith(("https://", "http://"))
            ]
            if not self._is_placeholder(item):
                filtered.append(item)
        return filtered

    def _has_x_post_candidates(self, text: str) -> bool:
        return bool(_X_URL_PATTERN.search(text))

    def _rss_stock_block(self, rss_context: str) -> str:
        if not (rss_context or "").strip():
            return ""
        return (
            "\nAlso consider these locally downloaded RSS articles that mention the company "
            "or ticker. You may cite their URLs in sources alongside X posts.\n\n"
            f"{rss_context}\n"
        )

    def _collect_stock_posts_prompt(self, symbol: str, company: str, *, broad: bool) -> str:
        if broad:
            return f"""Use x_search to find the latest X posts mentioning ${symbol}, {symbol}, or {company}.

List up to 20 distinct posts with:
- Post URL (x.com or twitter.com)
- @handle
- Short quote or one-line summary

Cast a wide net: include earnings, guidance, analyst notes, product news, ad/marketing campaigns, brand campaigns, geographic expansion or new state/market launches, institutional ownership, sentiment, and technical discussion.

Exclude only obvious spam or promo bots. Do not judge quality yet — gather candidates first."""

        return f"""Use x_search to find recent X posts about {company} (ticker ${symbol}).

List up to 20 distinct posts from {self._hours_label()}.
For each post provide:
- Post URL (x.com or twitter.com)
- @handle
- Short quote or one-line summary

Include posts about earnings, guidance, analyst takes, product or company news, ad/marketing campaigns, brand campaigns, geographic expansion or new state/market launches, institutional ownership, notable sentiment, and credible market discussion.

Exclude only obvious spam bots and generic trading-group promos. Do NOT filter for "high signal" yet.
If posts exist, list them. Do not say "no posts found" without searching."""

    def _curate_stock_posts_prompt(
        self,
        symbol: str,
        company: str,
        candidates: str,
        *,
        permissive: bool,
        rss_context: str = "",
    ) -> str:
        rss_block = self._rss_stock_block(rss_context)
        if permissive:
            return f"""Curate a stock digest for {company} (${symbol}) from these collected X posts:

{candidates}
{rss_block}
Select up to 10 useful items for a personal investor. Include sentiment shifts, technical levels, and community discussion when that is what the posts contain. Also keep real company moves such as ad/marketing campaigns and new state/market launches.

Rules:
- Every item must cite at least one URL from the candidates or RSS articles above in sources (internal only).
- Summaries may be up to one paragraph; state concrete who/what/when; avoid generic filler.
- If 3 or more distinct substantive posts exist, return at least 3 items.
- Skip only pure spam. Never return a "no news" placeholder item."""

        return f"""Curate a stock digest for {company} (${symbol}) from these collected X posts:

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
- Never return a "no news" placeholder item."""

    def _recover_stock_prompt(self, symbol: str, company: str, rss_context: str = "") -> str:
        rss_block = self._rss_stock_block(rss_context)
        return f"""Search X for recent posts about {company} (ticker ${symbol}) from {self._hours_label()}.
{rss_block}
Return up to 10 digest items from any non-spam posts, including ad/marketing campaigns, new state/market launches, sentiment, and technical discussion if needed.

Every source must be a real http(s) URL (X post and/or RSS article) in the sources field (internal only). Summaries may be up to one paragraph; concrete who/what/when; no filler. If posts or RSS articles exist, return at least one item. No placeholder "no news" entries."""

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
        rss_context: str = "",
    ) -> Tuple[StockDigestResult, int, int]:
        result, input_tokens, output_tokens = self._call_structured(
            self._curate_stock_posts_prompt(
                symbol,
                company,
                candidates,
                permissive=permissive,
                rss_context=rss_context,
            ),
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
        rss_context: str = "",
    ) -> Tuple[StockDigestResult, int, int]:
        result, input_tokens, output_tokens = self._call_structured(
            self._recover_stock_prompt(symbol, company, rss_context=rss_context),
            StockDigestResult,
            f"stock_{symbol}_recover",
            tools=self._x_search_tools(),
        )
        result.ticker = symbol
        result.items = self._filter_stock_items(result.items)
        return result, input_tokens, output_tokens

    def generate_main_digest(
        self,
        rss_context: str,
        topics: list[str],
    ) -> Tuple[DigestResult, int, int]:
        topic_list = ", ".join(topics) if topics else "technology and business"
        from_date, to_date = self._date_window()
        hours_label = self._hours_label()
        topic_mix = ""
        if topics:
            topic_mix = (
                f"Enabled topics: {topic_list}. Include at least one item per topic when "
                "credible signal exists. Do not let a single topic occupy more than half "
                "the edition.\n"
            )
        prompt = f"""You are a personal news curator for an e-ink reader. Produce 6-12 highest-signal items (prefer 8-10). Never pad with weak items.

Use x_search for posts from {hours_label} about: {topic_list}.
Use web_search to ground X-discovered stories in reporting when an RSS article below does not already cover them.
{topic_mix}
RSS articles below were already downloaded locally with full or partial article text. Use them as the source of truth for RSS-grounded items. Do not use tools to fetch or re-read those article URLs.

{rss_context or "(No RSS items available this run.)"}

For each item:
- RSS-sourced (origin=rss): rewrite a 3-6 paragraph digest FROM the provided article text. Do not paste the source. Do not invent facts that are not in the text. Put the article URL in sources.
- Mixed (origin=mixed): RSS (or web_search) supplies the facts; X supplies discovery. Cite both URLs.
- X-only (origin=x): only when no RSS/web reporting grounds the story. Quote the posts. Do not invent context, numbers, or motives beyond the posts. Keep it shorter than RSS items.

Selection criteria:
- Prioritize actionable business, technology, and market-moving news
- Prefer primary sources and credible reporting
- Avoid duplicate stories covering the same event
- Populate each item's sources field with the RSS and/or X URLs you used (internal tracking only; not shown to the reader)

Return JSON matching the schema with 6-12 items when signal exists; fewer if it does not."""

        result, in_tok, out_tok = self._call_structured(
            prompt,
            DigestResult,
            "main_digest",
            tools=[
                {"type": "x_search", "from_date": from_date, "to_date": to_date},
                {"type": "web_search"},
            ],
            max_output_tokens=self.settings.max_main_output_tokens,
        )
        logger.info(
            "Main digest selected %d items: %s",
            len(result.items),
            "; ".join(item.title for item in result.items),
        )
        return result, in_tok, out_tok

    def generate_stock_digest(
        self,
        ticker: str,
        company_name: Optional[str] = None,
        rss_context: str = "",
    ) -> Tuple[StockDigestResult, int, int]:
        symbol = ticker.upper().lstrip("$")
        label = (company_name or "").strip()
        company = label if label else symbol
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
        curate_kwargs: dict[str, Any] = {}
        if (rss_context or "").strip():
            curate_kwargs["rss_context"] = rss_context

        if self._has_x_post_candidates(candidates) and calls < _MAX_STOCK_API_CALLS:
            result, in_tok, out_tok = self._curate_stock_posts(
                symbol,
                company,
                candidates,
                **curate_kwargs,
            )
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
                    **curate_kwargs,
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
            recover_kwargs: dict[str, Any] = {}
            if (rss_context or "").strip():
                recover_kwargs["rss_context"] = rss_context
            result, in_tok, out_tok = self._recover_stock_digest(symbol, company, **recover_kwargs)
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
