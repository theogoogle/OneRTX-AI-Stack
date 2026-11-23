"""Text normalization, EN/FR detection, request header parsing, and sentence/paragraph chunking for podcast stories."""
import re

FR_DIACRITICS_RE = re.compile(r"[àâäçéèêëîïôöùûüÿœæ«»]", re.I)
FR_WORDS_RE = re.compile(
    r"\b(le|la|les|un|une|des|du|au|aux|et|est|sont|dans|pour|avec|qui|que|sur|sous|chez|sans|"
    r"il|elle|ils|elles|nous|vous|mais|pas|plus|comme|tout|tous|toute|petit|petite|grand|grande|"
    r"était|étaient|avait|avaient|fait|dit|bois|forêt|nuit|jour|fois|très|bien|aussi|alors|"
    r"c'est|qu'il|qu'elle|d'un|d'une|l'histoire|mon|ma|mes|ton|ta|son|sa|ses)\b",
    re.I,
)
EN_WORDS_RE = re.compile(
    r"\b(the|and|is|are|was|were|in|on|at|with|for|that|this|these|those|they|them|she|her|he|him|"
    r"it|its|but|not|from|once|upon|time|little|small|big|there|here|said|asked|whispered|into|"
    r"onto|out|about|have|has|had|you|your|my|our|one|two|forest|night|day|very|then|suddenly)\b",
    re.I,
)

ABBREV_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|Rev|Gen|Col|Lt|Capt|Mme|Mlle|Mgr|M|MM|etc|cf|vs|ex|fig|p|pp|vol|ch|no|n°)\.$",
    re.I,
)
HEADER_DIRECTIVE_RE = re.compile(
    r"^\s*(?:please\b|pls\b|can\s+you\b|could\s+you\b|would\s+you\b|"
    r"peux[- ]tu\b|pourrais[- ]tu\b|pouvez[- ]vous\b|"
    r"read\b|narrate\b|speak\b|recite\b|convert\b|compare\b|test\b|try\b|use\b|"
    r"lis\b|raconte\b|narre\b|r[ée]cite\b|convertis\b|compare[rz]?\b|essaie\b|teste\b|utilise[rz]?\b|"
    r"engine\s*[:=]|voice\s*[:=]|profil\s*[:=]|language\s*[:=]|lang\s*[:=]|"
    r"qwen3?(?:-tts)?\b|chatterbox\b)",
    re.I,
)


def detect_language(text: str, default: str = "en") -> str:
    """Heuristic French vs. English detector tuned for children's stories."""
    sample = (text or "")[:8000]
    if not sample.strip():
        return default
    fr_score = len(FR_DIACRITICS_RE.findall(sample)) * 2 + len(FR_WORDS_RE.findall(sample)) * 3
    en_score = len(EN_WORDS_RE.findall(sample)) * 3
    if fr_score == en_score:
        return default
    return "fr" if fr_score > en_score else "en"


def parse_story_request(raw_text: str):
    """Extract optional engine/language/exaggeration header from a copy-pasted chat prompt without eating the story."""
    txt = (raw_text or "").replace("\r\n", "\n").strip()
    opts = {"engine": None, "language": None, "voice": None, "exaggeration": None, "story_text": txt}
    if not txt:
        return opts

    header = ""
    rest = ""
    # Case 1: "Short directive line:\n<story>" or "Short directive line\n\n<story>"
    lines = txt.split("\n", 1)
    first_line = lines[0].strip()
    if len(lines) == 2 and lines[1].strip() and len(first_line) <= 220 and HEADER_DIRECTIVE_RE.search(first_line):
        if first_line.endswith(":") or (not first_line.endswith((".", "!", "?")) and len(first_line.split()) <= 20):
            header, rest = first_line, lines[1].strip()
    # Case 2: Single line "Read this in my voice: Once upon a time..."
    if not header and ":" in txt[:180]:
        head, tail = txt.split(":", 1)
        if len(head.strip()) <= 160 and len(tail.strip()) >= 20 and HEADER_DIRECTIVE_RE.search(head):
            header, rest = head.strip(), tail.strip()

    if header:
        h = header.lower()
        if re.search(r"\b(compare|both|a/b|ab|les\s+deux)\b", h):
            opts["engine"] = "both"
        elif re.search(r"\b(chatterbox|cb)\b", h) and not re.search(r"\bqwen", h):
            opts["engine"] = "chatterbox"
        elif re.search(r"\b(qwen3?|qwen-tts)\b", h) and not re.search(r"\bchatterbox\b", h):
            opts["engine"] = "qwen3"

        if re.search(r"\b(in\s+french|french\s+voice|en\s+fran[çc]ais|voix\s+fran[çc]aise|\bfr\b)\b", h):
            opts["language"] = "fr"
        elif re.search(r"\b(in\s+english|english\s+voice|en\s+anglais|voix\s+anglaise|\ben\b)\b", h):
            opts["language"] = "en"

        m_ex = re.search(r"\bexaggeration\s*[:=]?\s*(0?\.\d+|1(?:\.0+)?)\b", h)
        if m_ex:
            opts["exaggeration"] = max(0.1, min(1.2, float(m_ex.group(1))))

        m_v = re.search(r"\b(?:voice|profil|profile)\s*[:=]\s*([a-zA-Z0-9_.-]+)\b", header, re.I)
        if m_v:
            opts["voice"] = m_v.group(1)

        if rest:
            opts["story_text"] = rest

    return opts


