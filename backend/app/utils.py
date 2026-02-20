import os
from typing import Tuple
import pdfplumber
from PIL import Image
import pytesseract
import io
import re


def extract_text_from_pdf(path: str) -> str:
    text_parts = []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
                else:
                    # fallback: render page as image and OCR
                    try:
                        im = page.to_image(resolution=150).original
                        text_parts.append(pytesseract.image_to_string(im))
                    except Exception:
                        continue
    except Exception:
        return ""
    return "\n".join(text_parts)


def extract_text_from_image(path: str) -> str:
    try:
        img = Image.open(path)
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


HEADING_KEYWORDS = [
    "Purpose",
    "Architects",
    "Audience",
    "Context and Problem Statement",
    "Decisions",
    "Decision drivers",
    "Decision",
    "High-Level AWS Architecture",
    "Technical Specifications",
    "Database",
    "Key Points / Notes",
    "Rationale",
    "Authors",
    "Contributors",
]


def parse_cdr_text(text: str) -> dict:
    """
    Heuristic parser: finds known headings and captures text until the next heading.
    Returns a dict mapping heading -> content.
    """
    if not text:
        return {}

    # Normalize spacing
    t = text.replace('\r', '')

    # Build map of heading positions
    positions = {}
    lowered = t
    for h in HEADING_KEYWORDS:
        idx = lowered.find(h)
        if idx != -1:
            positions[idx] = h

    if not positions:
        # As fallback, try to split by blank-line separated paragraphs and return first chunk as body
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
        return {"body": paragraphs[0] if paragraphs else t}

    parsed = {}
    for pos in sorted(positions.keys()):
        heading = positions[pos]
        # start after heading
        start = pos + len(heading)
        # find next heading position
        next_positions = [p for p in positions.keys() if p > pos]
        end = min(next_positions) if next_positions else len(t)
        content = t[start:end].strip()
        # clean up leading punctuation/newlines
        content = re.sub(r"^[:\-\s]+", "", content)
        parsed[heading] = content

    return parsed


def extract_key_values(text: str, expected_keys: list | None = None) -> dict:
    """
    Extract key/value pairs from a block of text.
    Recognizes patterns like:
      - Key: Value
      - Key<TAB or multiple spaces>Value
      - Table rows like "Key    Value"
    Returns a dict of cleaned keys -> values.
    """
    if not text:
        return {}

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    result = {}

    # Prepare expected keys lookup (normalize to lowercase, longest-first)
    ek_list = []
    if expected_keys:
        # sort by length desc to prefer longer matches (e.g., 'Service offering' before 'Service')
        ek_list = sorted(expected_keys, key=lambda s: -len(s))
        ek_norm = [k.lower() for k in ek_list]
    else:
        ek_norm = []

    last_key = None
    for line in lines:
        low = line.lower()

        # 0) If expected keys provided, try to match a key at the start of the line
        matched = False
        for idx, ek in enumerate(ek_norm):
            if low.startswith(ek):
                orig_key = ek_list[idx]
                value = line[len(orig_key) :].strip()
                # if value starts with separator like ':' or '|' remove it
                value = re.sub(r"^[:\|\-\s]+", "", value)
                result[orig_key] = value
                matched = True
                break
        if matched:
            last_key = orig_key
            continue

        # 1) key: value
        if ":" in line:
            k, v = line.split(":", 1)
            result[k.strip()] = v.strip()
            last_key = k.strip()
            continue

        # 2) multiple spaces or tabs separation
        parts = re.split(r"\s{2,}|\t", line, maxsplit=1)
        if len(parts) == 2:
            k, v = parts
            result[k.strip()] = v.strip()
            last_key = k.strip()
            continue

        # 3) fallback: try to split by first space and see if left matches an expected key
        if " " in line and ek_norm:
            first_word = line.split(" ", 1)[0].lower()
            for idx, ek in enumerate(ek_norm):
                if ek.startswith(first_word):
                    orig_key = ek_list[idx]
                    value = line[len(orig_key) :].strip()
                    value = re.sub(r"^[:\|\-\s]+", "", value)
                    if value:
                        result[orig_key] = value
                        matched = True
                        break
            if matched:
                last_key = orig_key
                continue

        # 4) otherwise try last-space split (best-effort)
        if " " in line:
            k, v = line.rsplit(" ", 1)
            if len(k) > 1:
                result[k.strip()] = v.strip()
                last_key = k.strip()
                continue

        # 5) fallback: try to append this line to the previous key's value when it looks like a continuation
        appended = False
        if last_key and last_key in result:
            prev = result[last_key]
            # If previous value is empty, treat this as the value
            if prev == "":
                result[last_key] = line
                appended = True
            else:
                # If previous looks like an email fragment (contains @ but no dot after @), stitch without space
                if isinstance(prev, str) and "@" in prev and re.search(r"@[^.\s]+$", prev):
                    result[last_key] = prev + line
                    appended = True
                # If this line looks like a domain fragment (no spaces, contains a dot) and previous contains '@', stitch
                elif "@" in str(prev) and re.match(r"^[\w.-]+\.[a-z]{2,}$", line.strip(), re.IGNORECASE):
                    result[last_key] = prev + line.strip()
                    appended = True
                # If line contains pipe separators or looks like continuation text, append with space
                elif "|" in line or (":" not in line and not re.search(r"\s{2,}|\t", line)):
                    result[last_key] = prev + " " + line
                    appended = True

        if appended:
            continue

        # fallback: append to a free-text key
        idx = 0
        base = "text"
        key = f"{base}_{idx}"
        while key in result:
            idx += 1
            key = f"{base}_{idx}"
        result[key] = line
        last_key = key

    return result

