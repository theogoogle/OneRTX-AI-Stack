"""Friendly names, 1-click dropdown visibility, and native Auto -> Open Code dispatch in Open WebUI.

Patches (idempotent; re-applied on every Open WebUI start by AI\\open-webui\\run.ps1):
1. open_webui/utils/agents/discovery.py:
   - Renames Open Terminal agent models to '🧑‍💻 Open Code (Agent)' and '🤖 Claude Code (Agent)' with clear descriptions.
2. open_webui/main.py:
   - Sorts coding agents right below '💻 Coding / Planning' ('think') in /api/models.
   - Auto-resolves the user's default terminal connection in /api/models and /api/chat/completions when terminal_id is
     omitted, so agents are always 1-click selectable without ticking '🛠️ Coding Agents' first.
   - When '💡 Smart' ('auto') is selected in a saved chat, checks parent agent session state or asks the local router
     (POST http://127.0.0.1:4000/route-check) and transparently dispatches multi-file / CLI / test-verified builds to
     '🧑‍💻 Open Code (Agent)' with native tool cards, bash approvals, and relabeled message headers.
3. open_webui/utils/tools.py, open_webui/utils/middleware.py & open_webui/tools/builtin.py:
   - Exposes built-in `generate_image` (and `edit_image` when enabled) to native function-calling models whenever
     `image_generation.enable` is on, even when the user did not manually toggle `+` -> `Image` in the composer.
   - Adds a system hint when `+` -> `Image` IS toggled on in native FC mode so bare visual noun phrases still call
     `generate_image`.
   - Merges server-side `model.info.meta.toolIds` into `tool_ids` inside `process_chat_payload` so stale browser tabs
     never drop newly attached default model tools.
   - Coerces stringified `image_urls` arguments (e.g. JSON-encoded list strings from llama.cpp/Gemma) in `edit_image`
     into a flat `list[str]` before resolving URLs/files.
4. open_webui/frontend/_app/immutable/chunks (ResponseMessage toolbar):
   - Renders `response_token/s` and `generation_time_s` (or token count fallback) as an always-visible inline badge
     next to the `ⓘ` Info icon below every assistant message (desktop & mobile), and enables click/tap opening of the
     full token-usage popup on touch devices.
"""
import importlib.util
import inspect
import sys
from pathlib import Path

MARKER = "# local patch: friendly agent names"
TARGET = "            'name': f'{agent[\"id\"]}/{model[\"id\"]}',\n"
REPLACEMENT = "            'name': _display_name(agent, model),\n"
META_TARGET = "                'meta': {\n                    'capabilities': {\n"
META_REPLACEMENT = (
    "                'meta': {\n"
    "                    'description': AGENT_DESCRIPTIONS.get(agent.get('id')),\n"
    "                    'capabilities': {\n"
)
DESC_LINE = (
    "Autonomous coding agent: edits files in AI\\projects, runs commands & 3D scene tool "
    "(Qwen 3.8 27B + Pro 256K overflow, 8K think cap)"
)
HELPER = f'''
{MARKER} (AI\\open-webui\\patch_agent_names.py)
AGENT_DISPLAY_NAMES = {{'opencode': '\U0001f9d1\u200d\U0001f4bb Open Code (Agent)', 'claude-code': '\U0001f916 Claude Code (Agent)'}}
AGENT_DESCRIPTIONS = {{
    'opencode': {DESC_LINE!r},
    'claude-code': {DESC_LINE!r},
}}


def _display_name(agent, model):
    name = AGENT_DISPLAY_NAMES.get(agent.get('id'))
    if not name:
        return f'{{agent["id"]}}/{{model["id"]}}'
    return name if len(agent.get('models') or []) <= 1 else f'{{name}} ({{model["id"]}})'

'''

ORDER_MARKER = "# local patch: agent models after Plan"
ORDER_TARGET = (
    "            log.debug('Agent discovery unavailable: %s', type(exc).__name__)\n"
    "    return {'data': models}\n"
)
ORDER_REPLACEMENT = (
    "            log.debug('Agent discovery unavailable: %s', type(exc).__name__)\n"
    f"        {ORDER_MARKER} (AI\\\\open-webui\\\\patch_agent_names.py)\n"
    "        _order = {model_id: i for i, model_id in enumerate(model_order_list or [])}\n"
    "        _after = _order.get('think', _order.get('deep', len(_order)))\n"
    "        _agent_rank = {'opencode': _after + 0.1, 'claude-code': _after + 0.2}\n"
    "        models.sort(\n"
    "            key=lambda model: (\n"
    "                _order.get(model.get('id', ''),\n"
    "                           _agent_rank.get((model.get('agent') or {}).get('provider'), float('inf'))),\n"
    "                (model.get('name', '') or ''),\n"
    "            )\n"
    "        )\n"
    "    return {'data': models}\n"
)

