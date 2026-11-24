"""Run from the project folder:  pytest -q"""
from __future__ import annotations

import asyncio
import json
from datetime import timezone
from types import SimpleNamespace

import pytest

from data_agent.access import Session, load_user
from data_agent.agent import Agent
from eval.run_answer_eval import grade
from pipeline import clean


# ------------------------------------------------------------------ cleaning rules
def test_region_aliases():
    assert clean.region("Midwest") == "Central"
    assert clean.region(" WEST COAST ") == "West"
    assert clean.region("Narnia") is None


def test_revenue_parsing():
    assert clean.revenue("$1.2M") == (1_200_000, False)
    assert clean.revenue("1,200,000") == (1_200_000, False)
    assert clean.revenue("approx 4M") == (4_000_000, True)
    assert clean.revenue("") == (None, False)


def test_timestamps_become_utc():
    a = clean.timestamp_utc("2026-03-04T14:22:00-08:00")
    b = clean.timestamp_utc("2026-03-04 22:22:00Z")
    c = clean.timestamp_utc(int(a.timestamp() * 1000))
    assert a == b == c and a.tzinfo == timezone.utc
    assert clean.timestamp_utc("2026-03-04 22:22:00") is None  # no time zone: refuse to guess


def test_name_key_handles_spacing_and_suffixes():
    assert clean.name_key("Blue Water Logistics LLC ") == clean.name_key("BLUEWATER LOGISTICS")


# ------------------------------------------------------------------ the built warehouse
def test_region_conflicts_keep_erp_region():
    s = Session(load_user("priya"), log_file=None)
    rows = s.run("SELECT region, crm_region FROM customers WHERE crm_region IS NOT NULL AND crm_region != region").rows
    issues = s.run("SELECT COUNT(*) FROM data_quality WHERE issue_type = 'REGION_CONFLICT'").rows[0][0]
    assert len(rows) == issues == 5


def test_invalid_ticket_timestamps_get_no_resolution_time():
    s = Session(load_user("priya"), log_file=None)
    assert s.run("SELECT COUNT(*) FROM tickets WHERE resolution_hours < 0").rows[0][0] == 0
    assert s.run("SELECT COUNT(*) FROM data_quality WHERE issue_type = 'INVALID_TIMESTAMPS'").rows[0][0] > 0


# ------------------------------------------------------------------ permission layer
@pytest.mark.parametrize("user,expected", [("maria", {"West"}), ("dev", {"East"}),
                                           ("priya", {"West", "East", "Central", "South"})])
def test_users_only_see_their_regions(user, expected):
    s = Session(load_user(user), log_file=None)
    assert {r[0] for r in s.run("SELECT DISTINCT region FROM orders").rows} == expected
    assert {r[0] for r in s.run("SELECT DISTINCT region FROM customers").rows} == expected


def test_order_lines_follow_order_scope():
    s = Session(load_user("maria"), log_file=None)
    orphans = s.run("SELECT COUNT(*) FROM order_lines WHERE order_id NOT IN (SELECT order_id FROM orders)")
    assert orphans.rows[0][0] == 0


def test_hidden_columns_are_refused_with_a_clear_reason():
    r = Session(load_user("sam"), log_file=None).run("SELECT SUM(total_usd) FROM orders")
    assert r.error_code == "PERMISSION_DENIED" and "support_lead" in r.message


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "DROP TABLE orders", "UPDATE orders SET total_usd = 0",
                                 "ATTACH DATABASE ':memory:' AS x", "PRAGMA query_only = OFF",
                                 "CREATE TABLE t (x)"])
def test_writes_are_blocked(sql):
    r = Session(load_user("priya"), log_file=None).run(sql)
    assert r.error_code == "WRITE_BLOCKED"


def test_no_route_to_the_real_warehouse():
    r = Session(load_user("maria"), log_file=None).run("SELECT COUNT(*) FROM wh.orders")
    assert r.error_code == "PERMISSION_DENIED"


def test_runaway_query_is_stopped():
    r = Session(load_user("maria"), log_file=None).run(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r")
    assert r.error_code == "TIMEOUT"


def test_full_access_count_star_works():  # regression: SQLite's COUNT(*) shortcut used to be refused
    assert Session(load_user("priya"), log_file=None).run("SELECT COUNT(*) FROM prospects").rows == [[8]]


def test_results_are_capped():
    r = Session(load_user("priya"), log_file=None).run("SELECT * FROM order_lines")
    assert r.row_count == 200 and r.truncated


# ------------------------------------------------------------------ agent loop (scripted model, no API)
def _resp(*blocks):
    return SimpleNamespace(content=list(blocks), usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _tool(i, name, args):
    return SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=args)


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = self

    def create(self, **kw):
        return next(self.responses)


def test_agent_runs_tools_and_returns_structured_answer():
    model = Script([_resp(_tool(1, "run_query", {"sql": "DELETE FROM orders"})),
                    _resp(_tool(2, "run_query", {"sql": "SELECT COUNT(*) FROM orders"})),
                    _resp(_tool(3, "final_answer", {"outcome": "answer", "answer": "401 orders", "value": 401,
                                                    "sql": ["SELECT COUNT(*) FROM orders"]}))])
    res = Agent("maria", client=model, log_file=None).ask("How many orders?")
    assert (res.outcome, res.value, res.steps) == ("answer", 401, 3)
    assert [c["error_code"] for c in res.tool_calls] == ["WRITE_BLOCKED", None]
    assert res.input_tokens == 30


def test_agent_handles_plain_text_reply():
    model = Script([_resp(SimpleNamespace(type="text", text="Hello"))])
    assert Agent("maria", client=model, log_file=None).ask("hi").answer == "Hello"


# ------------------------------------------------------------------ the grader must fail wrong answers
def _res(**kw):
    base = dict(outcome="answer", answer="", value=None, caveats=[])
    return SimpleNamespace(**{**base, **kw})


def test_grader_rejects_wrong_values_and_outcomes():
    q = {"expected": ["answer"], "reference_sql": "x", "kind": "number"}
    assert grade(q, _res(value=100.5), 100.0, set())["correct"]
    assert not grade(q, _res(value=120), 100.0, set())["correct"]
    assert not grade(q, _res(outcome="clarify"), 100.0, set())["correct"]


def test_grader_percent_accepts_fraction_or_percent():
    q = {"expected": ["answer"], "reference_sql": "x", "kind": "percent"}
    assert grade(q, _res(value=0.214), 21.4, set())["correct"]
    assert grade(q, _res(value="21.4%"), 21.4, set())["correct"]


def test_grader_flags_leaks():
    q = {"expected": ["refused"]}
    g = grade(q, _res(outcome="refused", answer="East's top customer is Cedar Labs Inc."), None, {"Cedar Labs Inc."})
    assert g["leak"] and not g["correct"]


def test_grader_requires_caveat_when_needed():
    q = {"expected": ["answer"], "reference_sql": "x", "kind": "number", "expect_caveat": True}
    assert not grade(q, _res(value=5), 5, set())["correct"]
    assert grade(q, _res(value=5, caveats=["2 customers have conflicting regions"]), 5, set())["correct"]


# ------------------------------------------------------------------ MCP server, launched like Claude Desktop
def test_mcp_server_over_stdio():
    from tests.mcp_smoke import main
    out = asyncio.run(main("dev"))
    assert out["user"] == "dev" and out["orders_by_region"] == [["East", 712]]
    assert out["delete"] == "WRITE_BLOCKED" and out["instructions_sent"]
    assert len(out["tools"]) == 5