import re

def extract_apm_from_text(text: str) -> dict:
    """
    Extract APM / Labels / Tagging related key/value items from a large block of text.
    Strategy:
      1. Find the region after 'Application Portfolio Management Details' heading (if present).
      2. Otherwise, search for lines that look like APM table rows (keys from an expected list) and capture surrounding lines.
      3. Parse the chosen block with extract_key_values.
    Returns a dict of extracted items (possibly empty).
    """
    if not text:
        return {}

    # Normalize line endings
    t = text.replace('\r', '')

    # 1) locate heading
    heading_regex = re.compile(r"Application\s*Portfolio\s*Management\s*Details", re.IGNORECASE)
    m = heading_regex.search(t)
    block = None
    if m:
        start = m.end()
        # take next lines until two consecutive blank lines or up to 200 collected lines
        lines = t[start:].splitlines()
        take = []
        blank_count = 0
        for ln in lines:
            if not ln.strip():
                blank_count += 1
                if blank_count >= 2:
                    break
                continue
            blank_count = 0
            take.append(ln)
            if len(take) > 200:
                break
        block = "\n".join(take)

    # 2) fallback: try to extract lines containing known APM keys
    expected_keys = [
        "Details",
        "Service offering",
        "Automated Service",
        "Environment",
        "APM Name",
        "APM ID",
        "MIO",
        "Business Unit",
        "Application Owner",
        "Compliance",
        "Application Service Level commitment",
        "Strategic Project ID",
        "Operational Project ID",
        "PMS ID",
        "Backup Policy",
        "Network Zone",
        "Patching Wave",
    ]

    if not block:
        lines = t.splitlines()
        candidates = []
        for i, ln in enumerate(lines):
            low = ln.lower()
            for ek in expected_keys:
                if ek.lower() in low:
                    # collect this line plus the next N lines as context (increased from 3 -> 10)
                    context = lines[i : i + 10 + 1]
                    candidates.extend(context)
        if candidates:
            block = "\n".join(candidates)

    # 3) if still not found, use whole text as last resort
    if not block:
        block = t

    # Clean HTML artifacts if any (e.g., from copy/paste containing links) - keep mailto:
    block = re.sub(r"https?://\S+", "", block)

    # Parse block into key/value pairs (give expected keys to help proper splitting)
    kv = extract_key_values(block, expected_keys=expected_keys)

    # Post-process keys: normalize spacing and capitalization; also pipe->comma, collapse spaces
    cleaned = {}
    for k, v in kv.items():
        clean_k = re.sub(r"\s+", " ", k).strip()
        if isinstance(v, str):
            val = v.replace("|", ",")
            val = re.sub(r"\s+", " ", val).strip()
        else:
            val = v
        cleaned[clean_k] = val

    # Build final output that contains ONLY the expected keys (preserve order)
    def normalize_key(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    cleaned_norm_map = {normalize_key(k): k for k in cleaned.keys()}

    final = {}
    for key in expected_keys:
        nk = normalize_key(key)
        if nk in cleaned_norm_map:
            final[key] = cleaned[cleaned_norm_map[nk]]
        else:
            final[key] = ""

    def find_owner_email_in_block(blk: str, max_lines_after: int = 20) -> str:
        """
        Prefer the email that appears within 'max_lines_after' lines after the 'Application Owner' line.
        If not found, return the first email found anywhere in the block.
        Never search the entire document to avoid picking unrelated emails.
        """
        email_re = re.compile(r"[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}")
        lines = blk.splitlines()

        # 1) Look near the 'Application Owner' line(s)
        for i, ln in enumerate(lines):
            if re.search(r"application\s*owner", ln, re.IGNORECASE):
                # search within the window after the key line
                window = "\n".join(lines[i : i + max_lines_after + 1])
                m_email = email_re.search(window)
                if m_email:
                    return m_email.group(0)

        # 2) Otherwise, take any email present within the block (still scoped)
        m_any = email_re.search(blk)
        if m_any:
            return m_any.group(0)

        return ""

    # If Application Owner missing/empty, fill from block-scoped search ONLY
    ao_key = None
    for k in final.keys():
        if normalize_key(k) == "applicationowner":
            ao_key = k
            break

    if ao_key and not final.get(ao_key):
        owner_email = find_owner_email_in_block(block)
        if owner_email:
            final[ao_key] = owner_email

    return final