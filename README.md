# QueryScope

Portfolio reference implementation of a 2024 project, built to show a PostgreSQL query-plan analyzer for application teams. You paste `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output, or let it EXPLAIN a SELECT on a read replica, and it tells you where the time goes, why, and what to change.

**Stack:** Python 3.11, FastAPI, psycopg 3, React 18 + TypeScript (Vite).

## What it finds
| Finding | Signal | Suggestion |
|---|---|---|
| `seq_scan_filter` | a Seq Scan reads ≥ 10k rows and the filter discards > 90% of them | `CREATE INDEX CONCURRENTLY ...` with equality columns first, then range columns |
| `misestimate` | estimated vs actual rows (× loops) off by ≥ 10× | `ANALYZE`, or extended statistics for correlated columns |
| `nested_loop` | the outer side has ≥ 10k rows and the loop takes > 30% of runtime | fix the underestimate, or index the inner join key |
| `sort_spill` / `hash_spill` | external merge sort, or a hash with more than 1 batch | a sized `SET LOCAL work_mem`, or an index matching ORDER BY |
| `buffer_reads` | ≥ 100 MB read from disk on a hot node | check bloat (`n_dead_tup`) and whether the working set fits `shared_buffers` |

- **Exclusive (self) time per node.** Postgres prints cumulative per-loop times; this computes total = time × loops minus the children's time, excluding InitPlans and SubPlans. Hotspots rank by self time, which is where the time actually goes.
- **The EXPLAIN endpoint is safe by construction.** `EXPLAIN ANALYZE` *executes* the statement, so only a single read-only `SELECT` is accepted:
  - The checker ignores string literals and comments, and rejects data-modifying CTEs (`WITH d AS (DELETE ...)`) and stacked statements.
  - Accepted statements still run in a `READ ONLY` transaction with a 30-second `statement_timeout`, on a replica, and are rolled back.
- **UI:** a plan tree with indentation, self-time bars and estimated vs actual rows. Findings are colored by severity and highlight their node on hover.

## Run and test
```bash
pip install -r requirements.txt
pytest -q                                     # 15 tests over realistic captured plans
REPLICA_URL=postgres://ro@replica/app uvicorn queryscope.api:app
cd web && npm install && npm run dev
```
