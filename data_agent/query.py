"""Run one SQL query as a specific user, through the same permission layer the AI uses.
Handy for checking what each user can see.

    python -m data_agent.query --user maria "SELECT region, COUNT(*) FROM orders GROUP BY region"
"""
from __future__ import annotations

import argparse

from data_agent.access import Session, load_user


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", required=True, help="maria, dev, priya or sam")
    p.add_argument("sql")
    a = p.parse_args()
    user = load_user(a.user)
    r = Session(user, log_file=None).run(a.sql)
    print(f"[{user.name} · {user.role} · {user.scope_text()}]")
    if not r.ok:
        print(f"{r.error_code}: {r.message}")
        return
    widths = [max(len(str(c)), *(len(f"{row[i]:,}" if isinstance(row[i], (int, float)) else str(row[i]))
                                 for row in r.rows)) if r.rows else len(str(c)) for i, c in enumerate(r.columns)]

    def fmt(v):
        return f"{v:,}" if isinstance(v, (int, float)) else str(v)

    print("  ".join(str(c).ljust(w) for c, w in zip(r.columns, widths)))
    for row in r.rows:
        print("  ".join(fmt(v).ljust(w) for v, w in zip(row, widths)))
    print(f"({r.row_count} row{'s' if r.row_count != 1 else ''}{', truncated' if r.truncated else ''})")


if __name__ == "__main__":
    main()
