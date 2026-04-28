"""
Insert-only data merge from a DEV SQLite DB into PROD SQLite DB.

Goals:
- Preserve all existing production data.
- Add records that exist in dev but are missing in prod.
- Never update existing prod records.
- Never delete prod records.

Usage:
  python scripts/safe_data_merge.py --prod-db data/gazebo_gic.db --dev-db D:/backups/dev_snapshot.db
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Dict, Optional


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def fetch_all(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def insert_row(conn: sqlite3.Connection, table: str, data: Dict[str, object]) -> None:
    cols = list(data.keys())
    placeholders = ", ".join(["?"] * len(cols))
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, [data[c] for c in cols])


def common_payload(
    prod_conn: sqlite3.Connection,
    dev_row: sqlite3.Row,
    table: str,
    exclude: Optional[set[str]] = None,
) -> Dict[str, object]:
    exclude = exclude or set()
    cols = table_columns(prod_conn, table)
    data: Dict[str, object] = {}
    for k in dev_row.keys():
        if k in cols and k not in exclude:
            data[k] = dev_row[k]
    return data


def build_member_maps(prod_conn: sqlite3.Connection) -> tuple[Dict[str, int], Dict[int, str]]:
    rows = prod_conn.execute("SELECT id, member_no FROM members").fetchall()
    by_no = {r["member_no"]: r["id"] for r in rows if r["member_no"]}
    by_id = {r["id"]: r["member_no"] for r in rows if r["member_no"]}
    return by_no, by_id


def build_user_maps(prod_conn: sqlite3.Connection) -> tuple[Dict[str, int], Dict[int, str]]:
    rows = prod_conn.execute("SELECT id, username FROM users").fetchall()
    by_username = {r["username"]: r["id"] for r in rows if r["username"]}
    by_id = {r["id"]: r["username"] for r in rows if r["username"]}
    return by_username, by_id


def build_loan_maps(prod_conn: sqlite3.Connection) -> tuple[Dict[str, int], Dict[int, str]]:
    rows = prod_conn.execute("SELECT id, loan_no FROM loans").fetchall()
    by_no = {r["loan_no"]: r["id"] for r in rows if r["loan_no"]}
    by_id = {r["id"]: r["loan_no"] for r in rows if r["loan_no"]}
    return by_no, by_id


def merge_members(prod: sqlite3.Connection, dev: sqlite3.Connection, stats: Dict[str, int]) -> None:
    prod_member_by_no, _ = build_member_maps(prod)
    for row in fetch_all(dev, "members"):
        member_no = row["member_no"]
        if not member_no or member_no in prod_member_by_no:
            stats["members_skipped"] += 1
            continue
        payload = common_payload(prod, row, "members", exclude={"id"})
        insert_row(prod, "members", payload)
        stats["members_inserted"] += 1
    prod.commit()


def merge_users(prod: sqlite3.Connection, dev: sqlite3.Connection, stats: Dict[str, int]) -> None:
    prod_member_by_no, _ = build_member_maps(prod)
    prod_user_by_name, _ = build_user_maps(prod)
    dev_members = {r["id"]: r["member_no"] for r in fetch_all(dev, "members")}

    for row in fetch_all(dev, "users"):
        username = row["username"]
        if not username or username in prod_user_by_name:
            stats["users_skipped"] += 1
            continue

        payload = common_payload(prod, row, "users", exclude={"id", "member_id"})
        member_no = dev_members.get(row["member_id"]) if row["member_id"] else None
        payload["member_id"] = prod_member_by_no.get(member_no) if member_no else None
        insert_row(prod, "users", payload)
        stats["users_inserted"] += 1
    prod.commit()


def merge_loans(prod: sqlite3.Connection, dev: sqlite3.Connection, stats: Dict[str, int]) -> None:
    prod_member_by_no, _ = build_member_maps(prod)
    prod_user_by_name, _ = build_user_maps(prod)

    dev_members = {r["id"]: r["member_no"] for r in fetch_all(dev, "members")}
    dev_users = {r["id"]: r["username"] for r in fetch_all(dev, "users")}
    prod_loan_by_no, _ = build_loan_maps(prod)

    for row in fetch_all(dev, "loans"):
        loan_no = row["loan_no"]
        if not loan_no or loan_no in prod_loan_by_no:
            stats["loans_skipped"] += 1
            continue

        payload = common_payload(
            prod,
            row,
            "loans",
            exclude={"id", "member_id", "guarantor1_id", "guarantor2_id", "approved_by"},
        )

        borrower_no = dev_members.get(row["member_id"]) if row["member_id"] else None
        g1_no = dev_members.get(row["guarantor1_id"]) if row["guarantor1_id"] else None
        g2_no = dev_members.get(row["guarantor2_id"]) if row["guarantor2_id"] else None
        approved_by_username = dev_users.get(row["approved_by"]) if row["approved_by"] else None

        payload["member_id"] = prod_member_by_no.get(borrower_no)
        payload["guarantor1_id"] = prod_member_by_no.get(g1_no) if g1_no else None
        payload["guarantor2_id"] = prod_member_by_no.get(g2_no) if g2_no else None
        payload["approved_by"] = prod_user_by_name.get(approved_by_username) if approved_by_username else None

        if not payload["member_id"]:
            stats["loans_skipped"] += 1
            continue

        insert_row(prod, "loans", payload)
        stats["loans_inserted"] += 1
    prod.commit()


def merge_remaining(prod: sqlite3.Connection, dev: sqlite3.Connection, stats: Dict[str, int]) -> None:
    prod_member_by_no, _ = build_member_maps(prod)
    prod_user_by_name, _ = build_user_maps(prod)
    prod_loan_by_no, _ = build_loan_maps(prod)

    dev_members = {r["id"]: r["member_no"] for r in fetch_all(dev, "members")}
    dev_users = {r["id"]: r["username"] for r in fetch_all(dev, "users")}
    dev_loans = {r["id"]: r["loan_no"] for r in fetch_all(dev, "loans")}

    def map_member_id(dev_member_id: Optional[int]) -> Optional[int]:
        if not dev_member_id:
            return None
        no = dev_members.get(dev_member_id)
        return prod_member_by_no.get(no) if no else None

    def map_user_id(dev_user_id: Optional[int]) -> Optional[int]:
        if not dev_user_id:
            return None
        username = dev_users.get(dev_user_id)
        return prod_user_by_name.get(username) if username else None

    def map_loan_id(dev_loan_id: Optional[int]) -> Optional[int]:
        if not dev_loan_id:
            return None
        loan_no = dev_loans.get(dev_loan_id)
        return prod_loan_by_no.get(loan_no) if loan_no else None

    def add_count(key: str) -> None:
        stats[key] = stats.get(key, 0) + 1

    # savings
    for row in fetch_all(dev, "savings"):
        member_id = map_member_id(row["member_id"])
        if not member_id:
            add_count("savings_skipped")
            continue
        exists = prod.execute(
            "SELECT 1 FROM savings WHERE member_id=? AND period=?",
            (member_id, row["period"]),
        ).fetchone()
        if exists:
            add_count("savings_skipped")
            continue
        payload = common_payload(prod, row, "savings", exclude={"id", "member_id", "recorded_by"})
        payload["member_id"] = member_id
        payload["recorded_by"] = map_user_id(row["recorded_by"])
        insert_row(prod, "savings", payload)
        add_count("savings_inserted")

    # annual_fees
    for row in fetch_all(dev, "annual_fees"):
        member_id = map_member_id(row["member_id"])
        if not member_id:
            add_count("annual_fees_skipped")
            continue
        exists = prod.execute(
            "SELECT 1 FROM annual_fees WHERE member_id=? AND year=?",
            (member_id, row["year"]),
        ).fetchone()
        if exists:
            add_count("annual_fees_skipped")
            continue
        payload = common_payload(prod, row, "annual_fees", exclude={"id", "member_id", "recorded_by"})
        payload["member_id"] = member_id
        payload["recorded_by"] = map_user_id(row["recorded_by"])
        insert_row(prod, "annual_fees", payload)
        add_count("annual_fees_inserted")

    # fines
    for row in fetch_all(dev, "fines"):
        member_id = map_member_id(row["member_id"])
        if not member_id:
            add_count("fines_skipped")
            continue
        exists = prod.execute(
            "SELECT 1 FROM fines WHERE member_id=? AND fine_date=? AND violation=? AND amount=?",
            (member_id, row["fine_date"], row["violation"], row["amount"]),
        ).fetchone()
        if exists:
            add_count("fines_skipped")
            continue
        payload = common_payload(prod, row, "fines", exclude={"id", "member_id", "issued_by"})
        payload["member_id"] = member_id
        payload["issued_by"] = map_user_id(row["issued_by"])
        insert_row(prod, "fines", payload)
        add_count("fines_inserted")

    # operational_incomes
    for row in fetch_all(dev, "operational_incomes"):
        exists = prod.execute("SELECT 1 FROM operational_incomes WHERE income_no=?", (row["income_no"],)).fetchone()
        if exists:
            add_count("operational_incomes_skipped")
            continue
        payload = common_payload(prod, row, "operational_incomes", exclude={"id", "recorded_by"})
        payload["recorded_by"] = map_user_id(row["recorded_by"])
        insert_row(prod, "operational_incomes", payload)
        add_count("operational_incomes_inserted")

    # expenses
    for row in fetch_all(dev, "expenses"):
        exists = prod.execute("SELECT 1 FROM expenses WHERE expense_no=?", (row["expense_no"],)).fetchone()
        if exists:
            add_count("expenses_skipped")
            continue
        payload = common_payload(
            prod,
            row,
            "expenses",
            exclude={"id", "requested_by", "requested_approver_user_id", "approved_by"},
        )
        payload["requested_by"] = map_user_id(row["requested_by"])
        payload["requested_approver_user_id"] = map_user_id(row["requested_approver_user_id"])
        payload["approved_by"] = map_user_id(row["approved_by"])
        insert_row(prod, "expenses", payload)
        add_count("expenses_inserted")

    # loan_repayments
    for row in fetch_all(dev, "loan_repayments"):
        loan_id = map_loan_id(row["loan_id"])
        if not loan_id:
            add_count("loan_repayments_skipped")
            continue
        exists = prod.execute(
            "SELECT 1 FROM loan_repayments WHERE loan_id=? AND payment_date=? AND amount=? AND IFNULL(reference_no,'')=IFNULL(?, '')",
            (loan_id, row["payment_date"], row["amount"], row["reference_no"]),
        ).fetchone()
        if exists:
            add_count("loan_repayments_skipped")
            continue
        payload = common_payload(prod, row, "loan_repayments", exclude={"id", "loan_id", "recorded_by"})
        payload["loan_id"] = loan_id
        payload["recorded_by"] = map_user_id(row["recorded_by"])
        insert_row(prod, "loan_repayments", payload)
        add_count("loan_repayments_inserted")

    # loan_penalties
    for row in fetch_all(dev, "loan_penalties"):
        loan_id = map_loan_id(row["loan_id"])
        if not loan_id:
            add_count("loan_penalties_skipped")
            continue
        exists = prod.execute(
            "SELECT 1 FROM loan_penalties WHERE loan_id=? AND period=?",
            (loan_id, row["period"]),
        ).fetchone()
        if exists:
            add_count("loan_penalties_skipped")
            continue
        payload = common_payload(prod, row, "loan_penalties", exclude={"id", "loan_id"})
        payload["loan_id"] = loan_id
        insert_row(prod, "loan_penalties", payload)
        add_count("loan_penalties_inserted")

    # minutes
    for row in fetch_all(dev, "minutes"):
        exists = prod.execute(
            "SELECT 1 FROM minutes WHERE meeting_date=? AND title=?",
            (row["meeting_date"], row["title"]),
        ).fetchone()
        if exists:
            add_count("minutes_skipped")
            continue
        payload = common_payload(prod, row, "minutes", exclude={"id", "chaired_by", "recorded_by"})
        payload["chaired_by"] = map_member_id(row["chaired_by"])
        payload["recorded_by"] = map_user_id(row["recorded_by"])
        insert_row(prod, "minutes", payload)
        add_count("minutes_inserted")

    # dividend_runs
    for row in fetch_all(dev, "dividend_runs"):
        exists = prod.execute("SELECT 1 FROM dividend_runs WHERE year=?", (row["year"],)).fetchone()
        if exists:
            add_count("dividend_runs_skipped")
            continue
        payload = common_payload(prod, row, "dividend_runs", exclude={"id", "top_saver_id", "processed_by"})
        payload["top_saver_id"] = map_member_id(row["top_saver_id"])
        payload["processed_by"] = map_user_id(row["processed_by"])
        insert_row(prod, "dividend_runs", payload)
        add_count("dividend_runs_inserted")

    # refresh dividend run map after inserts
    prod_dividend_runs = {r["year"]: r["id"] for r in fetch_all(prod, "dividend_runs")}
    dev_dividend_runs = {r["id"]: r["year"] for r in fetch_all(dev, "dividend_runs")}

    # dividend_payouts
    for row in fetch_all(dev, "dividend_payouts"):
        year = dev_dividend_runs.get(row["dividend_run_id"])
        run_id = prod_dividend_runs.get(year)
        member_id = map_member_id(row["member_id"])
        if not run_id or not member_id:
            add_count("dividend_payouts_skipped")
            continue
        exists = prod.execute(
            "SELECT 1 FROM dividend_payouts WHERE dividend_run_id=? AND member_id=? AND amount=? AND is_top_saver=?",
            (run_id, member_id, row["amount"], row["is_top_saver"]),
        ).fetchone()
        if exists:
            add_count("dividend_payouts_skipped")
            continue
        payload = common_payload(prod, row, "dividend_payouts", exclude={"id", "dividend_run_id", "member_id"})
        payload["dividend_run_id"] = run_id
        payload["member_id"] = member_id
        insert_row(prod, "dividend_payouts", payload)
        add_count("dividend_payouts_inserted")

    # settings: insert missing keys only
    for row in fetch_all(dev, "settings"):
        exists = prod.execute("SELECT 1 FROM settings WHERE key=?", (row["key"],)).fetchone()
        if exists:
            add_count("settings_skipped")
            continue
        payload = common_payload(prod, row, "settings")
        insert_row(prod, "settings", payload)
        add_count("settings_inserted")

    prod.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Insert-only dev->prod data merge for Gazebo GIC.")
    parser.add_argument("--prod-db", required=True, help="Path to production SQLite DB")
    parser.add_argument("--dev-db", required=True, help="Path to development SQLite DB snapshot")
    args = parser.parse_args()

    prod = connect(args.prod_db)
    dev = connect(args.dev_db)

    stats: Dict[str, int] = {
        "members_inserted": 0,
        "members_skipped": 0,
        "users_inserted": 0,
        "users_skipped": 0,
        "loans_inserted": 0,
        "loans_skipped": 0,
    }

    try:
        merge_members(prod, dev, stats)
        merge_users(prod, dev, stats)
        merge_loans(prod, dev, stats)
        merge_remaining(prod, dev, stats)
    finally:
        dev.close()
        prod.close()

    print("Insert-only merge complete.")
    for k in sorted(stats.keys()):
        print(f"  {k}: {stats[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
