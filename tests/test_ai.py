"""AI context builders: catalog prompt + members index."""

import pytest

from cubepy.ai import build_context, members_index, query_contract, system_prompt
from cubepy.schema.loader import load_cube_file
from cubepy.schema.registry import registry


@pytest.fixture(autouse=True)
def _clean():
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
def loaded(tmp_path):
    f = tmp_path / "c.yml"
    f.write_text(
        """
cubes:
  - name: orders
    sql: SELECT * FROM orders
    measures:
      - {name: revenue, sql: amount, type: sum, description: 总营收}
      - {name: old_m, sql: amount, type: sum, status: deprecated}
    dimensions:
      - {name: status, sql: status, type: string}
    joins:
      users: {relationship: belongsTo, sql: "orders.user_id = users.id"}
""",
        encoding="utf-8",
    )
    load_cube_file(f)
    return registry


def test_build_context_includes_members_and_joins(loaded):
    ctx = build_context()
    assert "- measure: orders.revenue [sum] (总营收)" in ctx
    assert "- dimension: orders.status [string]" in ctx
    assert "- relationship: orders belongsTo users" in ctx
    assert "old_m" not in ctx  # deprecated hidden


def test_build_context_with_explicit_sources(loaded):
    # explicit list and an explicit SchemaRegistry must produce the same catalog
    assert build_context(registry.all()) == build_context(registry)


def test_query_contract_returns_constant():
    from cubepy.ai import QUERY_CONTRACT, query_contract

    assert query_contract() == QUERY_CONTRACT


def test_deprecated_dimension_hidden_and_segments_listed(tmp_path):
    f = tmp_path / "c.yml"
    f.write_text(
        """
cubes:
  - name: t
    sql: SELECT * FROM t
    dimensions:
      - {name: old_d, sql: old_d, type: string, status: deprecated}
      - {name: status, sql: status, type: string}
    segments:
      - {name: active, sql: "status = 'active'"}
""",
        encoding="utf-8",
    )
    load_cube_file(f)
    ctx = build_context()
    assert "old_d" not in ctx  # deprecated dimension hidden
    assert "- segment: t.active" in ctx
    assert "t.active" in members_index()


def test_system_prompt_has_contract_and_example(loaded):
    sp = system_prompt()
    assert '"measures"' in sp and "Example" in sp and "orders.revenue" in sp


def test_members_index(loaded):
    idx = members_index()
    assert "orders.revenue" in idx and "orders.status" in idx
    assert "orders.old_m" not in idx


def test_contract_forbids_extra_dimensions() -> None:
    """即兴验证抓到：LLM 自加时间维度致 data 形状错（洞察层险些兜不住）。"""
    assert "Return ONLY the dimensions the question asks for" in query_contract()


def test_contract_documents_measure_filters() -> None:
    """docs/06 §2 measureFilter: the LLM contract teaches measure-path filters
    (HAVING) so "GMV超过100万的渠道" style questions are answerable."""
    assert "MEASURE" in query_contract() and "HAVING" in query_contract()
