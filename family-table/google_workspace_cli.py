"""
Local Google Workspace CLI helper for Open WebUI's private `Google Workspace` tool.
Reuses the exact same Google Cloud OAuth 2.0 Desktop App token and private config.json as The Family Table
(%USERPROFILE%\\AI\\secrets\\family_table\\, never synced to Git).

Subcommands:
  events --days 7 [--start YYYY-MM-DD] [--query TEXT]
  search-emails --query "newer_than:7d" --max 10
  read-thread --thread-id <ID>
  create-draft --to <EMAILS> --subject <SUBJ> --body-file <PATH>
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from googleapiclient.discovery import build  # noqa: E402
import google_connector as gc  # noqa: E402


def _cmd_events(creds, cfg: dict[str, Any], days: int, start_date: str, query: str) -> dict[str, Any]:
    days = max(1, min(int(days or 7), 90))
    if start_date and re.match(r"^\d{4}-\d{2}-\d{2}$", start_date.strip()):
        start_d = datetime.strptime(start_date.strip(), "%Y-%m-%d").date()
    else:
        start_d = datetime.now().date()
    end_d = start_d + timedelta(days=days)
    time_min = f"{start_d.isoformat()}T00:00:00Z"
    time_max = f"{end_d.isoformat()}T23:59:59Z"

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    cal_cfg = cfg.get("calendars") or {}
    cals = cal_cfg.get("include") or [{"id": "primary", "label": "Primary"}]
    skip_private = bool(cal_cfg.get("exclude_private_visibility", True))
    exclude_res = [re.compile(pat, re.I) for pat in (cal_cfg.get("exclude_title_regex") or [])]
    q_lc = (query or "").strip().lower()

    seen: set[tuple[str, str, str]] = set()
    events: list[dict[str, Any]] = []
    errors: list[str] = []

    for cal in cals:
        cal_id = cal.get("id")
        cal_label = cal.get("label") or cal_id
        if not cal_id:
            continue
        try:
            res = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=120,
            ).execute()
        except Exception as e:
            errors.append(f"{cal_label}: {e}")
            continue

        for ev in res.get("items") or []:
            if ev.get("status") == "cancelled":
                continue
            summary = (ev.get("summary") or "").strip()
            if not summary:
                continue
            if skip_private and (ev.get("visibility") or "").lower() in ("private", "confidential"):
                continue
            if any(rx.search(summary) for rx in exclude_res):
                continue
            start_obj = ev.get("start") or {}
            end_obj = ev.get("end") or {}
            start_val = start_obj.get("dateTime") or start_obj.get("date") or ""
            end_val = end_obj.get("dateTime") or end_obj.get("date") or ""
            loc = (ev.get("location") or "").strip()
            desc = gc._strip_html(ev.get("description") or "")[:300]

            if q_lc and q_lc not in f"{summary} {loc} {desc}".lower():
                continue

            key = (start_val[:16], end_val[:16], summary.lower())
            if key in seen:
                continue
            seen.add(key)

            all_day = "date" in start_obj and "dateTime" not in start_obj
            weekday = ""
            date_short = start_val[:10]
            when = ""
            time_range = "All day"
            if re.match(r"^\d{4}-\d{2}-\d{2}", start_val):
                try:
                    ev_d = datetime.strptime(start_val[:10], "%Y-%m-%d").date()
                    delta = (ev_d - datetime.now().date()).days
                    weekday = ev_d.strftime("%a")
                    date_short = f"{ev_d.strftime('%a')} {ev_d.day} {ev_d.strftime('%b %Y')}"
                    when = "Today" if delta == 0 else ("Tomorrow" if delta == 1 else (f"In {delta} days" if delta > 1 else f"{abs(delta)}d ago"))
                except ValueError:
                    pass
            if not all_day and "T" in start_val:
                t1 = start_val.split("T", 1)[1][:5]
                t2 = end_val.split("T", 1)[1][:5] if "T" in end_val else ""
                time_range = f"{t1}–{t2}" if t2 else t1

            item: dict[str, Any] = {
                "calendar": cal_label,
                "date": date_short,
                "weekday": weekday,
                "when": when,
                "time": time_range,
                "summary": summary,
            }
            if loc:
                item["location"] = loc[:160]
            if desc:
                item["description"] = desc
            if (ev.get("visibility") or "").lower() in ("private", "confidential"):
                item["private"] = True
            events.append((start_val, item))

    events.sort(key=lambda pair: pair[0])
    return {
        "ok": True,
        "window": {"start": start_d.isoformat(), "end": end_d.isoformat(), "days": days},
        "query": query,
        "count": len(events),
        "events": [item for _, item in events[:60]],
        **({"errors": errors} if errors else {}),
    }


def _cmd_search_emails(creds, query: str, max_results: int) -> dict[str, Any]:
    q = (query or "newer_than:7d").strip()
    max_n = max(1, min(int(max_results or 15), 30))
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    listed = service.users().threads().list(userId="me", q=q, maxResults=max_n).execute()
    items: list[dict[str, Any]] = []

    for stub in listed.get("threads") or []:
        tid = stub.get("id")
        if not tid:
            continue
        try:
            th = service.users().threads().get(
                userId="me",
                id=tid,
                format="metadata",
                metadataHeaders=["From", "To", "Cc", "Date", "Subject"],
            ).execute()
        except Exception:
            continue
        msgs = th.get("messages") or []
        if not msgs:
            continue
        last_msg = msgs[-1]
        last_hdrs = ((last_msg.get("payload") or {}).get("headers")) or []
        unread = any("UNREAD" in (m.get("labelIds") or []) for m in msgs)

        participants: list[str] = []
        seen_part: set[str] = set()
        cc_list: list[str] = []
        seen_cc: set[str] = set()
        for m in msgs:
            hdrs = ((m.get("payload") or {}).get("headers")) or []
            for key in ("From", "To", "Cc"):
                val = gc._header(hdrs, key)
                if val and val not in seen_part and len(participants) < 12:
                    seen_part.add(val)
                    participants.append(val[:100])
            cc_val = gc._header(hdrs, "Cc")
            if cc_val and cc_val not in seen_cc and len(cc_list) < 6:
                seen_cc.add(cc_val)
                cc_list.append(cc_val[:140])

        entry: dict[str, Any] = {
            "thread_id": tid,
            "message_count": len(msgs),
            "date": gc._header(last_hdrs, "Date")[:45],
            "from": gc._header(last_hdrs, "From")[:120],
            "to": gc._header(last_hdrs, "To")[:120],
            "subject": gc._header(last_hdrs, "Subject")[:180],
            "unread": unread,
            "snippet": (last_msg.get("snippet") or stub.get("snippet") or "")[:280],
        }
        if cc_list:
            entry["cc"] = ", ".join(cc_list)[:220]
        if len(participants) > 2:
            entry["participants"] = participants
        items.append(entry)
        if len(items) >= max_n:
            break

    return {"ok": True, "query": q, "count": len(items), "emails": items}


def _cmd_read_thread(creds, thread_id: str) -> dict[str, Any]:
    tid = (thread_id or "").strip()
    if not tid:
        raise ValueError("thread_id is required")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    th = service.users().threads().get(userId="me", id=tid, format="full").execute()
    msgs = th.get("messages") or []
    out_msgs = []
    for m in msgs[-10:]:
        payload = m.get("payload") or {}
        hdrs = payload.get("headers") or []
        cc_val = gc._header(hdrs, "Cc")[:200]
        item = {
            "from": gc._header(hdrs, "From")[:160],
            "to": gc._header(hdrs, "To")[:160],
            "date": gc._header(hdrs, "Date")[:60],
            "subject": gc._header(hdrs, "Subject")[:200],
            "body": gc._extract_message_text(payload)[:3500],
        }
        if cc_val:
            item["cc"] = cc_val
        out_msgs.append(item)
    return {"ok": True, "thread_id": tid, "total_messages": len(msgs), "messages": out_msgs}


def _cmd_create_draft(creds, to_addr: str, subject: str, body_text: str) -> dict[str, Any]:
    to_clean = (to_addr or "").strip()
    subj_clean = (subject or "").strip()
    if not to_clean or not subj_clean:
        raise ValueError("Both 'to' and 'subject' are required to create a Gmail draft.")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    mime = MIMEText(body_text or "", "plain", "utf-8")
    mime["To"] = to_clean
    mime["Subject"] = subj_clean
    raw_b64 = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    res = service.users().drafts().create(userId="me", body={"message": {"raw": raw_b64}}).execute()
    return {"ok": True, "action": "draft_created", "draft_id": res.get("id"), "to": to_clean, "subject": subj_clean}


def main() -> int:
    ap = argparse.ArgumentParser(description="Local Google Workspace CLI (Calendar & Gmail via OAuth 2.0)")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ev = sub.add_parser("events")
    p_ev.add_argument("--days", type=int, default=7)
    p_ev.add_argument("--start", default="")
    p_ev.add_argument("--query", default="")

    p_se = sub.add_parser("search-emails")
    p_se.add_argument("--query", default="newer_than:7d")
    p_se.add_argument("--max", type=int, default=15)

    p_rt = sub.add_parser("read-thread")
    p_rt.add_argument("--thread-id", required=True)

    p_dr = sub.add_parser("create-draft")
    p_dr.add_argument("--to", required=True)
    p_dr.add_argument("--subject", required=True)
    p_dr.add_argument("--body-file", required=True)

    args = ap.parse_args()
    try:
        cfg, secrets_dir = gc.load_config(args.config)
        creds = gc.get_credentials(cfg, secrets_dir, interactive=False)
        if args.cmd == "events":
            out = _cmd_events(creds, cfg, args.days, args.start, args.query)
        elif args.cmd == "search-emails":
            out = _cmd_search_emails(creds, args.query, args.max)
        elif args.cmd == "read-thread":
            out = _cmd_read_thread(creds, args.thread_id)
        elif args.cmd == "create-draft":
            body_text = Path(args.body_file).read_text(encoding="utf-8")
            out = _cmd_create_draft(creds, args.to, args.subject, body_text)
        else:
            raise ValueError(f"Unknown command: {args.cmd}")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except gc.GoogleAuthError as e:
        print(json.dumps({"ok": False, "auth_error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
