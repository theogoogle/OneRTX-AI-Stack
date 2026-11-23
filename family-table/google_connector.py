"""
Official Google Cloud OAuth 2.0 Desktop App connector for The Family Table.
Uses google-auth-oauthlib + google-api-python-client (zero App Passwords, zero public URLs).

Scopes (minimum needed):
  - gmail.readonly     (read school/nursery/party/invoice threads & replies)
  - gmail.compose      (create/update Gmail drafts with PDF attachment)
  - calendar.readonly  (read configured family & personal calendars)
  - spreadsheets       (read & append Memory Sheet suggestions)

Private config & tokens live strictly under %USERPROFILE%\\AI\\secrets\\family_table\\ (never in Git).
"""
from __future__ import annotations

import base64
import html as html_lib
import json
import os
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

DEFAULT_SECRETS_DIR = Path.home() / "AI" / "secrets" / "family_table"


class GoogleAuthError(RuntimeError):
    """Raised when Google OAuth credentials are missing or expired."""


def load_config(config_path: str | Path | None = None) -> tuple[dict[str, Any], Path]:
    cfg_path = Path(config_path) if config_path else (DEFAULT_SECRETS_DIR / "config.json")
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Private Family Table config not found: {cfg_path}. "
            "Copy config.example.json there and fill in your private values."
        )
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return cfg, cfg_path.parent


def _resolve_oauth_paths(cfg: dict[str, Any], secrets_dir: Path) -> tuple[Path, Path]:
    oauth = cfg.get("oauth") or {}
    secret_p = Path(oauth.get("client_secret_file") or "google_client_secret.json")
    token_p = Path(oauth.get("token_file") or "google_token.json")
    if not secret_p.is_absolute():
        secret_p = secrets_dir / secret_p
    if not token_p.is_absolute():
        token_p = secrets_dir / token_p
    return secret_p, token_p


def wait_for_network(host: str = "www.googleapis.com", timeout_s: int = 60) -> None:
    """After wake from hibernate, Wi-Fi/DNS can take a few seconds to reconnect."""
    deadline = time.time() + max(1, timeout_s)
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            socket.getaddrinfo(host, 443)
            return
        except OSError as e:
            last_err = e
            time.sleep(2)
    raise ConnectionError(f"Network unreachable ({host}): {last_err}")


def get_credentials(
    cfg: dict[str, Any],
    secrets_dir: Path,
    interactive: bool = False,
    force_reauth: bool = False,
) -> Credentials:
    secret_path, token_path = _resolve_oauth_paths(cfg, secrets_dir)
    creds: Credentials | None = None

    if not force_reauth and token_path.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as e:
            if not interactive:
                raise GoogleAuthError(f"Corrupted OAuth token at {token_path}: {e}. Re-run auth_setup.ps1.") from e
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError as e:
            if not interactive:
                raise GoogleAuthError(
                    "Google OAuth refresh token expired or was revoked (common after 7 days if the Google Cloud "
                    "OAuth consent screen is in 'Testing' mode). Run auth_setup.ps1 to re-authorize in 5 seconds, "
                    "or switch the OAuth app to 'In production' if permitted."
                ) from e
            creds = None

    if not interactive:
        if not secret_path.is_file():
            raise GoogleAuthError(
                f"Missing Google OAuth Desktop client file: {secret_path}. "
                "Download your Desktop App OAuth client JSON from Google Cloud Console, save it there, "
                "and run auth_setup.ps1."
            )
        raise GoogleAuthError(
            f"Google OAuth token not authorized yet ({token_path}). Run auth_setup.ps1 once to sign in."
        )

    if not secret_path.is_file():
        raise GoogleAuthError(
            f"Missing Google OAuth Desktop client file: {secret_path}.\n"
            "Download the OAuth 2.0 Client ID (Application type: Desktop app) JSON from Google Cloud Console "
            "and save it to that exact path first."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=True,
    )
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def auth_status(cfg: dict[str, Any], secrets_dir: Path) -> dict[str, Any]:
    secret_path, token_path = _resolve_oauth_paths(cfg, secrets_dir)
    out: dict[str, Any] = {
        "client_secret_exists": secret_path.is_file(),
        "token_exists": token_path.is_file(),
        "valid": False,
        "email": None,
        "error": None,
    }
    if not secret_path.is_file():
        out["error"] = f"Missing {secret_path.name} in secrets folder"
        return out
    if not token_path.is_file():
        out["error"] = "Not authorized yet (run auth_setup.ps1)"
        return out
    try:
        creds = get_credentials(cfg, secrets_dir, interactive=False)
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        prof = gmail.users().getProfile(userId="me").execute()
        out["valid"] = True
        out["email"] = prof.get("emailAddress")
    except Exception as e:
        out["error"] = str(e)
    return out


