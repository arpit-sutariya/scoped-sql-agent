"""Build the warehouse: pull the three sources, reconcile them, log every conflict.

Run:  python -m pipeline.build_warehouse                 # fetches tickets from the mock API in-process
      python -m pipeline.build_warehouse --api-url http://localhost:8002   # from a running API server

Writes warehouse/warehouse.db and docs/data_quality_report.md.

Rules decided with the (fictional) client, each written down so answers can be explained:
  * Region:     ERP wins (it's the billing address). CRM disagreements are kept in crm_region and logged.
  * Owner, industry, revenue, contact: CRM is the only source.
  * Duplicates: CRM rows for the same customer are merged; the most recently modified row wins.
  * Tickets:    matched to a customer by email domain, then by company name. Personal emails with no
                company stay unmatched rather than guessed.
  * Timestamps: everything stored in UTC. A ticket solved before it was opened gets no resolution time.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pipeline import clean

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "sources"
WAREHOUSE = ROOT / "warehouse" / "warehouse.db"
REPORT = ROOT / "docs" / "data_quality_report.md"
AS_OF = "2026-10-31"

SCHEMA = """
CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE reps (rep_id TEXT PRIMARY KEY, name TEXT, email TEXT, region TEXT, active INTEGER);
CREATE TABLE customers (
  customer_id TEXT PRIMARY KEY, name TEXT, region TEXT, crm_region TEXT, industry TEXT, owner_rep_id TEXT,
  annual_revenue_usd REAL, revenue_is_estimate INTEGER, contact_email TEXT, website_domain TEXT,
  city TEXT, customer_since TEXT, crm_account_id TEXT);
CREATE TABLE prospects (account_id TEXT PRIMARY KEY, name TEXT, region TEXT, industry TEXT, owner_rep_id TEXT,
  annual_revenue_usd REAL);
CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT, category TEXT, list_price REAL);
CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT, order_date TEXT, status TEXT, region TEXT,
  total_usd REAL, line_count INTEGER);
CREATE TABLE order_lines (order_id TEXT, line_no INTEGER, sku TEXT, qty INTEGER, unit_price REAL, line_total REAL);
CREATE TABLE tickets (ticket_id INTEGER PRIMARY KEY, customer_id TEXT, region TEXT, subject TEXT, priority TEXT,
  status TEXT, channel TEXT, opened_at TEXT, solved_at TEXT, resolution_hours REAL, csat INTEGER);
CREATE TABLE data_quality (issue_id INTEGER PRIMARY KEY, table_name TEXT, record_id TEXT, customer_id TEXT,
  issue_type TEXT, detail TEXT, resolution TEXT);