AUTO_HELPER_MARKER = "# local patch: smart auto -> opencode + 1-click agents"
AUTO_HELPER_ANCHOR = "\n@app.get('/api/models')\n"
AUTO_HELPER_BLOCK = f'''
{AUTO_HELPER_MARKER} (AI\\open-webui\\patch_agent_names.py)
async def _default_terminal_id_for_user(user):
    try:
        from open_webui.models.users import UserModel
        from open_webui.utils.tether import get_terminal_connections
        u = user if isinstance(user, UserModel) else UserModel(**user)
        conns = await get_terminal_connections(u)
        return next((c.get('id') for c in (conns or []) if c.get('enabled', True) and c.get('id')), None)
    except Exception:
        return None


def _router_route_check_sync(messages):
    import http.client
    import json as _json
    conn = http.client.HTTPConnection('127.0.0.1', 4000, timeout=35)
    try:
        conn.request(
            'POST',
            '/route-check',
            _json.dumps({{'messages': messages or []}}).encode('utf-8'),
            {{'Content-Type': 'application/json'}},
        )
        resp = conn.getresponse()
        raw = resp.read()
        return _json.loads(raw) if resp.status == 200 else {{}}
    except Exception:
        return {{}}
    finally:
        conn.close()


async def _prepare_agent_or_auto_dispatch(request, user, form_data, model_id, requested_directory):
    import asyncio as _asyncio
    from urllib.parse import unquote as _unquote
    if isinstance(model_id, str) and model_id.startswith('agent:'):
        parts = model_id.split(':', 3)
        if len(parts) == 4 and not form_data.get('terminal_id'):
            form_data['terminal_id'] = _unquote(parts[1])
        return model_id
    if model_id != 'auto':
        return model_id
    if (
        not form_data.get('session_id')
        or getattr(request.state, 'internal', False)
        or form_data.get('automation_id')
        or form_data.get('regeneration_message_id')
        or form_data.get('assistant_message_id')
    ):
        return model_id
    mids = form_data.get('message_ids')
    if isinstance(mids, (list, dict)) and len(mids) != 1:
        return model_id
    cid = form_data.get('chat_id')
    is_new = 'parent_id' in form_data and form_data['parent_id'] is None and not cid
    if not is_new and (not is_saved_chat_id(cid) or is_temporary_chat_id(cid)):
        return model_id
    try:
        from open_webui.utils.agents.discovery import get_models as get_agent_models
        from open_webui.utils.auth import decode_token as _decode_token
        tid = form_data.get('terminal_id')
        want_opencode = False
        resume_model = None
        if not is_new and cid:
            umsg = form_data.get('user_message') or form_data.get('parent_message') or {{}}
            pid = umsg.get('parentId') or form_data.get('parent_id')
            if pid:
                prev_msg = await Chats.get_message_by_id_and_message_id(cid, pid)
                tok = ((prev_msg or {{}}).get('meta') or {{}}).get('agent')
                dec = _decode_token(tok) if isinstance(tok, str) else None
                if dec and dec.get('purpose') == 'terminal-agent':
                    if dec.get('provider') == 'opencode':
                        want_opencode = True
                        tid = dec.get('terminal_id') or tid
                        resume_model = dec.get('model')
                    else:
                        return model_id
        if not want_opencode:
            rc = await _asyncio.to_thread(_router_route_check_sync, form_data.get('messages') or [])
            want_opencode = rc.get('dispatch') == 'opencode'
        if not want_opencode:
            return model_id
        if not tid:
            tid = await _default_terminal_id_for_user(user)
        if not tid:
            return model_id
        probe = {{**form_data, 'terminal_id': tid}}
        cands = await get_agent_models(request, user, probe, provider='opencode', cwd=requested_directory)
        chosen = (
            next((c for c in cands if (c.get('agent') or {{}}).get('model') == resume_model), None)
            if resume_model
            else None
        ) or (cands[0] if cands else None)
        if not chosen:
            return model_id
        new_id = chosen['id']
        form_data['terminal_id'] = tid
        form_data['model'] = new_id
        request.state.model_id = new_id
        if isinstance(mids, list) and len(mids) == 1 and isinstance(mids[0], dict):
            mids[0]['model_id'] = new_id
        elif isinstance(mids, dict) and len(mids) == 1:
            only_val = next(iter(mids.values()))
            form_data['message_ids'] = [{{'model_id': new_id, 'message_id': only_val}}]
        return new_id
    except Exception as exc:
        log.debug('Smart auto -> opencode dispatch skipped: %s', exc)
        return model_id
'''

MODELS_TID_MARKER = "# local patch: 1-click agent dropdown without ticking terminal first"
MODELS_TID_TARGET = (
    "    if terminal_id:\n"
    "        from open_webui.utils.agents.discovery import get_models as get_agent_models\n"
)
MODELS_TID_REPLACEMENT = (
    f"    {MODELS_TID_MARKER}\n"
    "    if not terminal_id:\n"
    "        terminal_id = await _default_terminal_id_for_user(user)\n"
    "    if terminal_id:\n"
    "        from open_webui.utils.agents.discovery import get_models as get_agent_models\n"
)

CHAT_DISPATCH_MARKER = "# local patch: auto -> opencode dispatch & 1-click agent terminal_id"
CHAT_DISPATCH_TARGET = (
    "    agent_model = None\n"
    "    if isinstance(model_id, str) and model_id.startswith('agent:'):\n"
)
CHAT_DISPATCH_REPLACEMENT = (
    f"    {CHAT_DISPATCH_MARKER}\n"
    "    model_id = await _prepare_agent_or_auto_dispatch(request, user, form_data, model_id, requested_directory)\n"
    "    agent_model = None\n"
    "    if isinstance(model_id, str) and model_id.startswith('agent:'):\n"
)

UPSERT_MODEL_TARGET = "                        **({'modelName': agent_model['name']} if agent_model else {}),\n"
UPSERT_MODEL_REPLACEMENT = (
    "                        **({'model': target_model_id, 'modelName': agent_model['name']} if agent_model else {}),\n"
)


def patch_main(pkg: Path) -> None:
    path = pkg / "main.py"
    raw = path.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    src = raw.replace("\r\n", "\n")
    changed = []

    if ORDER_MARKER not in src:
        if src.count(ORDER_TARGET) == 1 and "model_order_list = await Config.get('ui.model_order_list')" in src:
            src = src.replace(ORDER_TARGET, ORDER_REPLACEMENT, 1)
            changed.append("order")
        else:
            print("agent order: Open WebUI code changed, patch skipped")
    else:
        old_line = "        _after = _order.get('deep', len(_order))\n"
        new_line = "        _after = _order.get('think', _order.get('deep', len(_order)))\n"
        if old_line in src:
            src = src.replace(old_line, new_line, 1)
            changed.append("order-rank")

    if AUTO_HELPER_MARKER not in src:
        if src.count(AUTO_HELPER_ANCHOR) == 1:
            src = src.replace(AUTO_HELPER_ANCHOR, AUTO_HELPER_BLOCK + AUTO_HELPER_ANCHOR, 1)
            changed.append("auto-helper")
        else:
            print("auto helper: anchor not found, patch skipped")
    else:
        for old_t in ("HTTPConnection('127.0.0.1', 4000, timeout=4)", "HTTPConnection('127.0.0.1', 4000, timeout=15)"):
            if old_t in src:
                src = src.replace(old_t, "HTTPConnection('127.0.0.1', 4000, timeout=35)", 1)
                changed.append("auto-helper-timeout")
                break

    if MODELS_TID_MARKER not in src and AUTO_HELPER_MARKER in src:
        if src.count(MODELS_TID_TARGET) == 1:
            src = src.replace(MODELS_TID_TARGET, MODELS_TID_REPLACEMENT, 1)
            changed.append("1-click-models")
        else:
            print("1-click models: target not found, patch skipped")

    if CHAT_DISPATCH_MARKER not in src and AUTO_HELPER_MARKER in src:
        if src.count(CHAT_DISPATCH_TARGET) == 1:
            src = src.replace(CHAT_DISPATCH_TARGET, CHAT_DISPATCH_REPLACEMENT, 1)
            changed.append("auto-dispatch")
        else:
            print("auto dispatch: target not found, patch skipped")

    if UPSERT_MODEL_REPLACEMENT not in src and src.count(UPSERT_MODEL_TARGET) == 1:
        src = src.replace(UPSERT_MODEL_TARGET, UPSERT_MODEL_REPLACEMENT, 1)
        changed.append("message-model-sync")

    if changed:
        path.write_text(src.replace("\n", nl), encoding="utf-8")
        print(f"main.py: patched ({', '.join(changed)})")
    else:
        print("main.py: already patched")