# ------------------------------------------------------------------ Calendar
def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def fetch_calendars(
    creds: Credentials,
    cfg: dict[str, Any],
    start_iso: str,
    week_end_iso: str,
    end28_iso: str,
) -> dict[str, Any]:
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    cal_cfg = cfg.get("calendars") or {}
    cals = cal_cfg.get("include") or [{"id": "primary", "label": "Primary"}]
    exclude_res = [re.compile(pat, re.I) for pat in (cal_cfg.get("exclude_title_regex") or [])]
    skip_private = bool(cal_cfg.get("exclude_private_visibility", True))

    target_date = datetime.strptime(start_iso[:10], "%Y-%m-%d").date()

    def _query(time_min: str, time_max: str, max_results: int, detailed: bool) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str]] = set()
        out: list[dict[str, Any]] = []
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
                    maxResults=max_results,
                ).execute()
            except Exception as e:
                out.append({"calendar": cal_label, "error": f"Calendar fetch failed: {e}"})
                continue

            for ev in res.get("items") or []:
                if ev.get("status") == "cancelled":
                    continue
                if skip_private and (ev.get("visibility") or "").lower() in ("private", "confidential"):
                    continue
                summary = (ev.get("summary") or "").strip()
                if not summary:
                    continue
                if any(rx.search(summary) for rx in exclude_res):
                    continue
                start_obj = ev.get("start") or {}
                end_obj = ev.get("end") or {}
                start_val = start_obj.get("dateTime") or start_obj.get("date") or ""
                end_val = end_obj.get("dateTime") or end_obj.get("date") or ""
                dedupe_key = (start_val[:16], end_val[:16], summary.lower())
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                item: dict[str, Any] = {
                    "calendar": cal_label,
                    "summary": summary,
                    "start": start_val,
                    "end": end_val,
                    "all_day": "date" in start_obj and "dateTime" not in start_obj,
                }
                if re.match(r"^\d{4}-\d{2}-\d{2}", start_val):
                    try:
                        ev_d = datetime.strptime(start_val[:10], "%Y-%m-%d").date()
                        delta = (ev_d - target_date).days
                        item["weekday"] = ev_d.strftime("%a").upper()[:3]
                        item["date_short"] = f"{ev_d.day} {ev_d.strftime('%b')}"
                        item["when"] = "Today" if delta == 0 else ("Tomorrow" if delta == 1 else f"In {delta} days")
                    except ValueError:
                        pass
                loc = (ev.get("location") or "").strip()
                if loc:
                    item["location"] = loc[:180]
                if detailed:
                    desc = _strip_html(ev.get("description") or "")
                    if desc:
                        item["description"] = desc[:600]
                out.append(item)
        out.sort(key=lambda x: str(x.get("start") or "9999"))
        return out

    return {
        "week_detail": _query(start_iso, week_end_iso, max_results=80, detailed=True),
        "horizon_28d": _query(start_iso, end28_iso, max_results=140, detailed=False),
    }