CREATE INDEX ix_orders_customer ON orders(customer_id);
CREATE INDEX ix_lines_order ON order_lines(order_id);
CREATE INDEX ix_tickets_customer ON tickets(customer_id);
"""


class Log:
    def __init__(self):
        self.rows = []

    def add(self, table, record_id, customer_id, issue_type, detail, resolution):
        self.rows.append((len(self.rows) + 1, table, str(record_id), customer_id, issue_type, detail, resolution))


# ---------------------------------------------------------------------------- extract
def fetch_tickets(api_url: str | None, token: str = "demo-token", retries: int = 4) -> list[dict]:
    if api_url:
        http = httpx.Client(base_url=api_url, timeout=10)
    else:  # no server running: talk to the same app in-process
        from fastapi.testclient import TestClient
        from support_api.main import create_app
        http = TestClient(create_app())
    headers = {"Authorization": f"Bearer {token}"}
    out, page, total_pages = [], 1, 1
    while page <= total_pages:
        for attempt in range(retries):
            r = http.get("/v1/tickets", params={"page": page, "per_page": 200}, headers=headers)
            if r.status_code < 500:
                r.raise_for_status()
                break
            time.sleep(0.3 * 2 ** attempt)
        else:
            raise RuntimeError(f"Support API failed on page {page} after {retries} attempts")
        body = r.json()
        out.extend(body["data"])
        total_pages = body["total_pages"]
        page += 1
    return out


# ---------------------------------------------------------------------------- reconcile
def build(api_url: str | None = None, warehouse: Path = WAREHOUSE) -> dict:
    log = Log()
    erp = sqlite3.connect(SRC / "erp.db")
    erp.row_factory = sqlite3.Row
    erp_customers = [dict(r) for r in erp.execute("SELECT * FROM customers")]
    crm_rows = list(csv.DictReader((SRC / "crm_accounts.csv").open()))
    rep_rows = list(csv.DictReader((SRC / "crm_reps.csv").open()))
    tickets_raw = fetch_tickets(api_url)

    # reps
    reps = {r["rep_id"]: {"rep_id": r["rep_id"], "name": r["full_name"], "email": r["email"],
                          "region": r["region"], "active": int(r["active"] == "yes")} for r in rep_rows}
    rep_by_name = {r["name"].lower(): rid for rid, r in reps.items()}
    rep_by_email = {r["email"].lower(): rid for rid, r in reps.items()}

    # --- match CRM rows to ERP customers
    by_id = {c["cust_id"]: c for c in erp_customers}
    by_key = {clean.name_key(c["legal_name"]): c["cust_id"] for c in erp_customers}
    keys = list(by_key)
    matched: dict[str, list[dict]] = defaultdict(list)
    prospects, match_method = [], {}
    for row in crm_rows:
        cid = row["erp_customer_id"].strip() or None
        method = "erp_id"
        if cid and cid not in by_id:
            log.add("crm_accounts", row["account_id"], None, "BAD_ERP_ID",
                    f"erp_customer_id {cid} doesn't exist in the ERP", "matched by name instead")
            cid = None
        if not cid:
            key = clean.name_key(row["account_name"])
            if key in by_key:
                cid, method = by_key[key], "name"
            else:
                close = difflib.get_close_matches(key, keys, n=1, cutoff=0.9)
                if close:
                    cid, method = by_key[close[0]], "fuzzy_name"
                    log.add("crm_accounts", row["account_id"], cid, "FUZZY_MATCH",
                            f"'{row['account_name']}' matched to '{by_id[cid]['legal_name']}'", "accepted (similarity >= 0.9)")
        if cid:
            matched[cid].append(row)
            match_method[row["account_id"]] = (cid, method)
        else:
            prospects.append(row)
            match_method[row["account_id"]] = (None, "none")

    def owner_id(raw: str, record_id: str, cid: str | None) -> str | None:
        raw = (raw or "").strip().lower()
        rid = rep_by_email.get(raw) or rep_by_name.get(raw)
        if not rid and raw:
            log.add("crm_accounts", record_id, cid, "UNKNOWN_OWNER", f"owner '{raw}' isn't a known rep", "owner left empty")
        elif rid and not reps[rid]["active"]:
            log.add("crm_accounts", record_id, cid, "INACTIVE_OWNER",
                    f"owner {reps[rid]['name']} is marked inactive", "kept; account needs reassignment")
        return rid

    customers = {}
    for c in erp_customers:
        cid = c["cust_id"]
        region = clean.ERP_REGION_CODES[c["region_code"]]
        rows = matched.get(cid, [])
        rec = {"customer_id": cid, "name": c["legal_name"], "region": region, "crm_region": None,
               "industry": None, "owner_rep_id": None, "annual_revenue_usd": None, "revenue_is_estimate": 0,
               "contact_email": None, "website_domain": None, "city": c["billing_city"],
               "customer_since": c["created_on"], "crm_account_id": None}
        if not rows:
            log.add("customers", cid, cid, "MISSING_IN_CRM", f"{c['legal_name']} has no CRM account",
                    "owner, industry and revenue unknown")
        else:
            if len(rows) > 1:
                rows.sort(key=lambda r: clean.crm_date(r["last_modified"]) or datetime.min.date(), reverse=True)
                log.add("customers", cid, cid, "DUPLICATE_MERGED",
                        f"{len(rows)} CRM rows ({', '.join(r['account_id'] for r in rows)})",
                        f"kept {rows[0]['account_id']} (most recently modified)")
            r = rows[0]
            crm_region = clean.region(r["region"])
            if crm_region is None:
                log.add("customers", cid, cid, "UNKNOWN_REGION", f"CRM region '{r['region']}' not recognised", "ERP region used")
            elif crm_region != region:
                log.add("customers", cid, cid, "REGION_CONFLICT",
                        f"ERP says {region}, CRM says {crm_region}", "ERP region used (billing address)")
            rev, est = clean.revenue(r["annual_revenue"])
            if rev is None:
                log.add("customers", cid, cid, "MISSING_VALUE",
                        f"annual revenue '{r['annual_revenue']}' is blank or unreadable", "left empty")
            rec.update(crm_region=crm_region, industry=r["industry"] or None,
                       owner_rep_id=owner_id(r["owner"], r["account_id"], cid), annual_revenue_usd=rev,
                       revenue_is_estimate=int(est), contact_email=r["primary_contact_email"] or None,
                       website_domain=clean.domain_of(r["website"]), crm_account_id=r["account_id"])
        customers[cid] = rec

    prospect_recs = []
    for r in prospects:
        rev, _ = clean.revenue(r["annual_revenue"])
        prospect_recs.append({"account_id": r["account_id"], "name": r["account_name"].strip(),
                              "region": clean.region(r["region"]), "industry": r["industry"] or None,
                              "owner_rep_id": owner_id(r["owner"], r["account_id"], None), "annual_revenue_usd": rev})
        log.add("prospects", r["account_id"], None, "CRM_ONLY_ACCOUNT",
                f"{r['account_name'].strip()} is in the CRM but has never ordered", "stored as a prospect, not a customer")

    # --- orders
    lines = [dict(r) for r in erp.execute("SELECT * FROM order_lines ORDER BY order_id, line_no")]
    lines_by_order = defaultdict(list)
    for l in lines:
        l["line_total"] = round(l["qty"] * l["unit_price"], 2)
        lines_by_order[l["order_id"]].append(l)
    orders = []
    for o in erp.execute("SELECT * FROM orders"):
        cust = customers[o["cust_id"]]
        if o["ship_region_code"] is None:
            log.add("orders", o["order_id"], o["cust_id"], "MISSING_VALUE", "ship region missing",
                    f"used customer's region ({cust['region']})")
        ls = lines_by_order[o["order_id"]]
        orders.append({"order_id": o["order_id"], "customer_id": o["cust_id"], "order_date": o["order_date"],
                       "status": o["status"], "region": cust["region"],
                       "total_usd": round(sum(l["line_total"] for l in ls), 2), "line_count": len(ls)})

    # --- tickets
    by_domain = {c["website_domain"]: cid for cid, c in customers.items() if c["website_domain"]}
    ticket_recs, ticket_method = [], {}
    for t in tickets_raw:
        cid, method = None, "none"
        dom = clean.domain_of(t["requester_email"])
        if dom and dom not in clean.PERSONAL_DOMAINS and dom in by_domain:
            cid, method = by_domain[dom], "email_domain"
        elif dom and dom not in clean.PERSONAL_DOMAINS and clean.name_key(dom.split(".")[0]) in by_key:
            # customer has no CRM record (so no website), but the domain spells its ERP name
            cid, method = by_key[clean.name_key(dom.split(".")[0])], "domain_matches_name"
            log.add("tickets", t["id"], cid, "DOMAIN_NAME_MATCH",
                    f"{dom} matches ERP name {by_id[cid]['legal_name']!r}; customer has no CRM website",
                    "matched on the domain spelling the company name")
        elif t.get("company"):
            key = clean.name_key(t["company"])
            cid = by_key.get(key)
            if not cid:
                close = difflib.get_close_matches(key, keys, n=1, cutoff=0.9)
                cid = by_key[close[0]] if close else None
            method = "company_name" if cid else "none"
        if not cid:
            log.add("tickets", t["id"], None, "UNMATCHED_TICKET",
                    f"requester {t['requester_email']}, company {t.get('company')!r}", "kept with no customer")
        ticket_method[str(t["id"])] = cid
        opened, solved = clean.timestamp_utc(t["created_at"]), clean.timestamp_utc(t["solved_at"])
        hours = None
        if opened and solved:
            if solved < opened:
                log.add("tickets", t["id"], cid, "INVALID_TIMESTAMPS", "solved before it was opened",
                        "resolution time left empty")
            else:
                hours = round((solved - opened).total_seconds() / 3600, 2)
        ticket_recs.append({"ticket_id": t["id"], "customer_id": cid,
                            "region": customers[cid]["region"] if cid else None, "subject": t["subject"],
                            "priority": t["priority"], "status": t["status"], "channel": t["channel"],
                            "opened_at": opened.isoformat() if opened else None,
                            "solved_at": solved.isoformat() if solved else None,
                            "resolution_hours": hours, "csat": t["csat"]})

    # ---------------------------------------------------------------- write
    warehouse.parent.mkdir(parents=True, exist_ok=True)
    tmp = warehouse.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    out.executescript(SCHEMA)

    def insert(table, recs):
        if recs:
            cols = list(recs[0])
            out.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [tuple(r[c] for c in cols) for r in recs])

    insert("reps", list(reps.values()))
    insert("customers", list(customers.values()))
    insert("prospects", prospect_recs)
    insert("products", [dict(r) for r in erp.execute("SELECT * FROM products")])
    insert("orders", orders)
    insert("order_lines", lines)
    insert("tickets", ticket_recs)
    out.executemany("INSERT INTO data_quality VALUES (?,?,?,?,?,?,?)", log.rows)
    out.executemany("INSERT INTO _meta VALUES (?,?)", [("as_of", AS_OF),
                                                         ("built_at", datetime.now(timezone.utc).isoformat())])
    out.commit()
    out.close()
    tmp.replace(warehouse)

    stats = matching_accuracy(match_method, ticket_method)
    stats["issues"] = dict(Counter(r[4] for r in log.rows))
    stats["counts"] = {"customers": len(customers), "prospects": len(prospect_recs), "orders": len(orders),
                       "order_lines": len(lines), "tickets": len(ticket_recs)}
    write_report(stats)
    return stats


def matching_accuracy(match_method: dict, ticket_match: dict) -> dict:
    """Compare matches with the generator's ground truth (only possible because the data is synthetic)."""
    truth = json.loads((SRC / "_truth.json").read_text())
    acc = Counter()
    for acct, true_cid in truth["accounts"].items():
        got, method = match_method[acct]
        acc["correct" if got == true_cid else "wrong"] += 1
        acc[f"method:{method}"] += 1
    tk = Counter()
    for tid, true_cid in truth["tickets"].items():
        got = ticket_match[tid]
        if got is None and true_cid is None:
            tk["correctly_unmatched"] += 1
        elif got == true_cid:
            tk["correct"] += 1
        elif got is None:
            tk["missed"] += 1
        else:
            tk["wrong"] += 1
    return {"accounts": dict(acc), "tickets": dict(tk)}


