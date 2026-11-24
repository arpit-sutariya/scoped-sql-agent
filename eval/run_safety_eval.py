"""Safety eval: try to break the permission layer. No AI needed, fully deterministic.

Run:  python -m eval.run_safety_eval

Two kinds of test, for every user:
  1. Attacks that must be REFUSED: writes, DDL, ATTACH, PRAGMA, reading the real tables directly,
     hidden columns, schema snooping, runaway queries.
  2. Queries that are ALLOWED to run but try to pull data out anyway (OR 1=1, subqueries, UNIONs, CTEs,
     joins). Every cell they return is scanned for values the user must never see: IDs and names of
     customers outside their regions, and values from their hidden columns.
Writes eval/results/safety.md and .json.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from data_agent.access import ROOT, TABLES, WAREHOUSE, Session, load_user

USERS = ["maria", "dev", "priya", "sam"]
RESULTS = ROOT / "eval" / "results"

MUST_REFUSE = [
    ("write", "DELETE FROM orders"),
    ("write", "UPDATE orders SET total_usd = 0"),
    ("write", "INSERT INTO orders (order_id) VALUES ('X')"),
    ("write", "DROP TABLE orders"),
    ("write", "DROP VIEW orders"),
    ("write", "CREATE TABLE stolen AS SELECT * FROM customers"),
    ("write", "CREATE TEMP VIEW orders2 AS SELECT * FROM wh.orders"),
    ("write", "ALTER TABLE orders ADD COLUMN x"),
    ("write", "REPLACE INTO orders (order_id) VALUES ('X')"),
    ("attach", "ATTACH DATABASE 'warehouse/warehouse.db' AS w2"),
    ("attach", "ATTACH DATABASE ':memory:' AS scratch"),
    ("pragma", "PRAGMA wh.table_info(orders)"),
    ("pragma", "PRAGMA writable_schema = ON"),
    ("direct", "SELECT * FROM wh.orders"),
    ("direct", "SELECT COUNT(*) FROM wh.customers WHERE region = 'East'"),
    ("direct", "SELECT * FROM orders o JOIN wh.customers c ON c.customer_id = o.customer_id"),
    ("direct", "SELECT (SELECT COUNT(*) FROM wh.orders)"),
    ("direct", "WITH x AS (SELECT * FROM wh.tickets) SELECT COUNT(*) FROM x"),
    ("schema", "SELECT sql FROM sqlite_temp_master"),
    ("schema", "SELECT * FROM wh.sqlite_master"),
    ("multi", "SELECT 1; DELETE FROM orders"),
    ("runaway", "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r"),
    ("extension", "SELECT load_extension('evil')"),
]

# Hidden-column attacks only apply where the role hides that column
HIDDEN_ATTACKS = [
    ("customers", "contact_email", "SELECT contact_email FROM customers"),
    ("customers", "contact_email", "SELECT c.contact_email FROM customers c JOIN orders o USING (customer_id)"),
    ("customers", "annual_revenue_usd", "SELECT SUM(annual_revenue_usd) FROM customers"),
    ("orders", "total_usd", "SELECT SUM(total_usd) FROM orders"),
    ("order_lines", "unit_price", "SELECT AVG(unit_price) FROM order_lines"),
    ("order_lines", "line_total", "SELECT SUM(line_total) FROM order_lines"),
]

# Allowed to run; must not return anything outside the user's scope
EXFILTRATION = [
    "SELECT * FROM orders WHERE region = 'East' OR 1 = 1",
    "SELECT * FROM customers WHERE region != 'West'",
    "SELECT * FROM (SELECT * FROM orders) WHERE region IN ('West','East','Central','South')",
    "SELECT customer_id, name, region FROM customers UNION ALL SELECT account_id, name, region FROM prospects",
    "WITH all_orders AS (SELECT * FROM orders) SELECT customer_id, COUNT(*) FROM all_orders GROUP BY 1",
    "SELECT t.ticket_id, t.customer_id, c.name FROM tickets t LEFT JOIN customers c USING (customer_id)",
    "SELECT * FROM order_lines WHERE order_id NOT IN (SELECT order_id FROM orders)",
    "SELECT * FROM data_quality",
    "SELECT * FROM tickets WHERE customer_id IS NULL OR region IS NULL",
    "SELECT * FROM customers",
    "SELECT * FROM orders",
    "SELECT * FROM tickets",
    "SELECT * FROM prospects",
    "SELECT * FROM order_lines",
]


def forbidden_values(user) -> set:
    """Everything this user must never see, read from the raw warehouse with no restrictions."""
    raw = sqlite3.connect(f"file:{WAREHOUSE}?mode=ro", uri=True)
    bad: set = set()
    if user.regions is not None:
        regions = tuple(user.regions)
        ph = ",".join("?" * len(regions))
        for cid, name in raw.execute(f"SELECT customer_id, name FROM customers WHERE region NOT IN ({ph})", regions):
            bad.update([cid, name])
        bad.update(r[0] for r in raw.execute(f"SELECT order_id FROM orders WHERE region NOT IN ({ph})", regions))
        bad.update(r[0] for r in raw.execute(f"SELECT account_id FROM prospects WHERE region NOT IN ({ph})", regions))
        bad.update(f"ticket:{r[0]}" for r in raw.execute(  # ticket ids are distinctive integers (50001+)
            f"SELECT ticket_id FROM tickets WHERE region IS NULL OR region NOT IN ({ph})", regions))
    for table, cols in user.hidden_columns.items():
        for col in cols:
            # Only text values (emails...) are scanned for. A hidden number can't be told apart from the same
            # number appearing legitimately (a price of 104.0 vs row id 104), so hidden numeric columns are
            # covered by the refusal tests instead: any query that reads them must be refused.
            for (v,) in raw.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"):
                if isinstance(v, str):
                    bad.add(v)
    return bad


def run() -> dict:
    summary = {"refusals": Counter(), "refusal_failures": [], "exfil_queries": 0, "cells_scanned": 0,
               "leaks": [], "by_user": {}}
    for uid in USERS:
        user = load_user(uid)
        s = Session(user, log_file=None)
        bad = forbidden_values(user)
        u = {"refused": 0, "attempts": 0, "cells": 0, "leaks": 0}
        attacks = list(MUST_REFUSE) + [("hidden_column", sql) for t, c, sql in HIDDEN_ATTACKS
                                       if c in user.hidden_columns.get(t, [])]
        for kind, sql in attacks:
            r = s.run(sql)
            u["attempts"] += 1
            if r.ok:
                summary["refusal_failures"].append({"user": uid, "kind": kind, "sql": sql})
            else:
                u["refused"] += 1
                summary["refusals"][r.error_code] += 1
        for sql in EXFILTRATION:
            r = s.run(sql)
            summary["exfil_queries"] += 1
            if not r.ok:
                continue
            for row in r.rows:
                for cell in row:
                    u["cells"] += 1
                    key = f"ticket:{cell}" if isinstance(cell, int) and cell >= 50001 else cell
                    if isinstance(key, str) and key in bad:
                        u["leaks"] += 1
                        if len(summary["leaks"]) < 20:
                            summary["leaks"].append({"user": uid, "sql": sql, "value": str(cell)[:60]})
        # also: everything a user can count must equal the true scoped count (nothing missing, nothing extra)
        summary["cells_scanned"] += u["cells"]
        summary["by_user"][uid] = {**u, "scope": user.scope_text(), "forbidden_values_checked": len(bad)}
    summary["refusals"] = dict(summary["refusals"])
    return summary


def control_check(uid: str = "maria") -> int:
    """Prove the leak scanner works: switch the row filters and column hiding off for one user, run the
    same exfiltration queries, and count what leaks. This number must be large, or 0 leaks means nothing."""
    import data_agent.access as access
    saved = dict(access.ROW_FILTERS)
    try:
        for t in access.ROW_FILTERS:
            access.ROW_FILTERS[t] = None
        user = load_user(uid)
        bad = forbidden_values(user)
        user.hidden_columns = {}
        s = Session(user, log_file=None)
        leaks = 0
        for sql in EXFILTRATION:
            r = s.run(sql)
            for row in (r.rows if r.ok else []):
                for cell in row:
                    key = f"ticket:{cell}" if isinstance(cell, int) and cell >= 50001 else cell
                    leaks += isinstance(key, str) and key in bad
        return leaks
    finally:
        access.ROW_FILTERS.update(saved)


def to_markdown(s: dict) -> str:
    total_attempts = sum(u["attempts"] for u in s["by_user"].values())
    total_refused = sum(u["refused"] for u in s["by_user"].values())
    out = ["# Safety eval", "",
           "| Metric | Result |", "| --- | --- |",
           f"| Attacks refused | **{total_refused}/{total_attempts}** |",
           f"| Exfiltration queries run (allowed SQL trying to pull out-of-scope data) | {s['exfil_queries']} |",
           f"| Result cells scanned for forbidden values | {s['cells_scanned']:,} |",
           f"| **Leaked values** | **{sum(u['leaks'] for u in s['by_user'].values())}** |",
           f"| Scanner self-check: same queries with the permission layer switched off (maria) | "
           f"{s['control_leaks']:,} leaks detected |", "",
           "## By user", "", "| User | Scope | Attacks refused | Cells scanned | Forbidden values checked | Leaks |",
           "| --- | --- | --- | --- | --- | --- |"]
    for uid, u in s["by_user"].items():
        out.append(f"| {uid} | {u['scope']} | {u['refused']}/{u['attempts']} | {u['cells']:,} | "
                   f"{u['forbidden_values_checked']:,} | {u['leaks']} |")
    out += ["", "## How refusals were reported", "", "| Error code | Count |", "| --- | --- |"]
    out += [f"| {k} | {v} |" for k, v in sorted(s["refusals"].items())]
    if s["refusal_failures"]:
        out += ["", "## Attacks that were NOT refused", ""] + [f"- {f['user']}: `{f['sql']}`" for f in s["refusal_failures"]]
    if s["leaks"]:
        out += ["", "## Leaks", ""] + [f"- {l['user']}: `{l['sql']}` returned {l['value']!r}" for l in s["leaks"]]
    return "\n".join(out) + "\n"


def main():
    s = run()
    s["control_leaks"] = control_check()
    md = to_markdown(s)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "safety.md").write_text(md)
    (RESULTS / "safety.json").write_text(json.dumps(s, indent=1, default=str))
    users = s["by_user"]
    print(f"Attacks refused         {sum(u['refused'] for u in users.values())}/"
          f"{sum(u['attempts'] for u in users.values())}  ({len(users)} users)")
    print(f"Exfiltration attempts   {s['exfil_queries']} queries, {s['cells_scanned']:,} result cells scanned")
    print(f"Leaked values           {sum(u['leaks'] for u in users.values())}")
    print(f"Scanner self-check      {s['control_leaks']:,} leaks caught with the permission layer switched off")
    print("Full report: eval/results/safety.md")


if __name__ == "__main__":
    main()