def patch_discovery(pkg: Path) -> None:
    path = pkg / "utils" / "agents" / "discovery.py"
    raw = path.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    src = raw.replace("\r\n", "\n")

    old_desc = "'Autonomous coding agent: edits files in AI\\projects, runs commands & 3D scene tool (Qwen 3.8 27B, 64K)'"
    new_desc = repr(DESC_LINE)

    if MARKER in src:
        updated = False
        if old_desc in src:
            src = src.replace(old_desc, new_desc)
            updated = True
        if META_TARGET in src and "'description': AGENT_DESCRIPTIONS" not in src:
            src = src.replace(META_TARGET, META_REPLACEMENT, 1)
            updated = True
        if updated:
            path.write_text(src.replace("\n", nl), encoding="utf-8")
            print("discovery.py: updated descriptions")
        else:
            print("discovery.py: already patched")
        return

    if TARGET not in src or "\nasync def get_models(" not in src:
        print("discovery.py: Open WebUI code changed, patch skipped")
        return

    src = src.replace(TARGET, REPLACEMENT, 1).replace("\nasync def get_models(", HELPER + "\nasync def get_models(", 1)
    if META_TARGET in src:
        src = src.replace(META_TARGET, META_REPLACEMENT, 1)
    path.write_text(src.replace("\n", nl), encoding="utf-8")
    print("discovery.py: patched")


IMG_TOOLS_MARKER = "# local patch: expose generate_image without clicking + -> Image"
IMG_TOOLS_TARGET = (
    "    if (\n"
    "        is_builtin_tool_enabled('image_generation')\n"
    "        and config.get('image_generation.enable')\n"
    "        and get_model_capability('image_generation')\n"
    "        and features.get('image_generation')\n"
    "        and await has_user_permission('image_generation')\n"
    "    ):\n"
    "        builtin_functions.append(generate_image)\n"
    "    if (\n"
    "        is_builtin_tool_enabled('image_generation')\n"
    "        and config.get('images.edit.enable')\n"
    "        and get_model_capability('image_generation')\n"
    "        and features.get('image_generation')\n"
    "        and await has_user_permission('image_generation')\n"
    "    ):\n"
    "        builtin_functions.append(edit_image)\n"
)
IMG_TOOLS_REPLACEMENT = (
    f"    {IMG_TOOLS_MARKER} (AI\\\\open-webui\\\\patch_agent_names.py)\n"
    "    if (\n"
    "        is_builtin_tool_enabled('image_generation')\n"
    "        and config.get('image_generation.enable')\n"
    "        and get_model_capability('image_generation')\n"
    "        and await has_user_permission('image_generation')\n"
    "    ):\n"
    "        builtin_functions.append(generate_image)\n"
    "    if (\n"
    "        is_builtin_tool_enabled('image_generation')\n"
    "        and config.get('images.edit.enable')\n"
    "        and get_model_capability('image_generation')\n"
    "        and await has_user_permission('image_generation')\n"
    "    ):\n"
    "        builtin_functions.append(edit_image)\n"
)

IMG_MW_MARKER = "# local patch: hint native FC model when + -> Image switch is on"
IMG_MW_TARGET = (
    "                # Skip forced image generation when native FC is enabled - model can use generate_image tool\n"
    "                if metadata.get('params', {}).get('function_calling') == 'legacy':\n"
    "                    form_data = await chat_image_generation_handler(request, form_data, extra_params, user)\n"
)
IMG_MW_REPLACEMENT = (
    "                # Skip forced image generation when native FC is enabled - model can use generate_image tool\n"
    "                if metadata.get('params', {}).get('function_calling') == 'legacy':\n"
    "                    form_data = await chat_image_generation_handler(request, form_data, extra_params, user)\n"
    "                else:\n"
    f"                    {IMG_MW_MARKER} (AI\\\\open-webui\\\\patch_agent_names.py)\n"
    "                    form_data['messages'] = add_or_update_system_message(\n"
    "                        'Image Generation mode is enabled for this message: call `generate_image` '\n"
    "                        '(or `edit_image` when modifying an image) to fulfill visual prompts.',\n"
    "                        form_data['messages'],\n"
    "                    )\n"
)

MODEL_TIDS_MW_MARKER = "# local patch: merge server-side model default toolIds"
MODEL_TIDS_MW_TARGET = (
    "    form_data['messages'] = set_tool_approval_prompt(form_data['messages'])\n"
    "    tool_ids = form_data.pop('tool_ids', None)\n"
)
MODEL_TIDS_MW_REPLACEMENT = (
    "    form_data['messages'] = set_tool_approval_prompt(form_data['messages'])\n"
    "    tool_ids = form_data.pop('tool_ids', None)\n"
    f"    {MODEL_TIDS_MW_MARKER} (AI\\\\open-webui\\\\patch_agent_names.py)\n"
    "    if payload_tools is None:\n"
    "        _model_tids = ((model.get('info') or {}).get('meta') or {}).get('toolIds') or []\n"
    "        if _model_tids:\n"
    "            tool_ids = list(dict.fromkeys([*(tool_ids or []), *_model_tids]))\n"
)


