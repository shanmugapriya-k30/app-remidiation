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
        # take next up to 30 lines or until two consecutive blank lines or another major heading
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
                    # collect this line plus the next 3 lines as context
                    context = lines[i : i + 4]
                    candidates.extend(context)
        if candidates:
            block = "\n".join(candidates)

    # 3) if still not found, use whole text as last resort
    if not block:
        block = t

    # Clean HTML artifacts if any (e.g., from copy/paste containing links)
    block = re.sub(r"https?://\S+", "", block)

    # Parse block into key/value pairs (give expected keys to help proper splitting)
    kv = extract_key_values(block, expected_keys=expected_keys)

    # Post-process keys: normalize spacing and capitalization
    cleaned = {}
    for k, v in kv.items():
        clean_k = re.sub(r"\s+", " ", k).strip()
        cleaned[clean_k] = v.strip() if isinstance(v, str) else v

    # If Application Owner value is missing or empty, try to find an email near the heading in the original block or full text
    ao_key = None
    for k in cleaned.keys():
        if k.lower().replace(" ", "") == "applicationowner":
            ao_key = k
            break

    if (not ao_key) or (ao_key and not cleaned.get(ao_key)):
        # search in block first (block variable exists), then full text
        search_space = block if 'block' in locals() and block else t
        # find position of application owner in search_space
        m = re.search(r"application\s*owner", search_space, re.IGNORECASE)
        email = None
        if m:
            # search for email within the next 200 characters
            sub = search_space[m.end(): m.end() + 400]
            em = re.search(r"[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}", sub)
            if em:
                email = em.group(0)

        # fallback: search entire text for any email
        if not email:
            em2 = re.search(r"[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}", t)
            if em2:
                email = em2.group(0)

        if email:
            key_name = ao_key if ao_key else "Application Owner"
            cleaned[key_name] = email

    return cleaned
