"""FastAPI service: paste a plan, or point at a query and let us run EXPLAIN against a read replica."""
from __future__ import annotations

import os
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .analyze import analyze, to_dict


class PlanIn(BaseModel):
    plan: list | dict | str


class QueryIn(BaseModel):
    sql: str


# Only SELECTs: EXPLAIN ANALYZE *executes* the statement, so an UPDATE would really update.
_READ_ONLY = re.compile(r"^\s*(with\b[\s\S]*?\)\s*)?select\b", re.I)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|copy|call|do|vacuum)\b|;\s*\S", re.I)


def is_safe_select(sql: str) -> bool:
    stripped = re.sub(r"'(?:[^']|'')*'", "''", sql)  # ignore keywords inside string literals
    stripped = re.sub(r"--[^\n]*|/\*[\s\S]*?\*/", " ", stripped)
    return bool(_READ_ONLY.match(stripped)) and not _FORBIDDEN.search(stripped.rstrip().rstrip(";"))


def create_app(connect=None) -> FastAPI:
    app = FastAPI(title="QueryScope")

    @app.post("/analyze")
    def analyze_plan(body: PlanIn):
        try:
            return to_dict(analyze(body.plan))
        except (ValueError, KeyError) as e:
            raise HTTPException(422, str(e))

    @app.post("/explain")
    def explain(body: QueryIn):
        if not is_safe_select(body.sql):
            raise HTTPException(400, "only single read-only SELECT statements can be explained")
        if connect is None:
            raise HTTPException(503, "no database configured")
        with connect() as conn, conn.cursor() as cur:
            # Belt and braces: a read-only transaction with a timeout, on a replica.
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + body.sql)
            plan = cur.fetchone()[0]
            conn.rollback()
        return to_dict(analyze(plan))

    return app


def _connect():
    import psycopg
    return psycopg.connect(os.environ["REPLICA_URL"])


app = create_app(_connect if os.environ.get("REPLICA_URL") else None)
