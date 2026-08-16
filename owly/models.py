"""Pydantic models for structured Grok output."""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


class DigestItem(BaseModel):
    title: str = Field(description="Short headline for the item")
    summary: str = Field(
        description=(
            "Digest article of 3-6 short paragraphs. Use ONLY facts from the "
            "provided RSS article text and/or cited posts. Rewrite RSS sources; "
            "do not paste them. For X-only items, quote the posts and do not "
            "invent facts beyond them. Include what happened and why it matters. "
            "No filler."
        )
    )
    sources: List[str] = Field(
        description="Source URLs for internal tracking (RSS article or X post); not shown to reader"
    )
    origin: Literal["rss", "x", "mixed"] = Field(
        default="rss",
        description="rss if grounded in a provided article; x if social-only; mixed if both",
    )


class DigestResult(BaseModel):
    items: List[DigestItem] = Field(
        description=(
            "6-12 highest-signal news items. Prefer 8-10. Never pad with weak items; "
            "fewer is better than filler."
        ),
        min_length=1,
        max_length=12,
    )


class StockItem(BaseModel):
    title: str = Field(description="Short headline for the item")
    summary: str = Field(
        description="Up to one paragraph with concrete who/what/when; no filler or boilerplate"
    )
    sources: List[str] = Field(
        description="X post and/or RSS article URLs; for internal tracking only"
    )


class StockDigestResult(BaseModel):
    ticker: str
    items: List[StockItem] = Field(
        default_factory=list,
        description="Up to 10 highest-signal stock news items",
        max_length=10,
    )
