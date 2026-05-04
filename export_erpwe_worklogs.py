"""
Export Jira worklogs for a project (default ERPWE) to CSV.

One row per worklog entry, filtered by worklog start time (--worklog-from / --worklog-to).

Reuses JiraClient from export_erpwe_comments.py (same .env: JIRA_BASE_URL, JIRA_USER, JIRA_PASSWORD).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from export_erpwe_comments import (
    JiraClient,
    _adf_to_text,
    _description_plain,
    _paginate_worklogs,
    _parse_dt,
    _user_email_name,
)


def _worklog_comment_plain(wl: dict[str, Any]) -> str:
    c = wl.get("comment")
    if isinstance(c, str):
        return c.replace("\r\n", "\n").strip()
    if isinstance(c, dict):
        return _adf_to_text(c).strip()
    return ""


def _as_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_date_or_dt(s: str) -> datetime:
    """Accept YYYY-MM-DD or full ISO; date-only is start/end of day UTC."""
    s = s.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        d = date.fromisoformat(s)
        return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    dt = _parse_dt(s.replace("Z", "+00:00"))
    if not dt:
        raise ValueError(f"Invalid date/time: {s!r}")
    return _as_utc_aware(dt)


def _end_of_day_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=timezone.utc)


def _default_jql(project: str, d_lo: date, d_hi: date) -> str:
    """Narrow issues via worklogDate (Jira Server 7.6+). Still filter each worklog by `started`."""
    return (
        f'project = {project} AND worklogDate >= "{d_lo.strftime("%Y/%m/%d")}" '
        f'AND worklogDate <= "{d_hi.strftime("%Y/%m/%d")}" ORDER BY updated DESC'
    )


def run(
    *,
    client: JiraClient,
    jql: str,
    worklog_from: datetime,
    worklog_to: datetime,
    outfile: str,
    sleep_s: float,
) -> None:
    fields = [
        "summary",
        "description",
        "issuetype",
        "reporter",
        "assignee",
        "labels",
        "components",
        "created",
        "status",
    ]
    page = 50
    start = 0
    issues: list[dict[str, Any]] = []
    while True:
        data = client.search_issues(jql, fields, start, page)
        chunk = data.get("issues") or []
        issues.extend(chunk)
        total = int(data.get("total", len(issues)))
        start += len(chunk)
        if start >= total or not chunk:
            break
        if sleep_s:
            time.sleep(sleep_s)

    wf, wt = worklog_from, worklog_to
    wf = _as_utc_aware(wf) if wf.tzinfo else wf.replace(tzinfo=timezone.utc)
    wt = _as_utc_aware(wt) if wt.tzinfo else wt.replace(tzinfo=timezone.utc)

    fieldnames = [
        "Issue Type",
        "Issue number",
        "description",
        "labels",
        "components",
        "Creation date",
        "status",
        "author",
        "author email",
        "assignee",
        "worker (who created work log)",
        "worklog author email",
        "work start",
        "actual hours spent",
        "work description",
    ]

    rows_written = 0
    with open(outfile, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f, fieldnames=fieldnames, extrasaction="ignore", delimiter=";"
        )
        w.writeheader()

        for i, issue in enumerate(issues):
            key = issue.get("key") or ""
            fld = issue.get("fields") or {}
            rendered = issue.get("renderedFields") or {}

            it = (fld.get("issuetype") or {}).get("name") or ""
            desc = _description_plain(fld, rendered)
            created = fld.get("created") or ""
            st = (fld.get("status") or {}).get("name") or ""

            rep = fld.get("reporter") or {}
            auth_email, auth_name = _user_email_name(rep)

            asn = fld.get("assignee") or {}
            assignee_name = (asn.get("displayName") or asn.get("name") or "").strip()

            labels = ",".join(sorted(fld.get("labels") or []))
            comps = ",".join(
                sorted((c.get("name") or "") for c in (fld.get("components") or []) if c.get("name"))
            )

            if sleep_s:
                time.sleep(sleep_s)
            raw_wls = _paginate_worklogs(client, key)

            for wl in raw_wls:
                st_raw = wl.get("started")
                w_started = _parse_dt(st_raw)
                if not w_started:
                    continue
                w_started = _as_utc_aware(w_started) if w_started.tzinfo else w_started.replace(
                    tzinfo=timezone.utc
                )
                if not (wf <= w_started <= wt):
                    continue

                w_author = wl.get("author") or {}
                w_email, w_name = _user_email_name(w_author)
                sec = int(wl.get("timeSpentSeconds") or 0)
                hours = round(sec / 3600.0, 4)

                w.writerow(
                    {
                        "Issue Type": it,
                        "Issue number": key,
                        "description": desc,
                        "labels": labels,
                        "components": comps,
                        "Creation date": created,
                        "status": st,
                        "author": auth_name,
                        "author email": auth_email,
                        "assignee": assignee_name,
                        "worker (who created work log)": w_name or w_email,
                        "worklog author email": w_email,
                        "work start": st_raw or "",
                        "actual hours spent": hours,
                        "work description": _worklog_comment_plain(wl),
                    }
                )
                rows_written += 1

            if (i + 1) % 20 == 0:
                print(f"Processed {i + 1}/{len(issues)} issues...", file=sys.stderr)

    print(f"Wrote {outfile} ({rows_written} worklog rows).", file=sys.stderr)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    load_dotenv(script_dir / ".env")
    load_dotenv()

    p = argparse.ArgumentParser(
        description="Export ERPWE (or other project) Jira worklogs to CSV, filtered by worklog start time."
    )
    p.add_argument(
        "--jira-base-url",
        default=os.environ.get("JIRA_BASE_URL", "http://tracker.vsevtyres.ru:8080/jira"),
    )
    p.add_argument("--project", default="ERPWE", help="Jira project key (default ERPWE).")
    p.add_argument(
        "--jql",
        default=None,
        help="Full JQL override. If omitted, built from --project and worklog dates (worklogDate).",
    )
    p.add_argument(
        "--worklog-from",
        required=True,
        help='Start of worklog window (inclusive): YYYY-MM-DD or ISO datetime, e.g. "2026-03-01".',
    )
    p.add_argument(
        "--worklog-to",
        required=True,
        help='End of worklog window (inclusive): YYYY-MM-DD or ISO datetime, e.g. "2026-04-30".',
    )
    p.add_argument("-o", "--output", default="erpwe_worklogs.csv")
    p.add_argument("--no-verify-ssl", action="store_true")
    p.add_argument("--sleep", type=float, default=0.15, help="Pause between API calls (seconds).")
    args = p.parse_args()

    user = os.environ.get("JIRA_USER", "").strip()
    password = os.environ.get("JIRA_PASSWORD", "").strip()
    if not user or not password:
        raise SystemExit(
            "Set JIRA_USER and JIRA_PASSWORD in .env or environment. See .env.example."
        )

    try:
        wf = _parse_date_or_dt(args.worklog_from)
        wt = _parse_date_or_dt(args.worklog_to)
    except ValueError as e:
        raise SystemExit(str(e)) from e

    # Inclusive end: plain date for --worklog-to means end of that day (UTC).
    s_from = args.worklog_from.strip()
    s_to = args.worklog_to.strip()
    if len(s_to) == 10 and s_to[4] == "-" and s_to[7] == "-":
        wt = _end_of_day_utc(date.fromisoformat(s_to))

    if wt < wf:
        raise SystemExit("--worklog-to must be >= --worklog-from")

    if args.jql:
        jql = args.jql
    else:
        d_lo = wf.date() if wf.tzinfo else wf.replace(tzinfo=timezone.utc).date()
        d_hi = wt.date() if wt.tzinfo else wt.replace(tzinfo=timezone.utc).date()
        jql = _default_jql(args.project, d_lo, d_hi)

    client = JiraClient(
        args.jira_base_url,
        user,
        password,
        verify_ssl=not args.no_verify_ssl,
    )
    run(
        client=client,
        jql=jql,
        worklog_from=wf,
        worklog_to=wt,
        outfile=args.output,
        sleep_s=args.sleep,
    )


if __name__ == "__main__":
    main()
