"""Filter operator -> SQL fragment compilation.

Operators mirror cube.js's public set (``docs/06`` §2). Filter *values* are
user input and are always bound as parameters (never interpolated). The
``bind(value)`` callback registers a value and returns its placeholder name, so
the caller owns param naming.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

BindFn = Callable[[Any], str]
OpFn = Callable[[list[Any], str, BindFn], str]


def _op_equals(values: list[Any], col: str, bind: BindFn) -> str:
    return f"{col} = {bind(values[0])}"


def _op_not_equals(values: list[Any], col: str, bind: BindFn) -> str:
    return f"{col} <> {bind(values[0])}"


def _op_in(values: list[Any], col: str, bind: BindFn) -> str:
    phs = ", ".join(bind(v) for v in values)
    return f"{col} IN ({phs})"


def _op_not_in(values: list[Any], col: str, bind: BindFn) -> str:
    phs = ", ".join(bind(v) for v in values)
    return f"{col} NOT IN ({phs})"


def _op_compare(symbol: str) -> OpFn:
    def _fn(values: list[Any], col: str, bind: BindFn) -> str:
        return f"{col} {symbol} {bind(values[0])}"

    return _fn


def _op_like(prefix: str, suffix: str) -> OpFn:
    def _fn(values: list[Any], col: str, bind: BindFn) -> str:
        return f"{col} LIKE {bind(prefix + str(values[0]) + suffix)}"

    return _fn


def _op_not_like(prefix: str, suffix: str) -> OpFn:
    def _fn(values: list[Any], col: str, bind: BindFn) -> str:
        return f"{col} NOT LIKE {bind(prefix + str(values[0]) + suffix)}"

    return _fn


def _op_set(_values: list[Any], col: str, _bind: BindFn) -> str:
    return f"{col} IS NOT NULL"


def _op_not_set(_values: list[Any], col: str, _bind: BindFn) -> str:
    return f"{col} IS NULL"


def _op_in_date_range(values: list[Any], col: str, bind: BindFn) -> str:
    # values resolved to [start, end] by the query layer (concrete timestamps)
    return f"{col} >= {bind(values[0])} AND {col} <= {bind(values[1])}"


def _op_not_in_date_range(values: list[Any], col: str, bind: BindFn) -> str:
    return f"{col} < {bind(values[0])} OR {col} > {bind(values[1])}"


def _op_before_date(values: list[Any], col: str, bind: BindFn) -> str:
    return f"{col} < {bind(values[0])}"


def _op_after_date(values: list[Any], col: str, bind: BindFn) -> str:
    return f"{col} > {bind(values[0])}"


OPERATORS: dict[str, OpFn] = {
    "equals": _op_equals,
    "notEquals": _op_not_equals,
    "in": _op_in,
    "notIn": _op_not_in,
    "gt": _op_compare(">"),
    "gte": _op_compare(">="),
    "lt": _op_compare("<"),
    "lte": _op_compare("<="),
    "contains": _op_like("%", "%"),
    "notContains": _op_not_like("%", "%"),
    "startsWith": _op_like("", "%"),
    "endsWith": _op_like("%", ""),
    "set": _op_set,
    "notSet": _op_not_set,
    "inDateRange": _op_in_date_range,
    "notInDateRange": _op_not_in_date_range,
    "beforeDate": _op_before_date,
    "afterDate": _op_after_date,
}
