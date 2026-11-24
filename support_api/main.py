"""Mock support-ticket API (stands in for Zendesk/Freshdesk-style systems).

Run:   uvicorn support_api.main:app --port 8002
Docs:  http://localhost:8002/docs

Paginated, needs a bearer token, and can be made flaky (SUPPORT_API_FAIL_RATE=0.2 -> 20% of
requests return 503), so the pipeline has to handle what real APIs do.
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query

DATA = Path(__file__).resolve().parents[1] / "sources" / "support_tickets.json"
TOKEN = os.getenv("SUPPORT_API_TOKEN", "demo-token")


def create_app(fail_rate: float | None = None) -> FastAPI:
    app = FastAPI(title="Mock Support API", version="1.0")
    tickets = json.loads(DATA.read_text())
    rate = float(os.getenv("SUPPORT_API_FAIL_RATE", "0")) if fail_rate is None else fail_rate

    @app.get("/v1/tickets")
    def list_tickets(page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=200),
                     authorization: str | None = Header(None)):
        if authorization != f"Bearer {TOKEN}":
            raise HTTPException(401, "Missing or invalid token")
        if rate and random.random() < rate:
            raise HTTPException(503, "Service temporarily unavailable")
        total_pages = max(1, -(-len(tickets) // per_page))
        start = (page - 1) * per_page
        return {"data": tickets[start:start + per_page], "page": page, "per_page": per_page,
                "total_pages": total_pages, "total": len(tickets)}

    return app


app = create_app()