# ------------------------------------------------------------------ Gmail
def _decode_b64url(data: str) -> str:
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_message_text(payload: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def _walk(part: dict[str, Any]) -> None:
        mime = (part.get("mimeType") or "").lower()
        body_data = (part.get("body") or {}).get("data") or ""
        if mime == "text/plain" and body_data:
            plain_parts.append(_decode_b64url(body_data))
        elif mime == "text/html" and body_data:
            html_parts.append(_decode_b64url(body_data))
        for child in part.get("parts") or []:
            _walk(child)

    _walk(payload or {})
    if plain_parts:
        raw = "\n".join(plain_parts)
    elif html_parts:
        raw = _strip_html("\n".join(html_parts))
    else:
        raw = ""
    # Trim excessive blank lines and legal footers
    raw = re.sub(r"\r\n?", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return raw


def _header(headers: list[dict[str, str]], name: str) -> str:
    target = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == target:
            return (h.get("value") or "").strip()
    return ""


def fetch_gmail_bundles(creds: Credentials, cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    bundles: dict[str, list[dict[str, Any]]] = {}

    for qspec in cfg.get("gmail_queries") or []:
        name = qspec.get("name") or "messages"
        q = qspec.get("query") or ""
        max_n = int(qspec.get("max_results") or 8)
        include_replies = bool(qspec.get("include_thread_replies", True))
        if not q:
            continue
        items: list[dict[str, Any]] = []
        seen_threads: set[str] = set()
        try:
            listed = service.users().messages().list(userId="me", q=q, maxResults=max_n * 2).execute()
        except Exception as e:
            bundles[name] = [{"error": f"Gmail query failed: {e}"}]
            continue

        for stub in listed.get("messages") or []:
            tid = stub.get("threadId") or stub.get("id")
            if not tid or tid in seen_threads:
                continue
            seen_threads.add(tid)
            try:
                if include_replies:
                    th = service.users().threads().get(userId="me", id=tid, format="full").execute()
                    msgs = th.get("messages") or []
                else:
                    msg = service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
                    msgs = [msg]
            except Exception:
                continue
            if not msgs:
                continue

            thread_msgs = []
            for m in msgs[-4:]:  # keep up to last 4 messages in thread (captures replies like "paid")
                payload = m.get("payload") or {}
                hdrs = payload.get("headers") or []
                body = _extract_message_text(payload)
                thread_msgs.append({
                    "from": _header(hdrs, "From")[:160],
                    "date": _header(hdrs, "Date")[:60],
                    "subject": _header(hdrs, "Subject")[:200],
                    "body": body[:2400],
                })
            items.append({
                "thread_id": tid,
                "subject": thread_msgs[0]["subject"],
                "message_count": len(msgs),
                "messages": thread_msgs,
            })
            if len(items) >= max_n:
                break
        bundles[name] = items
    return bundles


# ------------------------------------------------------------------ Google Sheets Memory
def read_memory_sheet(creds: Credentials, cfg: dict[str, Any], target_iso: str) -> dict[str, Any]:
    mem_cfg = cfg.get("memory_sheet") or {}
    sheet_id = (mem_cfg.get("spreadsheet_id") or "").strip()
    if not sheet_id or sheet_id.startswith("REPLACE_"):
        return {"excluded_permanent": [], "excluded_recent_14d": [], "raw_history_rows": 0, "raw_suggestion_rows": 0}

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    history_range = mem_cfg.get("history_range") or "'Suggestions & History'!A1:E100"
    sugg_range = mem_cfg.get("suggestions_range") or "'Suggestions'!A1:C200"
    cooldown_days = int(mem_cfg.get("repeat_cooldown_days") or 14)

    target_dt = datetime.strptime(target_iso, "%Y-%m-%d").date()
    cutoff_dt = target_dt - timedelta(days=cooldown_days)

    excluded_permanent: list[dict[str, str]] = []
    excluded_recent: list[dict[str, str]] = []
    hist_rows: list[list[str]] = []
    sugg_rows: list[list[str]] = []

    try:
        res_h = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=history_range).execute()
        hist_rows = res_h.get("values") or []
    except Exception:
        hist_rows = []

    for row in hist_rows[1:]:
        if not row:
            continue
        title = (row[0] if len(row) > 0 else "").strip()
        status = " ".join(str(c) for c in row[1:]).lower()
        if title and any(tok in status for tok in ("attended", "skip", "visited", "done")):
            excluded_permanent.append({"title": title, "status": status[:80]})

    try:
        res_s = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=sugg_range).execute()
        sugg_rows = res_s.get("values") or []
    except Exception:
        sugg_rows = []

    for row in sugg_rows[1:]:
        if not row:
            continue
        title = (row[0] if len(row) > 0 else "").strip()
        where = (row[1] if len(row) > 1 else "").strip()
        date_str = (row[2] if len(row) > 2 else "").strip()
        if not title:
            continue
        m_date = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", date_str)
        if m_date:
            try:
                d = datetime.strptime(m_date.group(1), "%Y-%m-%d").date()
                if cutoff_dt <= d <= target_dt:
                    excluded_recent.append({"title": title, "where": where, "date": m_date.group(1)})
            except ValueError:
                pass

    return {
        "excluded_permanent": excluded_permanent,
        "excluded_recent_14d": excluded_recent,
        "raw_history_rows": len(hist_rows),
        "raw_suggestion_rows": len(sugg_rows),
    }


def append_memory_suggestions(
    creds: Credentials,
    cfg: dict[str, Any],
    town_items: list[dict[str, Any]],
    file_date: str,
) -> dict[str, Any]:
    mem_cfg = cfg.get("memory_sheet") or {}
    sheet_id = (mem_cfg.get("spreadsheet_id") or "").strip()
    if not sheet_id or sheet_id.startswith("REPLACE_") or not town_items:
        return {"appended": 0, "skipped": True}

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sugg_range = mem_cfg.get("suggestions_range") or "'Suggestions'!A1:C200"
    append_range = mem_cfg.get("append_range") or "'Suggestions'!A1"

    # Idempotency check: skip titles already logged for file_date
    existing_today: set[str] = set()
    try:
        res = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=sugg_range).execute()
        for row in (res.get("values") or [])[1:]:
            if len(row) >= 3 and str(row[2]).strip() == file_date:
                existing_today.add(str(row[0]).strip().lower())
    except Exception:
        pass

    new_rows = []
    for item in town_items:
        title = str(item.get("title") or "").strip()
        where = str(item.get("where") or "").strip()
        if not title or title.lower() in existing_today:
            continue
        new_rows.append([title, where, file_date])

    if not new_rows:
        return {"appended": 0, "already_logged": True}

    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=append_range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": new_rows},
    ).execute()
    return {"appended": len(new_rows), "rows": new_rows}


