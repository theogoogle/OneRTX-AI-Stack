"""Shrinks Playwright page snapshots (accessibility YAML) for small local models. Standard library only.

A heavy page (Met Office forecast: 464,000 characters) does not fit NorthCUA's 128K context. Kept: every visible
text and the [ref=...] of elements you can act on (links, buttons, fields...), so clicks still work. Dropped: refs of
plain text, layout-only lines, URLs, cursor/level markers.
"""
import re

ACTIONABLE = ("link", "button", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox", "option",
              "tab", "menuitem", "switch", "slider", "spinbutton", "treeitem")
REF = re.compile(r"\s*\[ref=[^\]]+\]")
NOISE = re.compile(r"\s*\[(cursor=pointer|level=\d+|active|selected|checked=false|expanded=false)\]")
LAYOUT_ONLY = re.compile(r"^-\s*(generic|separator|group|list|listitem|region|navigation|main|banner|contentinfo|"
                         r"article|section|complementary|figure|img|table|rowgroup|row|paragraph|strong|emphasis|"
                         r"time|term|definition|document|presentation|none)\s*:?\s*$")
SNAPSHOT_MARK = re.compile(r"\[ref=[a-z]*\d*e\d+\]")


def is_snapshot(text):
    return isinstance(text, str) and len(SNAPSHOT_MARK.findall(text[:20000])) >= 20


def compress(text):
    out = []
    for line in text.splitlines():
        body = line.lstrip()
        indent = (len(line) - len(body)) // 2
        if body.startswith("- /url:") or body.startswith("/url:"):
            continue
        role = re.match(r"-\s*([a-z]+)", body)
        keep_ref = role and role.group(1) in ACTIONABLE
        if not keep_ref:
            body = REF.sub("", body)
        body = NOISE.sub("", body)
        if LAYOUT_ONLY.match(body):
            continue
        body = re.sub(r"^-\s*(generic|paragraph|strong|emphasis|time|term|definition|cell|gridcell|listitem)"
                      r"\s*:\s*", "- ", body)
        out.append(" " * min(indent, 12) + body)
    return "\n".join(out)


def shrink(text, limit):
    """Compressed snapshot, cut in the middle if still over `limit` characters."""
    small = compress(text) if is_snapshot(text) else text
    if len(small) <= limit:
        return small
    head, tail = int(limit * 0.6), int(limit * 0.4)
    return (small[:head] + f"\n\n[... {len(small) - head - tail:,} characters of this page left out to fit the model. "
            "Use browser_find with a keyword to read a specific part ...]\n\n" + small[-tail:])