EDIT_IMG_URLS_MARKER = "# local patch: coerce stringified image_urls in edit_image"
EDIT_IMG_URLS_ANCHOR = "\nasync def edit_image(\n"
EDIT_IMG_URLS_TARGET = "        resolved_images = []\n        for url in image_urls:\n"
EDIT_IMG_URLS_REPLACEMENT = (
    f"        {EDIT_IMG_URLS_MARKER} (AI/open-webui/patch_agent_names.py)\n"
    "        image_urls = _coerce_edit_image_urls(image_urls)\n"
    "        resolved_images = []\n"
    "        for url in image_urls:\n"
)


def _coerce_edit_image_urls(image_urls):
    """Normalize list[str], stringified JSON/Python lists, nested strings, or a bare URL string to list[str]."""
    import ast
    import json

    out = []
    stack = [image_urls]
    steps = 0
    while stack and steps < 64:
        steps += 1
        item = stack.pop(0)
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            stack[0:0] = list(item)
            continue
        s = str(item).strip()
        if not s:
            continue
        parsed = None
        if s[0] in '[{"\'(' or s.lower() == "null":
            for loader in (json.loads, ast.literal_eval):
                try:
                    cand = loader(s)
                except Exception:
                    continue
                if isinstance(cand, (list, tuple, str)) and cand != s:
                    parsed = cand
                    break
        if parsed is not None:
            stack.insert(0, parsed)
            continue
        cleaned = s.strip().strip('"\'').strip()
        if cleaned:
            out.append(cleaned)
    return out


def patch_image_tools(pkg: Path) -> None:
    tpath = pkg / "utils" / "tools.py"
    traw = tpath.read_text(encoding="utf-8")
    tnl = "\r\n" if "\r\n" in traw else "\n"
    tsrc = traw.replace("\r\n", "\n")
    if IMG_TOOLS_MARKER in tsrc:
        print("tools.py: already patched")
    elif tsrc.count(IMG_TOOLS_TARGET) == 1:
        tsrc = tsrc.replace(IMG_TOOLS_TARGET, IMG_TOOLS_REPLACEMENT, 1)
        tpath.write_text(tsrc.replace("\n", tnl), encoding="utf-8")
        print("tools.py: patched (always expose generate_image/edit_image)")
    else:
        print("tools.py: target not found, patch skipped")

    mpath = pkg / "utils" / "middleware.py"
    mraw = mpath.read_text(encoding="utf-8")
    mnl = "\r\n" if "\r\n" in mraw else "\n"
    msrc = mraw.replace("\r\n", "\n")
    mchanged = []
    if IMG_MW_MARKER not in msrc:
        if msrc.count(IMG_MW_TARGET) == 1:
            msrc = msrc.replace(IMG_MW_TARGET, IMG_MW_REPLACEMENT, 1)
            mchanged.append("Image switch hint for native FC")
        else:
            print("middleware.py: Image switch target not found, patch skipped")
    if MODEL_TIDS_MW_MARKER not in msrc:
        if msrc.count(MODEL_TIDS_MW_TARGET) == 1:
            msrc = msrc.replace(MODEL_TIDS_MW_TARGET, MODEL_TIDS_MW_REPLACEMENT, 1)
            mchanged.append("merge server-side model toolIds")
        else:
            print("middleware.py: model toolIds target not found, patch skipped")
    if mchanged:
        mpath.write_text(msrc.replace("\n", mnl), encoding="utf-8")
        print(f"middleware.py: patched ({', '.join(mchanged)})")
    else:
        print("middleware.py: already patched")

    bpath = pkg / "tools" / "builtin.py"
    braw = bpath.read_text(encoding="utf-8")
    bnl = "\r\n" if "\r\n" in braw else "\n"
    bsrc = braw.replace("\r\n", "\n")
    if EDIT_IMG_URLS_MARKER in bsrc:
        print("builtin.py: already patched")
    elif bsrc.count(EDIT_IMG_URLS_ANCHOR) == 1 and bsrc.count(EDIT_IMG_URLS_TARGET) == 1:
        helper_src = inspect.getsource(_coerce_edit_image_urls).rstrip() + "\n\n\n"
        bsrc = bsrc.replace(EDIT_IMG_URLS_ANCHOR, "\n" + helper_src + "async def edit_image(\n", 1)
        bsrc = bsrc.replace(EDIT_IMG_URLS_TARGET, EDIT_IMG_URLS_REPLACEMENT, 1)
        bpath.write_text(bsrc.replace("\n", bnl), encoding="utf-8")
        print("builtin.py: patched (coerce stringified image_urls in edit_image)")
    else:
        print("builtin.py: target not found, patch skipped")


USAGE_BADGE_MARKER = "/* local patch: inline tok/s badge */"
_ID = r"[A-Za-z_$][\w$]*"
USAGE_BADGE_RE_PATTERN = (
    rf'allowHTML:!0,placement:"bottom",children:\((?P<p1>{_ID}),(?P<p2>{_ID})\)=>{{var (?P<el>{_ID})=(?P<clone>{_ID})\(\);'
    rf'(?P<eff>{_ID})\((?P<arg>{_ID})=>{{(?P<attr>{_ID})\((?P=el),"aria-label",(?P=arg)\),'
    rf'(?P=attr)\((?P=el),"aria-describedby",`usage-\${{(?P<get>{_ID})\((?P<msg>{_ID})\),(?P<thunk>{_ID})\(\(\)=>(?P=get)\((?P=msg)\)\.id\)\?\?""}}`\),'
    rf'(?P<cls>{_ID})\((?P=el),1,` \${{(?P<untrack>{_ID})\((?P<mob>{_ID})\(\)\),(?P<cfg>{_ID})\(\),(?P=thunk)\(\(\)=>(?P=mob)\(\)\|\|\((?P=cfg)\(\)\?\.highContrastMode\?\?!1\)\?"visible":"hover-reveal"\)\?\?""}} '
    rf'inline-flex size-7 shrink-0 items-center justify-center p-1\.5 hover:bg-black/5 dark:hover:bg-white/5 '
    rf'rounded-lg dark:hover:text-gray-400 hover:text-gray-600 transition whitespace-pre-wrap`\),'
    rf'(?P=attr)\((?P=el),"id",`info-\${{(?P=get)\((?P=msg)\),(?P=thunk)\(\(\)=>(?P=get)\((?P=msg)\)\.id\)\?\?""}}`\)}},\[(?P<arr>\(\)=>\((?P<i18n>{_ID})\(\),(?P=thunk)\()'
)


