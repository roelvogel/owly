"""Grok/xAI client for X search only. Writing is handled by CursorWriter."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from openai import OpenAI

from owly.config import get_settings
from owly.models import DigestResult, StockDigestResult, StockItem

logger = logging.getLogger(__name__)

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
        self.writer = None

    def _get_writer(self):
        if self.writer is None:
            from owly.writer import CursorWriter

            self.writer = CursorWriter()
        return self.writer

    def _date_window(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=self.settings.ingestion_hours)
        return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")

    def _hours_label(self) -> str:
        hours = self.settings.ingestion_hours
        return f"the last {hours} hours (ignore older posts even if they fall on the same calendar day)"

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
        max_output_tokens: Optional[int] = None,
    ) -> Tuple[str, int, int]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": [{"role": "user", "content": prompt}],
            "max_output_tokens": max_output_tokens or self.settings.xai_search_max_tokens,
        }
        if tools:
            kwargs["tools"] = tools

        response = self.client.responses.create(**kwargs)
        raw = self._extract_output_text(response)
        if not raw:
            raise RuntimeError("Grok returned an empty response")
        in_tok, out_tok = self._usage(response)
        return raw, in_tok, out_tok

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

    def collect_topic_posts_prompt(self, topics: list[str], *, broad: bool = False) -> str:
        topic_list = ", ".join(topics) if topics else "technology and business"
        window = "the latest posts" if broad else f"posts from {self._hours_label()}"
        return f"""Use x_search to find {window} about: {topic_list}.

List up to 25 distinct posts with:
- Post URL (x.com or twitter.com)
- @handle
- Short quote or one-line summary
- Topic tag if obvious

Do NOT write articles. Do NOT search the public web. Do NOT judge quality yet — gather candidates only.
If posts exist, list them. Do not say "no posts found" without searching."""

    def collect_topic_posts(self, topics: list[str], *, broad: bool = False) -> Tuple[str, int, int]:
        tools = [{"type": "x_search"}] if broad else self._x_search_tools()
        return self._call(
            self.collect_topic_posts_prompt(topics, broad=broad),
            tools=tools,
            max_output_tokens=self.settings.xai_search_max_tokens,
        )

    def _collect_stock_posts_prompt(self, symbol: str, company: str, *, broad: bool) -> str:
        if broad:
            return f"""Use x_search to find the latest X posts mentioning ${symbol}, {symbol}, or {company}.

List up to 20 distinct posts with:
- Post URL (x.com or twitter.com)
- @handle
- Short quote or one-line summary

Cast a wide net: include earnings, guidance, analyst notes, product news, ad/marketing campaigns, brand campaigns, geographic expansion or new state/market launches, institutional ownership, sentiment, and technical discussion.

Exclude only obvious spam or promo bots. Do not judge quality yet — gather candidates first.
Do NOT write digest articles."""

        return f"""Use x_search to find recent X posts about {company} (ticker ${symbol}).

List up to 20 distinct posts from {self._hours_label()}.
For each post provide:
- Post URL (x.com or twitter.com)
- @handle
- Short quote or one-line summary

Include posts about earnings, guidance, analyst takes, product or company news, ad/marketing campaigns, brand campaigns, geographic expansion or new state/market launches, institutional ownership, notable sentiment, and credible market discussion.

Exclude only obvious spam bots and generic trading-group promos. Do NOT filter for "high signal" yet.
Do NOT write digest articles.
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
        """Prompt text kept for tests; CursorWriter builds the live curate prompt."""
        rss_block = ""
        if (rss_context or "").strip():
            rss_block = f"\nRSS:\n{rss_context}\n"
        mode = "permissive" if permissive else "standard"
        return (
            f"Curate a stock digest for {company} (${symbol}) [{mode}].\n"
            f"{candidates}\n{rss_block}"
            "ad/marketing campaigns, new state/market launches, do not drop them as fluff"
        )

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
            max_output_tokens=self.settings.xai_search_max_tokens,
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
        result, input_tokens, output_tokens = self._get_writer().write_stock_digest(
            symbol,
            company,
            candidates,
            permissive=permissive,
            rss_context=rss_context,
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
        extra, in_tok, out_tok = self._collect_stock_posts(symbol, company, broad=True)
        total_in, total_out = in_tok, out_tok
        candidates = extra if self._has_x_post_candidates(extra) else extra
        curate_kwargs: dict[str, Any] = {"permissive": True}
        if (rss_context or "").strip():
            curate_kwargs["rss_context"] = rss_context
        result, c_in, c_out = self._curate_stock_posts(symbol, company, candidates, **curate_kwargs)
        return result, total_in + c_in, total_out + c_out

    def generate_main_digest(
        self,
        rss_context: str,
        topics: list[str],
    ) -> Tuple[DigestResult, int, int]:
        x_candidates, in_tok, out_tok = self.collect_topic_posts(topics)
        total_in, total_out = in_tok, out_tok
        url_count = len(_X_URL_PATTERN.findall(x_candidates))
        logger.info("X collect for main digest: %d candidate URLs", url_count)
        hours_label = self._hours_label()
        if not self._has_x_post_candidates(x_candidates):
            extra, in_tok, out_tok = self.collect_topic_posts(topics, broad=True)
            total_in += in_tok
            total_out += out_tok
            extra_count = len(_X_URL_PATTERN.findall(extra))
            logger.info("X collect for main digest (broad): %d candidate URLs", extra_count)
            if extra_count:
                x_candidates = extra
                hours_label = (
                    f"a broader X search (dated window returned no post URLs; "
                    f"prefer posts from {self._hours_label()})"
                )
        result, w_in, w_out = self._get_writer().write_main_digest(
            rss_context,
            topics,
            x_candidates,
            hours_label,
        )
        return result, total_in + w_in, total_out + w_out

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
                "Stock %s Cursor curate (%d items)",
                symbol,
                len(result.items),
            )

            if not result.items:
                result, in_tok, out_tok = self._curate_stock_posts(
                    symbol,
                    company,
                    candidates,
                    permissive=True,
                    **curate_kwargs,
                )
                total_in += in_tok
                total_out += out_tok
                logger.info(
                    "Stock %s Cursor permissive curate (%d items)",
                    symbol,
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
                "Stock %s recover (%d items)",
                symbol,
                len(result.items),
            )

        if not result.items:
            logger.warning("Stock digest for %s is empty after %d xAI collect call(s)", symbol, calls)

        return result, total_in, total_out
