"""P0.1 — metric catalog, lineage extraction, impact analysis, API endpoints."""

import pytest
from fastapi.testclient import TestClient

from cubepy.api.app import create_app
from cubepy.catalog import build_catalog, columns_ref, impact, lineage, table_of
from cubepy.config import settings
from cubepy.schema.loader import load_cube_file
from cubepy.schema.registry import registry
from cubepy.security.context import create_token


def _auth(role: str = "admin") -> dict[str, str]:
    token = create_token({"sub": "u1", "role": role}, secret=settings.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _clean():
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
def cubes(tmp_path):
    f = tmp_path / "cubes.yml"
    f.write_text(
        """
cubes:
  - name: posts
    sql: SELECT * FROM public.posts
    owner: growth-team
    tags: [content, core]
    measures:
      - {name: post_count, type: count, owner: alice, tags: [kpi]}
      - {name: title_len_sum, sql: title_len, type: sum, status: deprecated}
    dimensions:
      - {name: title, sql: title, type: string}
      - {name: created_at, sql: created_at, type: time}
      - {name: calc_flag, sql: "is_published AND featured", type: boolean}
    joins:
      users:
        relationship: belongsTo
        sql: "posts.author_id = users.id"
  - name: users
    sql: SELECT * FROM users
    dimensions:
      - {name: id, sql: id, type: number, primaryKey: true}
      - {name: name, sql: name, type: string}
  - name: weird
    sql: SELECT 1
    measures:
      - {name: one, type: count}
""",
        encoding="utf-8",
    )
    load_cube_file(f)
    return registry


def test_table_of():
    assert table_of("SELECT * FROM posts") == "posts"
    assert table_of("SELECT * FROM public.post") == "public.post"
    assert table_of("posts") == "posts"
    assert table_of("") is None
    # No FROM clause and not a bare identifier -> no source table.
    assert table_of("SELECT 1") is None
    assert table_of("orders o") is None


def test_columns_ref_filters_keywords_and_functions():
    assert columns_ref("amount") == ["amount"]
    assert columns_ref("amount * 1.1") == ["amount"]
    assert columns_ref("COALESCE(x, 0) + y") == ["x", "y"]
    assert columns_ref("is_published AND featured") == ["is_published", "featured"]
    assert columns_ref(None) == []
    assert columns_ref("{a} / {b}") == []  # sibling refs, not columns


def test_catalog_structure_and_governance(cubes):
    cat = build_catalog()
    posts = next(c for c in cat["cubes"] if c["name"] == "posts")
    assert posts["table"] == "public.posts"
    assert posts["owner"] == "growth-team"
    assert posts["tags"] == ["content", "core"]
    assert posts["lineage"]["table"] == "public.posts"
    assert "title" in posts["lineage"]["columns"]

    m = next(m for m in posts["members"] if m["name"] == "post_count")
    assert m["owner"] == "alice" and m["tags"] == ["kpi"] and m["status"] == "active"
    dep = next(m for m in posts["members"] if m["name"] == "title_len_sum")
    assert dep["status"] == "deprecated"

    calc = next(m for m in posts["members"] if m["name"] == "calc_flag")
    assert calc["columns"] == ["is_published", "featured"]


def test_build_catalog_accepts_registry_instance(cubes):
    cat = build_catalog(registry)  # a SchemaRegistry, not the default singleton
    assert any(c["name"] == "posts" for c in cat["cubes"])
    assert any(c["name"] == "weird" for c in cat["cubes"])


def test_lineage_edges(cubes):
    g = lineage()
    edges = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("posts.title", "public.posts.title") in edges
    assert ("posts.post_count", "public.posts") in edges  # table-level (no sql)


def test_impact_table_and_column(cubes):
    # whole table: every member on posts
    r = impact("posts")  # exact + suffix match against public.posts
    paths = {h["member"] for h in r["impacted"] if h["kind"] != "join"}
    assert "posts.post_count" in paths and "posts.title" in paths
    assert all(h["cube"] == "posts" for h in r["impacted"])

    # column-level: only members referencing that column + joins on it
    r2 = impact("posts", "author_id")
    assert r2["impacted"] == [
        {"cube": "posts", "join": "users", "kind": "join",
         "reason": "join ON references author_id", "status": "active"}
    ]

    # a cube with no derivable source table never matches an impact query
    assert impact("weird")["impacted"] == []


def test_api_catalog_and_lineage(cubes):
    app = create_app()
    client = TestClient(app)
    r = client.get("/cubepy/v1/catalog", headers=_auth())
    assert r.status_code == 200
    posts = next(c for c in r.json()["cubes"] if c["name"] == "posts")
    assert posts["owner"] == "growth-team"

    r2 = client.get("/cubepy/v1/lineage", headers=_auth())
    assert r2.status_code == 200
    assert any("edges" in r2.json() for _ in [0])

    r3 = client.get("/cubepy/v1/lineage", params={"table": "posts", "column": "title"}, headers=_auth())
    assert r3.status_code == 200
    assert {h.get("member") for h in r3.json()["impacted"]} == {"posts.title"}