def _render_usage_badge_replacement(m, derived_fn: str) -> str:
    g = m.groupdict()
    return (
        f'allowHTML:!0,placement:"bottom",tippyOptions:{{trigger:"mouseenter focusin click",onShow:()=>{{}}}},'
        f'children:({g["p1"]},{g["p2"]})=>{{{USAGE_BADGE_MARKER}'
        f'var {g["el"]}={g["clone"]}(),_sp=document.createElement("span");'
        f'_sp.style.cssText="font-size:11px;line-height:1;letter-spacing:-0.01em;opacity:0.85";'
        f'{g["el"]}.appendChild(_sp);'
        f'let _bdg={derived_fn}(()=>({g["get"]}({g["msg"]}),{g["thunk"]}(()=>{{'
        f'const u={g["get"]}({g["msg"]})?.usage;'
        f'if(!u||typeof u!=="object")return"";'
        f'const tps=Number(u["response_token/s"]??u.tokens_per_second??0),'
        f'gs=Number(u.generation_time_s??0),'
        f'otk=Number(u.completion_tokens??u.output_tokens??0);'
        f'if(tps>0){{'
        f'const rs=Math.round(gs),'
        f'dur=gs>=60?`${{Math.floor(rs/60)}}m${{String(rs%60).padStart(2,"0")}}s`:gs>0?`${{gs.toFixed(1)}}s`:"";'
        f'return dur?`${{tps.toFixed(1)}} tok/s \u00b7 ${{dur}}`:`${{tps.toFixed(1)}} tok/s`;'
        f'}}'
        f'if(otk>0)return otk>=1e3?`${{(otk/1e3).toFixed(1)}}k tok`:`${{Math.round(otk)}} tok`;'
        f'return"";'
        f'}})));'
        f'{g["eff"]}({g["arg"]}=>{{'
        f'const _bt={g["get"]}(_bdg);'
        f'_sp.textContent=_bt;'
        f'_sp.style.display=_bt?"inline":"none";'
        f'{g["attr"]}({g["el"]},"aria-label",_bt?`${{{g["arg"]}}}: ${{_bt}}`:{g["arg"]}),'
        f'{g["attr"]}({g["el"]},"aria-describedby",`usage-${{{g["get"]}({g["msg"]}),{g["thunk"]}(()=>{g["get"]}({g["msg"]}).id)??""}}`),'
        f'{g["cls"]}({g["el"]},1,` ${{_bt?"visible":({g["untrack"]}({g["mob"]}()),{g["cfg"]}(),{g["thunk"]}(()=>{g["mob"]}()||({g["cfg"]}()?.highContrastMode??!1)?"visible":"hover-reveal")??"")}} '
        f'inline-flex ${{_bt?"h-7 px-2 gap-1 font-mono":"size-7 p-1.5"}} shrink-0 items-center justify-center '
        f'hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-gray-400 hover:text-gray-600 '
        f'transition whitespace-pre-wrap`),'
        f'{g["attr"]}({g["el"]},"id",`info-${{{g["get"]}({g["msg"]}),{g["thunk"]}(()=>{g["get"]}({g["msg"]}).id)??""}}`)}},[{g["arr"]}'
    )


IMPORTMAP_MARKER = "<!-- local patch: inline tok/s badge importmap -->"
SMART_LABEL_V1_MARKER = "/* local patch: smart model badge */"
SMART_LABEL_MARKER = "/* local patch: smart model badge v2 */"
SMART_LABEL_END = "/* end smart model badge */"
SMART_ARENA_MARKER = "/* local patch: smart selected_model_id */"
CUSTOM_CSS_MARKER = "/* local patch: rich editorial images & seamless wallpaper fade */"

SMART_LABEL_RE_PATTERN = (
    rf'(?P<eff>{_ID})\(\(\)=>\((?P<models>{_ID})\(\),(?P<get>{_ID})\((?P<msg>{_ID})\)\),'
    rf'\(\)=>{{(?P<set>{_ID})\((?P<mvar>{_ID}),(?P=models)\(\)\.find\((?P<it>{_ID})=>(?P=it)\.id===(?P=get)\((?P=msg)\)\.model\)\)}}\),'
    rf'(?P=eff)\(\(\)=>\((?P=get)\((?P=mvar)\),(?P<i18n>{_ID})\(\),(?P=get)\((?P=msg)\)\),'
    rf'\(\)=>{{(?P=set)\((?P<nvar>{_ID}),(?P=get)\((?P=mvar)\)\?(?P<loc>{_ID})\((?P=get)\((?P=mvar)\),(?P=i18n)\(\)\.language\):'
    rf'(?P=get)\((?P=msg)\)\.modelName\|\|(?P=get)\((?P=msg)\)\.model\)}}\)'
)
SMART_ARENA_RE_PATTERN = rf'(?P<sel>{_ID})&&\((?P<msg>{_ID})\.selectedModelId=(?P=sel),(?P=msg)\.arena=!0\)'