def write_report(s: dict):
    a, t = s["accounts"], s["tickets"]
    n_acc = a.get("correct", 0) + a.get("wrong", 0)
    n_t = sum(t.values())
    lines = ["# Data quality report", "", f"Data as of {AS_OF}. Generated by `python -m pipeline.build_warehouse`.", "",
             "## Rows loaded", "", "| Table | Rows |", "| --- | --- |"]
    lines += [f"| {k} | {v:,} |" for k, v in s["counts"].items()]
    lines += ["", "## Matching accuracy (checked against the generator's ground truth)", "",
              "| What | Result |", "| --- | --- |",
              f"| CRM accounts matched to the right ERP customer (or correctly left as prospects) | "
              f"{a.get('correct', 0)}/{n_acc} |",
              f"| ...matched by ERP id / exact name / fuzzy name / not matched | {a.get('method:erp_id', 0)} / "
              f"{a.get('method:name', 0)} / {a.get('method:fuzzy_name', 0)} / {a.get('method:none', 0)} |",
              f"| Tickets attributed to the right customer | {t.get('correct', 0)}/{n_t} |",
              f"| Tickets correctly left unattributed (personal email, no company) | {t.get('correctly_unmatched', 0)} |",
              f"| Tickets wrongly attributed | {t.get('wrong', 0)} |",
              f"| Tickets that could have been matched but weren't | {t.get('missed', 0)} |",
              "", "## Issues logged (all in the `data_quality` table)", "", "| Issue | Count |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in sorted(s["issues"].items(), key=lambda x: -x[1])]
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-url", default=None, help="support API base URL (default: run it in-process)")
    a = p.parse_args()
    s = build(a.api_url)
    c, acc, t = s["counts"], s["accounts"], s["tickets"]
    n_t = sum(t.values())
    top = sorted(s["issues"].items(), key=lambda x: -x[1])
    print(f"Loaded    {c['customers']} customers · {c['prospects']} prospects · {c['orders']:,} orders · "
          f"{c['tickets']:,} tickets")
    print(f"Accounts  {acc.get('correct', 0)}/{acc.get('correct', 0) + acc.get('wrong', 0)} matched correctly "
          f"({acc.get('method:erp_id', 0)} by ERP id, {acc.get('method:name', 0)} by name, "
          f"{acc.get('method:none', 0)} prospects)")
    print(f"Tickets   {t.get('correct', 0) + t.get('correctly_unmatched', 0):,}/{n_t:,} correct "
          f"({t.get('correctly_unmatched', 0)} rightly left unattributed, {t.get('wrong', 0)} wrong)")
    print(f"Issues    {sum(s['issues'].values())} logged: " + ", ".join(f"{k} {v}" for k, v in top[:3]) + ", ...")
    print(f"Wrote     {WAREHOUSE.relative_to(ROOT)} and {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