def clean_story_text(text: str) -> str:
    """Strip Markdown noise while preserving paragraph breaks and natural spoken punctuation."""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # [label](url) -> label
    s = re.sub(r"\[([^\]]+)\]\((?:https?://|/)[^)]+\)", r"\1", s)
    # Remove fenced code markers if someone accidentally pasted them
    s = re.sub(r"```[^\n]*\n", "", s).replace("```", "")
    out_lines = []
    for raw_ln in s.split("\n"):
        ln = raw_ln.strip()
        if not ln:
            out_lines.append("")
            continue
        is_heading = bool(re.match(r"^#{1,6}\s+", ln))
        ln = re.sub(r"^#{1,6}\s+", "", ln)
        ln = re.sub(r"^\s*[-*•]\s+", "", ln)
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        ln = re.sub(r"__(.+?)__", r"\1", ln)
        ln = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", ln)
        ln = ln.replace("…", "...")
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if is_heading:
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            out_lines.append(f"__HEADING__{ln}")
            out_lines.append("")
        else:
            out_lines.append(ln)
    joined = "\n".join(out_lines)
    joined = re.sub(r"\n{3,}", "\n\n", joined).strip()
    return joined


def _split_sentences(paragraph: str):
    """Split a paragraph into complete sentences without breaking common EN/FR abbreviations or decimals."""
    # Protect decimal numbers like 3.5 -> 3<DOT>5
    work = re.sub(r"(\d)\.(\d)", r"\1<DOT>\2", paragraph)
    # Split candidate boundaries: . ! ? ... followed by optional closing quote/paren + whitespace
    tokens = re.split(r"([.!?]+(?:\.{0,2})[\"'»”)\]]*)(?=\s+|$)", work)
    sentences = []
    buf = ""
    i = 0
    while i < len(tokens):
        part = tokens[i]
        punct = tokens[i + 1] if i + 1 < len(tokens) else ""
        i += 2
        piece = (part + punct).strip()
        if not piece:
            continue
        buf = (buf + " " + piece).strip() if buf else piece
        # Keep merging if buf ends with a known abbreviation like Mr. or Mme.
        tail_word = buf.split()[-1] if buf.split() else ""
        tail_clean = re.sub(r"[\"'»”)\]]+$", "", tail_word)
        if punct and ABBREV_RE.search(tail_clean):
            continue
        if punct:
            sentences.append(buf.replace("<DOT>", "."))
            buf = ""
    if buf:
        sentences.append(buf.replace("<DOT>", "."))
    return [s for s in sentences if s.strip()]


def _split_long_clause(sentence: str, max_chars: int):
    """Split an extra-long sentence cleanly on clause punctuation, falling back to spaces."""
    if len(sentence) <= max_chars:
        return [sentence]
    parts = re.split(r"([,;:\u2014\u2013—–]\s+)", sentence)
    chunks, cur = [], ""
    for i in range(0, len(parts), 2):
        seg = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if len(cur) + len(seg) <= max_chars:
            cur += seg
        else:
            if cur.strip():
                chunks.append(cur.strip())
            if len(seg) <= max_chars:
                cur = seg
            else:
                # Last resort: split at spaces
                words = seg.split()
                cur = ""
                for w in words:
                    cand = f"{cur} {w}".strip() if cur else w
                    if len(cand) <= max_chars:
                        cur = cand
                    else:
                        if cur:
                            chunks.append(cur)
                        cur = w
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def chunk_story(text: str, language: str = None, target_chars: int = 210, max_chars: int = 270):
    """Split story text into spoken chunks with natural pause durations (ms)."""
    cleaned = clean_story_text(text)
    if not cleaned:
        return []
    default_lang = language or detect_language(cleaned)
    raw_paras = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
    chunks = []
    for p_idx, para in enumerate(raw_paras):
        is_heading = para.startswith("__HEADING__")
        if is_heading:
            para = para[len("__HEADING__"):].strip()
        # Preserve single-line breaks inside a paragraph as soft sentence breaks (e.g. dialogue lines)
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        sentences = []
        for ln in lines:
            sentences.extend(_split_sentences(ln))
        # Expand any single huge sentence
        expanded = []
        for s in sentences:
            expanded.extend(_split_long_clause(s, max_chars))
        # Group short sentences up to target_chars while respecting max_chars
        grouped = []
        cur = ""
        for s in expanded:
            cand = f"{cur} {s}".strip() if cur else s
            if cur and len(cand) > target_chars:
                grouped.append(cur)
                cur = s
            elif len(cand) <= max_chars:
                cur = cand
            else:
                if cur:
                    grouped.append(cur)
                cur = s
        if cur:
            grouped.append(cur)

        para_lang = language or detect_language(para, default=default_lang)
        for j, g in enumerate(grouped):
            is_last_in_para = j == len(grouped) - 1
            if is_heading:
                pause_ms = 850
            elif is_last_in_para:
                pause_ms = 580
            else:
                pause_ms = 210
            chunks.append({
                "text": g,
                "language": para_lang,
                "pause_after_ms": pause_ms,
                "is_heading": is_heading,
                "paragraph": p_idx,
            })
    if chunks:
        chunks[-1]["pause_after_ms"] = 320
    return chunks
