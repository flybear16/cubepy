"""Typed parse of an incoming cube.js-style query object."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Filter(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    member: str | None = None
    operator: str | None = None
    values: list[Any] = []
    or_: list[Filter] | None = Field(default=None, alias="or")
    and_: list[Filter] | None = Field(default=None, alias="and")

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Filter:
        return cls.model_validate(raw)


Filter.model_rebuild()


class TimeDimension(BaseModel):
    dimension: str
    dateRange: list[str] | str | None = None
    granularity: str | None = None


class Query(BaseModel):
    measures: list[str] = []
    dimensions: list[str] = []
    timeDimensions: list[TimeDimension] = []
    filters: list[Filter] = []
    segments: list[str] = []
    order: dict[str, str] | list[list[str]] | None = None
    limit: int | None = None
    offset: int | None = None
    timezone: str | None = "UTC"

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Query:
        return cls.model_validate(raw)

    def order_items(self) -> list[tuple[str, str]]:
        """Normalise ``order`` to a list of (memberPath, 'asc'|'desc')."""
        if self.order is None:
            return []
        if isinstance(self.order, dict):
            return [(k, v) for k, v in self.order.items()]
        return [(item[0], item[1]) for item in self.order]
