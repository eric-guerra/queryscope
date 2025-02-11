import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from queryscope.analyze import analyze, parse, suggest_index, to_dict
from queryscope.api import create_app, is_safe_select

PLANS = Path(__file__).parent / "plans"


def load(name):
    return json.loads((PLANS / name).read_text())


def kinds(rep):
    return [f.kind for f in rep.findings]


def test_exclusive_time_and_hotspots():
    root, total, planning = parse(load("orders_by_customer.json"))
    assert total == pytest.approx(2341.5)
    rep = analyze(load("orders_by_customer.json"))
    top = rep.hotspots(1)[0]
    assert top.type == "Seq Scan" and top.relation == "orders"
    assert sum(n.self_ms for n in rep.nodes) == pytest.approx(rep.nodes[0].total_ms, rel=1e-6)


def test_seq_scan_with_selective_filter_suggests_index():
    rep = analyze(load("orders_by_customer.json"))
    f = next(f for f in rep.findings if f.kind == "seq_scan_filter")
    assert f.severity == "high"
    assert "99.98%" in f.message
    assert f.suggestion == "CREATE INDEX CONCURRENTLY ON orders (customer_id, created_at);"


def test_misestimate_and_nested_loop_are_flagged():
    rep = analyze(load("nested_loop_misestimate.json"))
    assert "misestimate" in kinds(rep) and "nested_loop" in kinds(rep)
    mis = next(f for f in rep.findings if f.kind == "misestimate")
    assert "underestimated" in mis.message and "ANALYZE order_lines" in (mis.suggestion or "") or "ANALYZE" in (mis.suggestion or "")


def test_spills_are_flagged():
    rep = analyze(load("sort_spill.json"))
    assert {"sort_spill", "hash_spill"} <= set(kinds(rep))
    assert "work_mem" in next(f for f in rep.findings if f.kind == "sort_spill").suggestion


def test_healthy_plan_has_no_findings():
    assert analyze(load("index_scan_ok.json")).findings == []


def test_rejects_plans_without_timing():
    with pytest.raises(ValueError, match="ANALYZE"):
        analyze([{"Plan": {"Node Type": "Seq Scan", "Plan Rows": 10}}])


def test_index_suggestion_orders_equality_before_range():
    from queryscope.analyze import Node
    n = Node(1, "Seq Scan", "events", 1, 1, 1, 1, 1, {"Filter": "((created_at > '2024-01-01'::date) AND (tenant_id = 42) AND (kind = 'click'::text))"})
    assert suggest_index(n) == "CREATE INDEX CONCURRENTLY ON events (tenant_id, kind, created_at);"


@pytest.mark.parametrize("sql,ok", [
    ("SELECT * FROM orders WHERE id = 1", True),
    ("with x as (select 1) select * from x", True),
    ("select 'update me' from t", True),
    ("UPDATE orders SET status = 'x'", False),
    ("select 1; drop table orders", False),
    ("with d as (delete from orders returning *) select * from d", False),
    ("/* select */ delete from t", False),
])
def test_only_read_only_selects_are_explained(sql, ok):
    assert is_safe_select(sql) is ok


def test_api_round_trip():
    client = TestClient(create_app())
    r = client.post("/analyze", json={"plan": load("orders_by_customer.json")})
    assert r.status_code == 200
    body = r.json()
    assert body["hotspots"][0]["relation"] == "orders" and body["findings"]
    assert client.post("/analyze", json={"plan": {"nope": 1}}).status_code == 422
    assert client.post("/explain", json={"sql": "delete from orders"}).status_code == 400
    assert client.post("/explain", json={"sql": "select 1"}).status_code == 503
    assert to_dict(analyze(load("index_scan_ok.json")))["findings"] == []
