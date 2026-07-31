"""Pydantic models for structured Grok output."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class DigestItem(BaseModel):
    title: str = Field(description="Short headline for the item")
    summary: str = Field(
        description="2-3 sentences with concrete who/what/when; no filler or boilerplate"
    )
    sources: List[str] = Field(
        description="Source URLs for internal tracking (RSS article or X post); not shown to reader"
    )


class DigestResult(BaseModel):
    items: List[DigestItem] = Field(
        description="Exactly 10 highest-signal news items",
        min_length=1,
        max_length=10,
    )


class StockItem(BaseModel):
    title: str = Field(description="Short headline for the item")
    summary: str = Field(
        description="2-3 sentences with concrete who/what/when; no filler or boilerplate"
    )
    sources: List[str] = Field(
        description="X post URLs from collected candidates; for internal tracking only"
    )


class StockDigestResult(BaseModel):
    ticker: str
    items: List[StockItem] = Field(
        default_factory=list,
        description="Up to 10 highest-signal stock news items",
        max_length=10,
    )