RICH_CUSTOM_CSS = f"""{CUSTOM_CSS_MARKER}
/* 1. Seamless #0d0d0d transition between the dark left sidebar and Chat Background Image */
.dark #sidebar,
.dark [id="sidebar"],
.dark div:has(> #pinned-menu-items-list),
.dark .border-gray-850\\/30 {{
  border-inline-end-color: transparent !important;
  border-right-color: transparent !important;
}}

.dark [data-main-chat-pane]:has(div[style*="background-image"]),
.dark div:has(> .relative > [data-main-chat-pane] div[style*="background-image"]),
.dark div:has(> .relative > [data-main-chat-pane] div[style*="background-image"]) [data-panel-tab-bar] {{
  background-color: var(--color-gray-950, #0d0d0d) !important;
  border-color: transparent !important;
}}

/* Replace Open WebUI's flat 90% gray-900 (#171717) overlay with a smooth left->right #0d0d0d dissolve
   so x=0 matches the #0d0d0d sidebar identically and the right-side dusk horizon shines clearly */
.dark [data-main-chat-pane] div[style*="background-image"] + div {{
  background: linear-gradient(
    90deg,
    var(--color-gray-950, #0d0d0d) 0%,
    var(--color-gray-950, #0d0d0d) 7%,
    rgba(13, 13, 13, 0.90) 18%,
    rgba(13, 13, 13, 0.56) 36%,
    rgba(13, 13, 13, 0.26) 64%,
    rgba(13, 13, 13, 0.12) 100%
  ) !important;
}}

.dark [data-main-chat-pane]:has(div[style*="background-image"]) div.bottom-0.bg-linear-to-t {{
  background: transparent !important;
}}

/* 2. Editorial rounded cards & side-by-side 2-up/3-up gallery for inline Markdown news & web photos */
div.relative.group:has(> button > img[data-cy="image"][src$="/favicon.png"]),
p:has(> div:only-child > button > img[data-cy="image"][src$="/favicon.png"]),
#response-content-container div.relative.group:has(> [data-cy="image-unavailable"]),
.markdown-prose div.relative.group:has(> [data-cy="image-unavailable"]) {{
  display: none !important;
}}

#response-content-container img:not([class*="favicon"]):not([class*="size-"]):not([width="16"]):not([width="32"]),
.markdown-prose img:not([class*="favicon"]):not([class*="size-"]):not([width="16"]):not([width="32"]) {{
  display: block;
  width: auto;
  max-width: min(100%, 600px);
  max-height: 280px;
  object-fit: cover;
  border-radius: 14px;
  margin: 0.7rem 0 0.85rem 0;
  border: 1px solid rgba(148, 163, 184, 0.18);
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.28);
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}}

/* Multi-image paragraph (`![a](u1) ![b](u2)`): responsive side-by-side equal-height grid */
#response-content-container p:has(> div:nth-of-type(2) img[data-cy="image"]),
.markdown-prose p:has(> div:nth-of-type(2) img[data-cy="image"]) {{
  display: grid !important;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)) !important;
  gap: 12px !important;
  align-items: stretch !important;
  margin: 0.8rem 0 0.95rem 0 !important;
}}

#response-content-container p:has(> div:nth-of-type(2) img[data-cy="image"]) > div,
.markdown-prose p:has(> div:nth-of-type(2) img[data-cy="image"]) > div,
#response-content-container p:has(> div:nth-of-type(2) img[data-cy="image"]) > div > button,
.markdown-prose p:has(> div:nth-of-type(2) img[data-cy="image"]) > div > button {{
  width: 100% !important;
  height: 100% !important;
  display: block !important;
}}

#response-content-container p:has(> div:nth-of-type(2) img[data-cy="image"]) img[data-cy="image"],
.markdown-prose p:has(> div:nth-of-type(2) img[data-cy="image"]) img[data-cy="image"] {{
  width: 100% !important;
  height: 215px !important;
  max-height: 215px !important;
  max-width: 100% !important;
  object-fit: cover !important;
  margin: 0 !important;
}}

/* Consecutive single-image paragraphs (`![a](u1)\n\n![b](u2)` in existing chats): side-by-side on desktop */
@media (min-width: 641px) {{
  #response-content-container p:has(> div:only-child img[data-cy="image"]):has(+ p > div:only-child img[data-cy="image"]),
  #response-content-container p:has(> div:only-child img[data-cy="image"]) + p:has(> div:only-child img[data-cy="image"]),
  .markdown-prose p:has(> div:only-child img[data-cy="image"]):has(+ p > div:only-child img[data-cy="image"]),
  .markdown-prose p:has(> div:only-child img[data-cy="image"]) + p:has(> div:only-child img[data-cy="image"]) {{
    display: inline-block !important;
    width: calc(50% - 7px) !important;
    vertical-align: top !important;
    margin: 0.65rem 12px 0.85rem 0 !important;
  }}
  #response-content-container p:has(> div:only-child img[data-cy="image"]) + p:has(> div:only-child img[data-cy="image"]),
  .markdown-prose p:has(> div:only-child img[data-cy="image"]) + p:has(> div:only-child img[data-cy="image"]) {{
    margin-right: 0 !important;
  }}
  #response-content-container p:has(> div:only-child img[data-cy="image"]):has(+ p > div:only-child img[data-cy="image"]) > div,
  #response-content-container p:has(> div:only-child img[data-cy="image"]) + p:has(> div:only-child img[data-cy="image"]) > div,
  #response-content-container p:has(> div:only-child img[data-cy="image"]):has(+ p > div:only-child img[data-cy="image"]) > div > button,
  #response-content-container p:has(> div:only-child img[data-cy="image"]) + p:has(> div:only-child img[data-cy="image"]) > div > button {{
    width: 100% !important;
    display: block !important;
  }}
  #response-content-container p:has(> div:only-child img[data-cy="image"]):has(+ p > div:only-child img[data-cy="image"]) img[data-cy="image"],
  #response-content-container p:has(> div:only-child img[data-cy="image"]) + p:has(> div:only-child img[data-cy="image"]) img[data-cy="image"],
  .markdown-prose p:has(> div:only-child img[data-cy="image"]):has(+ p > div:only-child img[data-cy="image"]) img[data-cy="image"],
  .markdown-prose p:has(> div:only-child img[data-cy="image"]) + p:has(> div:only-child img[data-cy="image"]) img[data-cy="image"] {{
    width: 100% !important;
    height: 215px !important;
    max-height: 215px !important;
    max-width: 100% !important;
    object-fit: cover !important;
    margin: 0 !important;
  }}
}}

#response-content-container img:not([class*="favicon"]):not([class*="size-"]):hover,
.markdown-prose img:not([class*="favicon"]):not([class*="size-"]):hover {{
  transform: translateY(-1px) scale(1.006);
  box-shadow: 0 14px 34px rgba(0, 0, 0, 0.36);
}}

@media (max-width: 640px) {{
  #response-content-container img:not([class*="favicon"]):not([class*="size-"]),
  .markdown-prose img:not([class*="favicon"]):not([class*="size-"]) {{
    width: 100%;
    max-height: 220px;
    border-radius: 12px;
  }}
}}
"""


