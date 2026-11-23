"""Sanitized export & 1-command restore of Open WebUI's setup (`webui.db` models + non-secret Admin config).

Why: `data\\webui.db` itself is never committed to Git because it holds private chats, user accounts, and tokens.
Without exporting the non-private setup tables, resetting the PC would lose all 31 model definitions (friendly names,
emojis, hidden flags, native function-calling params, vision/image capabilities), `ui.model_order_list`, MCP tool
servers (`Browser Use`, `Browser Use (headless)`, `Desktop Use`), `🛠️ Coding Agents` connection metadata, and
`images.edit` / `audio.tts` / `web.search` settings.

Usage:
  python -I setup_backup.py export                  # writes sanitized setup-snapshot.json next to this script
  python -I setup_backup.py restore [--dry-run]     # restores models + config into data\\webui.db (stop Open WebUI first)
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "data" / "webui.db"
DEFAULT_SNAPSHOT = HERE / "setup-snapshot.json"
OPEN_TERMINAL_KEY_FILE = Path.home() / "AI" / "secrets" / "open_terminal_api_key.txt"
OPEN_TERMINAL_PLACEHOLDER = "__FROM_OPEN_TERMINAL_SECRET_FILE__"
REDACTED_SECRET_PLACEHOLDER = "__REDACTED_SECRET__"

# Config prefixes safe & useful to back up (excludes auth.admin.email, oauth, ldap, webhooks)
EXPORTED_CONFIG_PREFIXES = (
    "openai.",
    "ollama.",
    "direct.",
    "models.",
    "ui.default_models",
    "ui.default_pinned_models",
    "ui.model_order_list",
    "ui.default_locale",
    "ui.default_interface_settings",
    "ui.banners",
    "ui.prompt_suggestions",
    "ui.enable_signup",
    "ui.enable_community_sharing",
    "ui.enable_message_rating",
    "tool_server.connections",
    "terminal_server.connections",
    "image_generation.",
    "images.",
    "audio.tts.",
    "audio.stt.",
    "web.search.enable",
    "web.search.engine",
    "web.search.external_web_search_url",
    "web.search.external_web_search_api_key",
    "web.search.result_count",
    "task.",
    "chat.context_compaction.",
    "chat.tool_permissions.enable",
    "chat.tool_call_labels.enable",
    "evaluation.arena.enable",
    "user.permissions",
)

SECRET_KEY_NAME_RE = re.compile(
    r"(?:^|[._-])("
    r"api_keys?|keys?|secrets?|passwords?|tokens?|client_secrets?|access_keys?|private_keys?|"
    r"authorization|credentials?|cookie|sk"
    r")(?:$|[._-])",
    re.I,
)
SECRET_VALUE_RE = re.compile(
    r"(?:^\s*(?:Bearer|Basic)\s+\S+|"
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|tvly-[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{20,}|"
    r"ghp_[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"ya29\.[A-Za-z0-9_-]{16,}|GOCSPX-[A-Za-z0-9_-]{10,}|xox[baprs]-[A-Za-z0-9-]{10,})\b)",
    re.I,
)


def _load_open_terminal_secret() -> str:
    if OPEN_TERMINAL_KEY_FILE.is_file():
        return OPEN_TERMINAL_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def _normalize_key_name(key: str) -> str:
    """Split camelCase/PascalCase boundaries so apiKey/clientSecret match SECRET_KEY_NAME_RE."""
    if not key:
        return ""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)


def _scrub_value(obj, ot_secret: str, parent_key: str = ""):
    if isinstance(obj, dict):
        return {k: _scrub_value(v, ot_secret, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_value(v, ot_secret, parent_key) for v in obj]
    if isinstance(obj, str):
        if ot_secret and obj == ot_secret:
            return OPEN_TERMINAL_PLACEHOLDER
        if ot_secret and ot_secret in obj:
            return obj.replace(ot_secret, OPEN_TERMINAL_PLACEHOLDER)
        if SECRET_VALUE_RE.search(obj):
            return REDACTED_SECRET_PLACEHOLDER
        if SECRET_KEY_NAME_RE.search(_normalize_key_name(parent_key or "")) and obj not in (
            "",
            "local",
            "none",
            "0",
            OPEN_TERMINAL_PLACEHOLDER,
            REDACTED_SECRET_PLACEHOLDER,
        ):
            return REDACTED_SECRET_PLACEHOLDER
    return obj


def _unscrub_value(obj, ot_secret: str):
    if isinstance(obj, dict):
        return {k: _unscrub_value(v, ot_secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_unscrub_value(v, ot_secret) for v in obj]
    if isinstance(obj, str):
        if obj == OPEN_TERMINAL_PLACEHOLDER:
            return ot_secret
        if OPEN_TERMINAL_PLACEHOLDER in obj:
            return obj.replace(OPEN_TERMINAL_PLACEHOLDER, ot_secret)
        if obj == REDACTED_SECRET_PLACEHOLDER:
            return ""
    return obj


def export_setup(db_path: Path, out_path: Path) -> int:
    if not db_path.is_file():
        print(f"setup_backup: database not found at {db_path}", file=sys.stderr)
        return 1
    ot_secret = _load_open_terminal_secret()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cfg_rows = con.execute("SELECT key, value FROM config ORDER BY key").fetchall()
        config_out = {}
        for k, raw_val in cfg_rows:
            if not any(k == p or k.startswith(p) for p in EXPORTED_CONFIG_PREFIXES):
                continue
            try:
                parsed = json.loads(raw_val)
            except Exception:
                parsed = raw_val
            config_out[k] = _scrub_value(parsed, ot_secret, k)

        model_rows = con.execute(
            "SELECT id, base_model_id, name, params, meta, is_active FROM model ORDER BY id"
        ).fetchall()
        models_out = []
        for mid, base_id, name, params_raw, meta_raw, is_active in model_rows:
            try:
                params = json.loads(params_raw) if isinstance(params_raw, str) and params_raw else (params_raw or {})
            except Exception:
                params = {}
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) and meta_raw else (meta_raw or {})
            except Exception:
                meta = {}
            # Strip any uploaded local user avatar hashes that wouldn't exist on a fresh install unless built-in
            models_out.append({
                "id": mid,
                "base_model_id": base_id,
                "name": name,
                "params": _scrub_value(params, ot_secret, "params"),
                "meta": _scrub_value(meta, ot_secret, "meta"),
                "is_active": int(bool(is_active)),
            })
    finally:
        con.close()

    snapshot = {
        "schema_version": 1,
        "notes": (
            "Sanitized Open WebUI setup snapshot (models + non-secret Admin config). "
            "Restore on a fresh install: stop Open WebUI, run `python -I setup_backup.py restore`, start Open WebUI."
        ),
        "models": models_out,
        "config": config_out,
    }
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"setup_backup: exported {len(models_out)} models and {len(config_out)} config keys -> {out_path}")
    return 0


def restore_setup(db_path: Path, snap_path: Path, dry_run: bool = False) -> int:
    if not snap_path.is_file():
        print(f"setup_backup: snapshot not found at {snap_path}", file=sys.stderr)
        return 1
    if not db_path.is_file():
        print(
            f"setup_backup: {db_path} does not exist yet — start Open WebUI once, create your admin account, "
            "stop Open WebUI, then run `restore`.",
            file=sys.stderr,
        )
        return 1
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    ot_secret = _load_open_terminal_secret()
    models = snap.get("models") or []
    config = snap.get("config") or {}
    if dry_run:
        print(f"[dry-run] Would restore {len(models)} models and {len(config)} config keys into {db_path}")
        return 0

    now_ts = int(time.time())
    con = sqlite3.connect(str(db_path))
    try:
        admin_row = con.execute("SELECT id FROM user WHERE role = 'admin' ORDER BY created_at LIMIT 1").fetchone()
        owner_id = admin_row[0] if admin_row else "system"

        for k, val in config.items():
            restored_val = _unscrub_value(val, ot_secret)
            # Skip overwriting existing secrets with empty string if REDACTED_SECRET_PLACEHOLDER was used
            if restored_val == "" and val == REDACTED_SECRET_PLACEHOLDER:
                continue
            val_json = json.dumps(restored_val, ensure_ascii=False)
            con.execute(
                "INSERT INTO config (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (k, val_json),
            )

        model_cols = {r[1] for r in con.execute("PRAGMA table_info(model)").fetchall()}
        has_version_id = "version_id" in model_cols
        for m in models:
            params_json = json.dumps(_unscrub_value(m.get("params") or {}, ot_secret), ensure_ascii=False)
            meta_json = json.dumps(_unscrub_value(m.get("meta") or {}, ot_secret), ensure_ascii=False)
            if has_version_id:
                con.execute(
                    "INSERT INTO model (id, user_id, base_model_id, name, params, meta, updated_at, created_at, is_active, version_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                    "ON CONFLICT(id) DO UPDATE SET base_model_id = excluded.base_model_id, name = excluded.name, "
                    "params = excluded.params, meta = excluded.meta, updated_at = excluded.updated_at, is_active = excluded.is_active",
                    (m["id"], owner_id, m.get("base_model_id"), m["name"], params_json, meta_json, now_ts, now_ts, int(m.get("is_active", 1))),
                )
            else:
                con.execute(
                    "INSERT INTO model (id, user_id, base_model_id, name, params, meta, updated_at, created_at, is_active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET base_model_id = excluded.base_model_id, name = excluded.name, "
                    "params = excluded.params, meta = excluded.meta, updated_at = excluded.updated_at, is_active = excluded.is_active",
                    (m["id"], owner_id, m.get("base_model_id"), m["name"], params_json, meta_json, now_ts, now_ts, int(m.get("is_active", 1))),
                )
        con.commit()
    finally:
        con.close()
    print(f"setup_backup: restored {len(models)} models and {len(config)} config keys into {db_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Export or restore sanitized Open WebUI setup.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_exp = sub.add_parser("export")
    p_exp.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_exp.add_argument("--out", type=Path, default=DEFAULT_SNAPSHOT)
    p_res = sub.add_parser("restore")
    p_res.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_res.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    p_res.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cmd == "export":
        return export_setup(args.db, args.out)
    return restore_setup(args.db, args.snapshot, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
