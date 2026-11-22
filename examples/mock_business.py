"""Run a local demonstration service for the two configured workflows."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


class Purchase(BaseModel):
    amount: float = Field(gt=0)


class Ticket(BaseModel):
    title: str = Field(min_length=1, max_length=100)


def create_app(database: Path):
    app = FastAPI(title="deep-harness demonstration connectors")
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS submissions "
            "(effect_id TEXT PRIMARY KEY, request TEXT NOT NULL, reference TEXT NOT NULL)"
        )

    def commit(kind, effect_id, data):
        request = json.dumps({"kind": kind, "data": data}, sort_keys=True)
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request,reference FROM submissions WHERE effect_id=?", (effect_id,)
            ).fetchone()
            if row is not None:
                if row[0] != request:
                    raise HTTPException(409, "Conflicting idempotency key")
                return {"reference": row[1]}
            reference = f"{kind}-{uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO submissions (effect_id,request,reference) VALUES (?,?,?)",
                (effect_id, request, reference),
            )
            return {"reference": reference}

    @app.post("/purchases")
    def purchase(body: Purchase, idempotency_key: Annotated[str, Header(min_length=1)]):
        return commit("PO", idempotency_key, body.model_dump())

    @app.post("/tickets")
    def ticket(body: Ticket, idempotency_key: Annotated[str, Header(min_length=1)]):
        return commit("TICKET", idempotency_key, body.model_dump())

    @app.get("/status")
    def status(effect_id: str):
        with closing(sqlite3.connect(database)) as conn:
            row = conn.execute(
                "SELECT reference FROM submissions WHERE effect_id=?", (effect_id,)
            ).fetchone()
        return {"applied": row is not None, "reference": row[0] if row else None}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    uvicorn.run(create_app(args.database), host="127.0.0.1", port=9393)