def _render_smart_label_replacement(m) -> str:
    g = m.groupdict()
    return (
        f'{SMART_LABEL_MARKER}{g["eff"]}(()=>({g["models"]}(),{g["get"]}({g["msg"]})),()=>{{'
        f'const _m={g["get"]}({g["msg"]}),'
        f'_auto=_m.model==="auto"||_m.model==="local-auto",'
        f'_tid=_auto&&_m.selectedModelId?_m.selectedModelId:_m.model;'
        f'{g["set"]}({g["mvar"]},{g["models"]}().find({g["it"]}=>{g["it"]}.id===_tid)||{g["models"]}().find({g["it"]}=>{g["it"]}.id===_m.model))'
        f'}}),'
        f'{g["eff"]}(()=>({g["get"]}({g["mvar"]}),{g["i18n"]}(),{g["get"]}({g["msg"]})),()=>{{'
        f'const _m={g["get"]}({g["msg"]}),'
        f'_auto=_m.model==="auto"||_m.model==="local-auto",'
        f'_simple={{fast:"Gemma 4",code:"Qwen 3.8",think:"Qwen 3.8",'
        f'deep:"Qwen 3.6",pro:"Qwen 3.6","3d":"Qwen 3.5 3D Coder","3d-coder":"Qwen 3.5 3D Coder",'
        f'voice:"Qwen 3 TTS",podcast:"Qwen 3 TTS",flash:"Qwen 3.5 4B",screen:"NorthCUA"}};'
        f'if(_auto&&_m.selectedModelId){{'
        f'const _sid=_m.selectedModelId,_lbl=_simple[_sid]||_sid;'
        f'{g["set"]}({g["nvar"]},`\U0001f4a1 Smart (${{_lbl}})`);return'
        f'}}'
        f'{g["set"]}({g["nvar"]},{g["get"]}({g["mvar"]})?{g["loc"]}({g["get"]}({g["mvar"]}),{g["i18n"]}().language):_m.modelName||_m.model)'
        f'}}){SMART_LABEL_END}'
    )


def _ensure_index_importmap(pkg: Path, chunk_name: str, chunk_bytes: bytes) -> None:
    import hashlib
    import json
    import re as _re

    idx_path = pkg / "frontend" / "index.html"
    if not idx_path.is_file():
        print("index.html: not found, importmap skipped")
        return
    ver = hashlib.sha256(chunk_bytes).hexdigest()[:8]
    raw = idx_path.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    src = raw.replace("\r\n", "\n")
    imports_map = {}
    existing = _re.search(
        _re.escape(IMPORTMAP_MARKER) + r'\n\s*<script type="importmap">(.*?)</script>',
        src,
        _re.S,
    )
    if existing:
        try:
            imports_map = dict((json.loads(existing.group(1)) or {}).get("imports") or {})
        except Exception:
            imports_map = {}
    key = f"/_app/immutable/chunks/{chunk_name}"
    imports_map[key] = f"{key}?v={ver}"
    payload = json.dumps({"imports": dict(sorted(imports_map.items()))}, separators=(",", ":"))
    tag = f'{IMPORTMAP_MARKER}\n\t\t<script type="importmap">{payload}</script>'
    if IMPORTMAP_MARKER in src:
        new_src = _re.sub(
            _re.escape(IMPORTMAP_MARKER) + r'\n\s*<script type="importmap">.*?</script>',
            tag,
            src,
            count=1,
            flags=_re.S,
        )
        if new_src != src:
            idx_path.write_text(new_src.replace("\n", nl), encoding="utf-8")
            print(f"index.html: updated importmap ({chunk_name}?v={ver})")
        else:
            print(f"index.html: importmap already up to date ({chunk_name}?v={ver})")
        return
    anchor = '<meta charset="utf-8" />'
    if anchor not in src:
        print("index.html: <meta charset> anchor not found, importmap skipped")
        return
    new_src = src.replace(anchor, f"{anchor}\n\t\t{tag}", 1)
    idx_path.write_text(new_src.replace("\n", nl), encoding="utf-8")
    print(f"index.html: patched importmap cache-bust ({chunk_name}?v={ver})")


def patch_usage_badge(pkg: Path) -> None:
    import re as _re

    chunks_dir = pkg / "frontend" / "_app" / "immutable" / "chunks"
    if not chunks_dir.is_dir():
        print("frontend chunks: directory not found, patch skipped")
        return
    rx = _re.compile(USAGE_BADGE_RE_PATTERN)
    der_rx = _re.compile(rf"let {_ID}=({_ID})\(\(\)=>\(")
    for chunk in sorted(chunks_dir.glob("*.js")):
        if not chunk.is_file():
            continue
        src = chunk.read_text(encoding="utf-8")
        if USAGE_BADGE_MARKER in src:
            print(f"{chunk.name}: already patched (inline tok/s badge)")
            _ensure_index_importmap(pkg, chunk.name, chunk.read_bytes())
            return
        matches = list(rx.finditer(src))
        if len(matches) == 1:
            m = matches[0]
            dm = der_rx.search(src, m.end(), m.end() + 500) or der_rx.search(src, max(0, m.start() - 500), m.start())
            if not dm:
                print(f"{chunk.name}: derived helper not found near usage badge, patch skipped")
                return
            rep = _render_usage_badge_replacement(m, dm.group(1))
            patched = src[:m.start()] + rep + src[m.end():]
            chunk.write_text(patched, encoding="utf-8")
            print(f"{chunk.name}: patched (inline tok/s badge)")
            _ensure_index_importmap(pkg, chunk.name, chunk.read_bytes())
            return
    print("frontend chunks: usage badge target not found, patch skipped")


