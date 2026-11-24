"""The permission layer. Everything the AI runs goes through here.

How a query is made safe, in layers:
  1. Isolation: each user gets a private in-memory database holding ONLY the rows and columns they're
     allowed to see (copied from the warehouse, which is opened read-only). The link to the warehouse is
     then cut (DETACH), so data outside their access doesn't exist anywhere in their session.
  2. Read-only: PRAGMA query_only, plus an authorizer callback that refuses every action except reading:
     INSERT/UPDATE/DELETE/CREATE/DROP/ATTACH/PRAGMA... and SQLite's internal schema tables.
  3. Limits: one statement at a time, at most MAX_ROWS rows back, and a time limit per query.

The AI never sees or chooses any of this. It just writes SQL against "orders", "customers"...
(In production the same idea maps to Postgres row-level security and a read-only role per user.)
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "warehouse" / "warehouse.db"
POLICY_FILE = ROOT / "config" / "access_policy.yaml"
QUERY_LOG = ROOT / "logs" / "query_log.jsonl"

MAX_ROWS = 200
TIME_LIMIT_S = 3.0

TABLES = ["customers", "prospects", "reps", "products", "orders", "order_lines", "tickets", "data_quality"]

# How each table is narrowed to a user's regions when it's copied into their session.
# {r} is replaced with the quoted region list.
ROW_FILTERS = {
    "customers": "region IN ({r})",
    "prospects": "region IN ({r})",
    "orders": "region IN ({r})",
    "order_lines": "order_id IN (SELECT order_id FROM wh.orders WHERE region IN ({r}))",
    "tickets": "region IN ({r})",
    "data_quality": "customer_id IN (SELECT customer_id FROM wh.customers WHERE region IN ({r}))",
    "reps": None,       # the rep directory isn't sensitive
    "products": None,
}

# sqlite3 authorizer action codes that are always refused
_WRITE_ACTIONS = {
    sqlite3.SQLITE_INSERT: "INSERT", sqlite3.SQLITE_UPDATE: "UPDATE", sqlite3.SQLITE_DELETE: "DELETE",
    sqlite3.SQLITE_CREATE_TABLE: "CREATE TABLE", sqlite3.SQLITE_CREATE_TEMP_TABLE: "CREATE TABLE",
    sqlite3.SQLITE_CREATE_VIEW: "CREATE VIEW", sqlite3.SQLITE_CREATE_TEMP_VIEW: "CREATE VIEW",
    sqlite3.SQLITE_CREATE_INDEX: "CREATE INDEX", sqlite3.SQLITE_CREATE_TEMP_INDEX: "CREATE INDEX",
    sqlite3.SQLITE_CREATE_TRIGGER: "CREATE TRIGGER", sqlite3.SQLITE_CREATE_TEMP_TRIGGER: "CREATE TRIGGER",
    sqlite3.SQLITE_DROP_TABLE: "DROP", sqlite3.SQLITE_DROP_TEMP_TABLE: "DROP", sqlite3.SQLITE_DROP_VIEW: "DROP",
    sqlite3.SQLITE_DROP_TEMP_VIEW: "DROP", sqlite3.SQLITE_DROP_INDEX: "DROP", sqlite3.SQLITE_DROP_TEMP_INDEX: "DROP",
    sqlite3.SQLITE_DROP_TRIGGER: "DROP", sqlite3.SQLITE_DROP_TEMP_TRIGGER: "DROP",
    sqlite3.SQLITE_ALTER_TABLE: "ALTER", sqlite3.SQLITE_ATTACH: "ATTACH", sqlite3.SQLITE_DETACH: "DETACH",
    sqlite3.SQLITE_PRAGMA: "PRAGMA", sqlite3.SQLITE_TRANSACTION: "TRANSACTION", sqlite3.SQLITE_SAVEPOINT: "SAVEPOINT",
    sqlite3.SQLITE_ANALYZE: "ANALYZE", sqlite3.SQLITE_REINDEX: "REINDEX",
    sqlite3.SQLITE_CREATE_VTABLE: "CREATE VIRTUAL TABLE", sqlite3.SQLITE_DROP_VTABLE: "DROP",
}


@dataclass
class User:
    user_id: str
    name: str
    role: str
    regions: list[str] | None  # None = all regions
    hidden_columns: dict[str, list[str]] = field(default_factory=dict)

    def scope_text(self) -> str:
        return "all regions" if self.regions is None else "region " + ", ".join(self.regions) + " only"


@dataclass
class QueryResult:
    ok: bool
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error_code: str | None = None   # PERMISSION_DENIED | WRITE_BLOCKED | INVALID_SQL | TIMEOUT
    message: str | None = None
    scope: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def load_user(user_id: str, policy_file: Path = POLICY_FILE) -> User:
    policy = yaml.safe_load(policy_file.read_text())
    if user_id not in policy["users"]:
        raise KeyError(f"Unknown user '{user_id}'. Known users: {', '.join(policy['users'])}")
    u = policy["users"][user_id]
    role = policy["roles"][u["role"]]
    regions = None if u["regions"] == "all" else list(u["regions"])
    return User(user_id, u["name"], u["role"], regions, role.get("hidden_columns") or {})


def _quote(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


class Session:
    """One user's locked-down view of the warehouse."""

    def __init__(self, user: User, warehouse: Path = WAREHOUSE, log_file: Path | None = QUERY_LOG):
        if not warehouse.exists():
            raise FileNotFoundError(f"{warehouse} not found. Run: python -m pipeline.build_warehouse")
        self.user = user
        self.log_file = log_file
        self.con = sqlite3.connect(":memory:", uri=True, check_same_thread=False)
        self.con.execute("ATTACH DATABASE ? AS wh", (f"file:{warehouse.resolve()}?mode=ro",))
        self.visible: dict[str, list[str]] = {}
        self.hidden: dict[str, list[str]] = {}
        for t in TABLES:
            cols = [r[1] for r in self.con.execute(f"PRAGMA wh.table_info({t})")]
            hidden = set(user.hidden_columns.get(t, []))
            keep = [c for c in cols if c not in hidden]
            where = ""
            if user.regions is not None and ROW_FILTERS[t]:
                where = " WHERE " + ROW_FILTERS[t].format(r=", ".join(_quote(x) for x in user.regions))
            self.con.execute(f"CREATE TABLE main.{t} AS SELECT {', '.join(keep)} FROM wh.{t}{where}")
            self.visible[t] = keep
            self.hidden[t] = [c for c in cols if c in hidden]
        for t, c in [("orders", "customer_id"), ("orders", "order_date"), ("order_lines", "order_id"),
                     ("tickets", "customer_id"), ("customers", "customer_id")]:
            self.con.execute(f"CREATE INDEX main.ix_{t}_{c} ON {t}({c})")
        self.con.commit()
        self.con.execute("DETACH DATABASE wh")       # nothing outside this user's access is reachable now
        self.con.execute("PRAGMA query_only = ON")   # belt and braces: the connection itself refuses writes
        self._denied: str | None = None
        self._last_sql = ""
        self.con.set_authorizer(self._authorize)  # from here on, every statement is checked
        self._deadline = 0.0
        self.con.set_progress_handler(self._check_time, 20_000)

    # ------------------------------------------------------------------ guards
    def _authorize(self, action, arg1, arg2, dbname, source):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = (arg1 or "").lower()
            if table.startswith("sqlite_"):
                self._denied = "Reading SQLite's internal schema tables isn't allowed; use list_tables instead."
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in ("load_extension", "readfile", "writefile"):
                self._denied = f"Function {arg2} isn't allowed."
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self._denied = f"{_WRITE_ACTIONS.get(action, 'This statement')} isn't allowed: this connection is read-only."
        return sqlite3.SQLITE_DENY

    def _check_time(self):
        return 1 if time.monotonic() > self._deadline else 0  # non-zero aborts the query

    # ------------------------------------------------------------------ query
    def run(self, sql: str) -> QueryResult:
        start = time.monotonic()
        sql = (sql or "").strip()
        res = QueryResult(ok=False, sql=sql, scope=self.user.scope_text())
        self._denied = None
        self._last_sql = sql
        self._deadline = start + TIME_LIMIT_S
        try:
            if not sql:
                raise ValueError("Empty query")
            cur = self.con.execute(sql)
            if cur.description is None:
                raise ValueError("Only SELECT queries are supported")
            res.columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
            res.truncated = len(rows) > MAX_ROWS
            res.rows = [list(r) for r in rows[:MAX_ROWS]]
            res.row_count = len(res.rows)
            res.ok = True
        except sqlite3.DatabaseError as e:
            res.error_code, res.message = self._explain(str(e))
        except (ValueError, sqlite3.ProgrammingError, sqlite3.Warning) as e:
            res.error_code, res.message = "INVALID_SQL", str(e)
        res.elapsed_ms = int((time.monotonic() - start) * 1000)
        self._log(res)
        return res

    def _explain(self, err: str) -> tuple[str, str]:
        verb = (re.match(r"\s*(\w+)", self._last_sql) or [None, "?"])[1].upper()
        read_only = (f"Only SELECT queries are allowed: this connection is read-only "
                     f"(blocked: {verb}).")
        if self._denied:
            if "read-only" in self._denied:
                return "WRITE_BLOCKED", read_only
            return "PERMISSION_DENIED", self._denied
        if "interrupted" in err:
            return "TIMEOUT", f"Query stopped after {TIME_LIMIT_S:.0f}s. Narrow it down or aggregate first."
        if "readonly" in err or "read-only" in err or "cannot modify" in err or "query_only" in err:
            return "WRITE_BLOCKED", read_only
        m = re.search(r"no such column: (?:\w+\.)?(\w+)", err)
        if m:
            col = m.group(1)
            tables = [t for t, cols in self.hidden.items() if col in cols]
            if tables:
                return ("PERMISSION_DENIED", f"Column '{col}' ({', '.join(tables)}) isn't available to the "
                                             f"{self.user.role} role.")
        if re.search(r"no such table: \w+\.", err) or "unknown database" in err:
            return ("PERMISSION_DENIED", "Only this user's tables exist in this session; query them by name "
                                         "(orders, customers...).")
        m = re.search(r"no such table: (?:\w+\.)?(\w+)", err)
        if m:
            return "INVALID_SQL", f"No table '{m.group(1)}'. Available: {', '.join(TABLES)}."
        if "one statement at a time" in err:
            return "INVALID_SQL", "Send one SELECT statement at a time."
        return "INVALID_SQL", err

    def _log(self, res: QueryResult):
        if not self.log_file:
            return
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "user": self.user.user_id, "sql": res.sql,
                 "ok": res.ok, "error_code": res.error_code, "rows": res.row_count, "ms": res.elapsed_ms}
        with self.log_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
