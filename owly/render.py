"""Deterministic Markdown renderer for e-ink editions."""



from __future__ import annotations



import re

from datetime import datetime

from typing import Optional



from owly.models import DigestItem, DigestResult, StockDigestResult





def _clean_text(text: str) -> str:

    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    return text.strip()





def _render_item(index: int, item: DigestItem, *, heading_level: int = 2) -> str:

    title = _clean_text(item.title)

    summary = _clean_text(item.summary)

    hashes = "#" * heading_level

    body = [

        f"{hashes} {index}. {title}",

        "",

        summary,

        "",

    ]

    return "\n".join(body)





def edition_filename(date: datetime, slot: str, suffix: Optional[str] = None) -> str:

    date_str = date.strftime("%Y-%m-%d")

    if suffix:

        return f"edition_{date_str}_{slot}_{suffix}.md"

    return f"edition_{date_str}_{slot}.md"





def render_main_edition(

    result: DigestResult,

    edition_slot: str,

    generated_at: Optional[datetime] = None,

    stock_links: Optional[list[tuple[str, str]]] = None,

) -> str:

    now = generated_at or datetime.now()

    date_label = now.strftime("%Y-%m-%d")

    slot_label = edition_slot.capitalize()

    lines = [

        f"# Owly Digest — {date_label} ({slot_label})",

        "",

        f"Generated at {now.strftime('%H:%M')}. Ten highest-signal items from RSS and X.",

        "",

    ]



    for idx, item in enumerate(result.items, start=1):

        lines.append(_render_item(idx, item))



    if stock_links:

        lines.extend(["", "## Stocks", ""])

        for label, filename in stock_links:

            lines.append(f"- [{label}]({filename})")



    lines.append("")

    return "\n".join(lines)





def render_combined_stocks_edition(

    results: list[StockDigestResult],

    edition_slot: str,

    generated_at: Optional[datetime] = None,

) -> str:

    now = generated_at or datetime.now()

    date_label = now.strftime("%Y-%m-%d")

    slot_label = edition_slot.capitalize()

    lines = [

        f"# Stocks — {date_label} ({slot_label})",

        "",

        f"Stock-focused edition generated at {now.strftime('%H:%M')}.",

        "",

    ]



    any_items = False

    for result in results:

        ticker = result.ticker.upper()

        lines.extend([f"## {ticker}", ""])

        if not result.items:

            lines.extend([

                "No stock-specific items were found for this run.",

                "",

            ])

            continue

        any_items = True

        for idx, item in enumerate(result.items, start=1):

            lines.append(_render_item(idx, item, heading_level=3))



    if not any_items and not results:

        lines.extend([

            "No enabled stock sources produced items this run.",

            "",

        ])



    lines.append("")

    return "\n".join(lines)