def patch_smart_model_badge(pkg: Path) -> None:
    """Display the resolved router tier (`⚡⚡ Flash (GPU) · 💡 Smart`, `🧠 Pro · 💡 Smart`, etc.) when `auto` is used,
    without triggering blind Arena toasts or mutating `message.model` away from `auto`."""
    import re as _re

    chunks_dir = pkg / "frontend" / "_app" / "immutable" / "chunks"
    if not chunks_dir.is_dir():
        return
    lbl_rx = _re.compile(SMART_LABEL_RE_PATTERN)
    arena_rx = _re.compile(SMART_ARENA_RE_PATTERN)
    lbl_done = False
    arena_done = False

    for chunk in sorted(chunks_dir.glob("*.js")):
        if not chunk.is_file():
            continue
        src = chunk.read_text(encoding="utf-8")
        changed = False
        if SMART_LABEL_MARKER in src:
            lbl_done = True
            _ensure_index_importmap(pkg, chunk.name, chunk.read_bytes())
        elif SMART_LABEL_V1_MARKER in src and not lbl_done:
            v1_rx = _re.compile(
                _re.escape(SMART_LABEL_V1_MARKER)
                + rf'(?P<eff>{_ID})\(\(\)=>\((?P<models>{_ID})\(\),(?P<get>{_ID})\((?P<msg>{_ID})\)\),'
                + rf'\(\)=>{{const _m=(?P=get)\((?P=msg)\),_auto=_m\.model==="auto"\|\|_m\.model==="local-auto",'
                + rf'_tid=_auto&&_m\.selectedModelId\?_m\.selectedModelId:_m\.model;'
                + rf'(?P<set>{_ID})\((?P<mvar>{_ID}),(?P=models)\(\)\.find\((?P<it>{_ID})=>(?P=it)\.id===_tid\)\|\|'
                + rf'.*?(?P=set)\((?P<nvar>{_ID}),(?P=get)\((?P=mvar)\)\?(?P<loc>{_ID})\((?P=get)\((?P=mvar)\),(?P<i18n>{_ID})\(\)\.language\):_m\.modelName\|\|_m\.model\)}}\)',
                _re.S,
            )
            vms = list(v1_rx.finditer(src))
            if len(vms) == 1:
                vm = vms[0]
                src = src[:vm.start()] + _render_smart_label_replacement(vm) + src[vm.end():]
                changed = True
                lbl_done = True
                print(f"{chunk.name}: upgraded v1 -> v2 (💡 Smart (<Model>) badge)")
        elif not lbl_done:
            ms = list(lbl_rx.finditer(src))
            if len(ms) == 1:
                m = ms[0]
                src = src[:m.start()] + _render_smart_label_replacement(m) + src[m.end():]
                changed = True
                lbl_done = True
                print(f"{chunk.name}: patched (💡 Smart (<Model>) badge)")

        if SMART_ARENA_MARKER in src:
            arena_done = True
            _ensure_index_importmap(pkg, chunk.name, chunk.read_bytes())
        elif not arena_done:
            ams = list(arena_rx.finditer(src))
            if len(ams) == 1:
                am = ams[0]
                g = am.groupdict()
                rep = (
                    f'{SMART_ARENA_MARKER}{g["sel"]}&&('
                    f'{g["msg"]}.selectedModelId={g["sel"]},'
                    f'{g["msg"]}.model!=="auto"&&{g["msg"]}.model!=="local-auto"&&({g["msg"]}.arena=!0))'
                )
                src = src[:am.start()] + rep + src[am.end():]
                changed = True
                arena_done = True
                print(f"{chunk.name}: patched (smart selected_model_id non-arena guard)")

        if changed:
            chunk.write_text(src, encoding="utf-8")
            _ensure_index_importmap(pkg, chunk.name, chunk.read_bytes())

    if not lbl_done:
        print("frontend chunks: smart model badge target not found, patch skipped")
    if not arena_done:
        print("frontend chunks: smart selected_model_id guard target not found, patch skipped")


def patch_custom_css(pkg: Path) -> None:
    """Ensure `static/custom.css` has editorial photo styling + seamless #0d0d0d sidebar/wallpaper fade,
    and cache-bust `/static/custom.css?v=<hash>` in `frontend/index.html`."""
    import hashlib
    import re as _re

    css_targets = [p for p in (pkg / "static" / "custom.css", pkg / "frontend" / "static" / "custom.css") if p.parent.is_dir()]
    if not css_targets:
        print("custom.css: static folder not found, skipped")
        return
    css_bytes = RICH_CUSTOM_CSS.encode("utf-8")
    for css_path in css_targets:
        cur = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
        if cur != RICH_CUSTOM_CSS:
            css_path.write_text(RICH_CUSTOM_CSS, encoding="utf-8")
            print(f"{css_path.relative_to(pkg)}: updated (editorial photos + seamless #0d0d0d wallpaper fade)")
        else:
            print(f"{css_path.relative_to(pkg)}: already up to date")

    ver = hashlib.sha256(css_bytes).hexdigest()[:8]
    idx_path = pkg / "frontend" / "index.html"
    if idx_path.is_file():
        raw = idx_path.read_text(encoding="utf-8")
        nl = "\r\n" if "\r\n" in raw else "\n"
        src = raw.replace("\r\n", "\n")
        new_src = _re.sub(
            r'href="/static/custom\.css(?:\?v=[0-9a-f]+)?"',
            f'href="/static/custom.css?v={ver}"',
            src,
            count=1,
        )
        if new_src != src:
            idx_path.write_text(new_src.replace("\n", nl), encoding="utf-8")
            print(f"index.html: cache-busted custom.css (?v={ver})")


def main() -> int:
    spec = importlib.util.find_spec("open_webui")
    if not spec or not spec.submodule_search_locations:
        print("open_webui not found in this Python environment")
        return 1
    pkg = Path(list(spec.submodule_search_locations)[0])
    patch_main(pkg)
    patch_discovery(pkg)
    patch_image_tools(pkg)
    patch_usage_badge(pkg)
    patch_smart_model_badge(pkg)
    patch_custom_css(pkg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
