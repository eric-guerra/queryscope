"""Analyze PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output and explain what's slow and why.

Findings are the things a DBA looks for first:
  * where the time goes (exclusive time per node, not the cumulative number Postgres prints)
  * row misestimates (planner expected 10 rows, got 2M -> wrong join strategy)
  * sequential scans that throw most rows away (missing index), with a suggested index
  * sorts / hashes that spilled to disk (work_mem)
  * nested loops with many outer rows
  * heavy shared-buffer reads (cold cache / bloat)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class Node:
    id: int
    type: str
    relation: str | None
    total_ms: float  # inclusive, all loops
    self_ms: float  # exclusive
    est_rows: float
    actual_rows: float  # per loop × loops
    loops: int
    raw: dict
    children: list["Node"] = field(default_factory=list)
    depth: int = 0


@dataclass
class Finding:
    severity: str  # high | medium | low
    kind: str
    node_id: int
    message: str
    suggestion: str | None = None


@dataclass
class Report:
    total_ms: float
    planning_ms: float
    nodes: list[Node]
    findings: list[Finding]

    def hotspots(self, n: int = 3) -> list[Node]:
        return sorted(self.nodes, key=lambda x: -x.self_ms)[:n]


def parse(plan_json: str | list | dict) -> tuple[Node, float, float]:
    data = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    if isinstance(data, list):
        data = data[0]
    if "Plan" not in data:
        raise ValueError("not EXPLAIN FORMAT JSON output")
    if "Actual Total Time" not in data["Plan"]:
        raise ValueError("plan has no timing: run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)")
    counter = iter(range(1, 100_000))

    def build(p: dict, depth: int) -> Node:
        loops = int(p.get("Actual Loops", 1)) or 1
        total = float(p["Actual Total Time"]) * loops
        kids = [build(c, depth + 1) for c in p.get("Plans", [])]
        # Exclusive time: this node's time minus its children's. CTE scans / InitPlans can make it negative.
        child_time = sum(k.total_ms for k in kids if k.raw.get("Parent Relationship") not in ("InitPlan", "SubPlan"))
        n = Node(next(counter), p["Node Type"], p.get("Relation Name"), total, max(0.0, total - child_time),
                 float(p.get("Plan Rows", 0)) * loops, float(p.get("Actual Rows", 0)) * loops, loops, p, kids, depth)
        return n

    root = build(data["Plan"], 0)
    return root, float(data.get("Execution Time", root.total_ms)), float(data.get("Planning Time", 0))


def _flatten(n: Node) -> list[Node]:
    out = [n]
    for c in n.children:
        out += _flatten(c)
    return out


_COL = re.compile(r"\(?\(?([a-z_][a-z0-9_]*)\)?\s*(=|<|>|<=|>=|~~|IS)\s", re.I)


def _filter_columns(f: str) -> list[str]:
    cols = []
    for m in _COL.finditer(f):
        c = m.group(1)
        if c.lower() not in ("and", "or", "not") and c not in cols:
            cols.append(c)
    return cols


def suggest_index(node: Node) -> str | None:
    f = node.raw.get("Filter")
    if not f or not node.relation:
        return None
    cols = _filter_columns(f)
    if not cols:
        return None
    # Equality columns first, then range: that's the order a btree can use.
    eq = [c for c in cols if re.search(rf"\b{c}\)?\s*=\s", f)]
    rest = [c for c in cols if c not in eq]
    ordered = eq + rest
    return f"CREATE INDEX CONCURRENTLY ON {node.relation} ({', '.join(ordered[:3])});"


def analyze(plan_json, *, seq_scan_min_rows: int = 10_000, misestimate_factor: float = 10.0) -> Report:
    root, total, planning = parse(plan_json)
    nodes = _flatten(root)
    findings: list[Finding] = []

    for n in nodes:
        share = n.self_ms / total if total else 0
        r = n.raw

        if n.type == "Seq Scan":
            removed = float(r.get("Rows Removed by Filter", 0)) * n.loops
            scanned = n.actual_rows + removed
            if scanned >= seq_scan_min_rows and removed / max(1, scanned) > 0.9:
                findings.append(Finding(
                    "high" if share > 0.2 else "medium", "seq_scan_filter", n.id,
                    f"Seq Scan on {n.relation} read {int(scanned):,} rows to keep {int(n.actual_rows):,} "
                    f"({removed / scanned:.2%} discarded by filter {r.get('Filter')}).",
                    suggest_index(n)))

        est, act = max(n.est_rows, 1), max(n.actual_rows, 1)
        ratio = max(est / act, act / est)
        if ratio >= misestimate_factor and max(est, act) >= 1000:
            direction = "under" if act > est else "over"
            findings.append(Finding(
                "high" if ratio >= 100 else "medium", "misestimate", n.id,
                f"{n.type}{' on ' + n.relation if n.relation else ''}: planner {direction}estimated rows "
                f"by {ratio:,.0f}x (expected {int(est):,}, got {int(act):,}).",
                f"ANALYZE {n.relation};" if n.relation else "Check statistics on the joined tables (ANALYZE, or CREATE STATISTICS for correlated columns)."))

        if r.get("Sort Space Type") == "Disk" or r.get("Sort Method", "").startswith("external"):
            findings.append(Finding("medium", "sort_spill", n.id,
                                    f"Sort spilled to disk ({r.get('Sort Space Used', '?')} kB, {r.get('Sort Method')}).",
                                    f"Raise work_mem for this query (SET LOCAL work_mem = '{max(64, int(r.get('Sort Space Used', 0)) // 1024 * 2)}MB') or add an index matching the ORDER BY."))
        if n.type == "Hash" and int(r.get("Hash Batches", 1)) > 1:
            findings.append(Finding("medium", "hash_spill", n.id,
                                    f"Hash used {r['Hash Batches']} batches (spilled; peak {r.get('Peak Memory Usage', '?')} kB).",
                                    "Raise work_mem or reduce the build side (filter earlier / join order)."))

        if n.type == "Nested Loop" and n.children:
            outer = n.children[0]
            inner = n.children[1] if len(n.children) > 1 else None
            if outer.actual_rows >= 10_000 and inner is not None and share + (inner.total_ms / total if total else 0) > 0.3:
                findings.append(Finding("high", "nested_loop", n.id,
                                        f"Nested Loop ran its inner side {int(outer.actual_rows):,} times ({inner.type}{' on ' + inner.relation if inner.relation else ''}).",
                                        "Usually caused by a row underestimate on the outer side; fix statistics, or an index on the inner join key."))

        reads = int(r.get("Shared Read Blocks", 0))
        if reads * 8 / 1024 >= 100 and share > 0.1:  # ≥100 MB from disk
            findings.append(Finding("low", "buffer_reads", n.id,
                                    f"{n.type} read {reads * 8 / 1024:,.0f} MB from disk (not cache).",
                                    "Check table bloat (pg_stat_user_tables.n_dead_tup) and whether the working set fits shared_buffers."))

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f.severity], -next(x.self_ms for x in nodes if x.id == f.node_id)))
    return Report(total, planning, nodes, findings)


def to_dict(rep: Report) -> dict:
    return {
        "execution_ms": round(rep.total_ms, 3),
        "planning_ms": round(rep.planning_ms, 3),
        "hotspots": [{"node_id": n.id, "type": n.type, "relation": n.relation, "self_ms": round(n.self_ms, 3),
                      "share": round(n.self_ms / rep.total_ms, 4) if rep.total_ms else 0} for n in rep.hotspots()],
        "nodes": [{"id": n.id, "type": n.type, "relation": n.relation, "depth": n.depth, "self_ms": round(n.self_ms, 3),
                   "total_ms": round(n.total_ms, 3), "est_rows": n.est_rows, "actual_rows": n.actual_rows, "loops": n.loops}
                  for n in rep.nodes],
        "findings": [f.__dict__ for f in rep.findings],
    }