# ------------------------------------------------------------------ Gmail Draft
def _markdown_body_to_html(md_text: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", md_text.strip()) if p.strip()]
    html_paras = []
    for p in paras:
        esc = html_lib.escape(p)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        esc = esc.replace("\n", "<br>")
        html_paras.append(f'<p style="margin:0 0 12px 0;line-height:1.5;color:#1F1F1C;">{esc}</p>')
    return (
        '<!DOCTYPE html><html><body style="font-family:Georgia,serif;font-size:15px;'
        'background:#FBF8F1;padding:20px;color:#1F1F1C;">'
        '<div style="max-width:600px;margin:0 auto;background:#FFFFFF;border-top:4px solid #1E4D3B;'
        'padding:20px 24px;border-radius:2px;">'
        + "".join(html_paras)
        + "</div></body></html>"
    )


def build_draft_mime(
    to_list: list[str],
    subject: str,
    markdown_body: str,
    pdf_path: Path,
) -> bytes:
    msg = MIMEMultipart("mixed")
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    plain = re.sub(r"\*\*(.+?)\*\*", r"\1", markdown_body).strip()
    alt.attach(MIMEText(plain, "plain", "utf-8"))
    alt.attach(MIMEText(_markdown_body_to_html(markdown_body), "html", "utf-8"))
    msg.attach(alt)

    pdf_bytes = Path(pdf_path).read_bytes()
    att = MIMEBase("application", "pdf")
    att.set_payload(pdf_bytes)
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment", filename=Path(pdf_path).name)
    msg.attach(att)
    return msg.as_bytes()


def create_or_update_gmail_draft(
    creds: Credentials,
    to_list: list[str],
    subject: str,
    markdown_body: str,
    pdf_path: Path,
) -> dict[str, Any]:
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    raw_bytes = build_draft_mime(to_list, subject, markdown_body, pdf_path)
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii")

    # Idempotency: update existing draft with the exact same subject if one already exists
    existing_draft_id: str | None = None
    try:
        drafts = service.users().drafts().list(userId="me", maxResults=25).execute().get("drafts") or []
        for d in drafts:
            did = d.get("id")
            if not did:
                continue
            detail = service.users().drafts().get(
                userId="me", id=did, format="metadata", metadataHeaders=["Subject"]
            ).execute()
            hdrs = ((detail.get("message") or {}).get("payload") or {}).get("headers") or []
            if _header(hdrs, "Subject") == subject:
                existing_draft_id = did
                break
    except Exception:
        existing_draft_id = None

    body = {"message": {"raw": raw_b64}}
    if existing_draft_id:
        res = service.users().drafts().update(userId="me", id=existing_draft_id, body=body).execute()
        return {"action": "updated", "draft_id": res.get("id"), "subject": subject, "to": to_list}
    res = service.users().drafts().create(userId="me", body=body).execute()
    return {"action": "created", "draft_id": res.get("id"), "subject": subject, "to": to_list}
