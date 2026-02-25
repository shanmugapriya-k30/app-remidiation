import os
from typing import Tuple
import io
import re


def extract_text_from_pdf(path: str) -> str:
    text_parts = []
    try:
        # lazy imports so module import doesn't fail when testing parsing logic
        import pdfplumber
        import pytesseract

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
        # lazy import to avoid import-time dependency errors
        from PIL import Image
        import pytesseract
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
def extract_technical_table(text: str) -> dict:
    """
    Improved extractor that reconstructs instance-spec fragments and extracts a clean
    Configuration_2 value (e.g. "Configuration Instance type = db.m4.xlarge, VCPU = 4, Memory= 16GB, Storage = 256GB, Single AZ").
    """
    if not text:
        return {}

    parsed = parse_cdr_text(text) if isinstance(text, str) else {}
    section_text = parsed.get("Technical Specifications") if isinstance(parsed, dict) else None
    t = section_text if section_text else text
    t = t.replace("Integratio n", "Integration").replace("Develo pment", "Development").replace("\r", "")

    raw_lines = [ln.rstrip() for ln in t.splitlines()]

    # helper: detect header in raw_lines
    header_pattern = re.compile(r"Production|Staging|Integration|Development", re.IGNORECASE)
    header_raw_index = None
    for idx, ln in enumerate(raw_lines):
        if header_pattern.search(ln):
            header_raw_index = idx
            break

    # helper to detect instance-spec tokens
    def looks_like_instance_spec(s: str) -> bool:
        if not s:
            return False
        return bool(re.search(r"\b(instance\s*type|vcpu|memory|storage|single\s*az|db\.[a-z0-9\-]+|db\s*\.)\b", s, re.IGNORECASE))

    # Extract a focused instance-spec substring from a broader window
    def extract_instance_spec_from_window(window: str) -> str:
        if not window:
            return ""
        w = window.strip()
        lw = w.lower()
        # find earliest anchor
        anchors = ["configuration instance", "configuration instance type", "instance type", "instance", "db."]
        start = None
        for a in anchors:
            idx = lw.find(a)
            if idx != -1:
                start = idx
                break
        if start is not None:
            candidate = w[start:]
        else:
            candidate = w

        # remove leading numeric/account noise like "434273790685)" etc.
        candidate = re.sub(r'^[\s\W]*\d+[\)\s\W]*', '', candidate)

        # Strip a leading "Configuration" (or variants) so output begins with "Instance..."
        candidate = re.sub(r'^\s*configuration[\s\-\:\)]*', '', candidate, flags=re.IGNORECASE)

        # Ensure the first word "instance" is capitalized to "Instance"
        candidate = re.sub(r'^\s*instance', 'Instance', candidate, flags=re.IGNORECASE)

        # remove repeated "N/A" tokens
        candidate = re.sub(r"\bN/?A\b(?:\s+N/?A\b)*", "", candidate, flags=re.IGNORECASE)
        # collapse spaces
        candidate = re.sub(r"\s+", " ", candidate).strip()
        return candidate

    # build rows similar to previous logic but use raw_lines so neighbors are available
    if header_raw_index is None:
        rows = []
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                rows.append({
                    "Environment": k.strip(),
                    "Production": v.strip() if isinstance(v, str) else "",
                    "Staging": "",
                    "Integration": "",
                    "Development": ""
                })
        else:
            return {}
    else:
        rows = []
        current_row = None
        for rel_idx, ln in enumerate(raw_lines[header_raw_index + 1 :]):
            i = header_raw_index + 1 + rel_idx
            line = ln.strip()
            if not line:
                continue

            # if instance-spec-like but does not start with a label, capture neighbor window and extract focused spec
            if looks_like_instance_spec(line) and not re.match(r"^(Application|Configuration|Database)\b", line, re.IGNORECASE):
                start = max(0, i - 2)
                end = min(len(raw_lines), i + 3)
                window = " ".join([raw_lines[j].strip() for j in range(start, end) if raw_lines[j].strip()])
                spec = extract_instance_spec_from_window(window)
                if spec:
                    if current_row:
                        rows.append(current_row)
                        current_row = None
                    rows.append({
                        "Environment": "Configuration_2",
                        "Production": spec,
                        "Staging": "",
                        "Integration": "",
                        "Development": ""
                    })
                continue

            # labeled rows (including configuration instance variants)
            m = re.match(r"^(Application|Configuration|Database|Configuration\s*Instance\s*Type|Configuration\s*Instance)\b(.*)$", line, re.IGNORECASE)
            if m:
                if current_row:
                    rows.append(current_row)
                raw_label = m.group(1)
                rest = m.group(2).strip()
                if re.match(r"Configuration\s*Instance", raw_label, re.IGNORECASE):
                    label = "Configuration_2"
                else:
                    label = raw_label.title()

                parts = re.split(r"\s{2,}|\t|\s\|\s", rest)
                parts = [p.strip() for p in parts if p.strip()]
                if len(parts) >= 4:
                    prod, stag, integ, dev = parts[0], parts[1], parts[2], parts[3]
                elif len(parts) > 0:
                    toks = rest.split()
                    if len(toks) >= 4 and all(len(tok) <= 10 for tok in toks[-3:]):
                        prod = " ".join(toks[:-3]).strip()
                        stag, integ, dev = toks[-3], toks[-2], toks[-1]
                    else:
                        prod = rest
                        stag = integ = dev = ""
                else:
                    prod = rest
                    stag = integ = dev = ""

                current_row = {
                    "Environment": label,
                    "Production": prod,
                    "Staging": stag,
                    "Integration": integ,
                    "Development": dev,
                }
            else:
                if current_row:
                    if current_row.get("Production"):
                        current_row["Production"] = (current_row["Production"] + " " + line).strip()
                    else:
                        for col in ("Production", "Staging", "Integration", "Development"):
                            if not current_row.get(col):
                                current_row[col] = line
                                break

        if current_row:
            rows.append(current_row)

    # consolidate into interim dict
    interim = {}
    name_count = {}
    for r in rows:
        label = r.get("Environment", "Unknown")
        key = label
        if label in name_count:
            name_count[label] += 1
            key = f"{label}_{name_count[label]}"
        else:
            name_count[label] = 1

        interim[key] = {
            "Production": re.sub(r"\s+", " ", r.get("Production", "")).strip(),
            "Staging": re.sub(r"\s+", " ", r.get("Staging", "")).strip(),
            "Integration": re.sub(r"\s+", " ", r.get("Integration", "")).strip(),
            "Development": re.sub(r"\s+", " ", r.get("Development", "")).strip(),
        }

    # canonicalization and fallbacks
    def nonempty_or_na(s: str) -> str:
        s = s.strip() if isinstance(s, str) else ""
        return s if s else "N/A"

    canonical = {
        "Application": "N/A",
        "Configuration": "N/A",
        "Database": "N/A",
        "Configuration_2": "N/A"
    }

    def is_instance_spec(s: str) -> bool:
        if not s:
            return False
        s = s.lower()
        return bool(re.search(r"\b(instance\s*type|vcpu|memory|storage|single\s*az|db\.[a-z0-9\-]+|db\s*\.)\b", s, re.IGNORECASE))

    for k, v in interim.items():
        lk = k.lower()
        prod = v.get("Production", "").strip()
        if "application" in lk and canonical["Application"] == "N/A":
            canonical["Application"] = nonempty_or_na(prod)
        elif "configuration" in lk:
            # if the production content looks like instance-spec, route to Configuration_2
            if is_instance_spec(prod) or re.search(r"instance\s*type|db\.", prod, re.IGNORECASE) or lk.startswith("configuration_2"):
                # try to extract precise spec if the string is noisy
                extracted = extract_instance_spec_from_window(prod)
                canonical["Configuration_2"] = nonempty_or_na(extracted or prod)
            else:
                if canonical["Configuration"] == "N/A":
                    canonical["Configuration"] = nonempty_or_na(prod)
        elif "database" in lk and canonical["Database"] == "N/A":
            canonical["Database"] = nonempty_or_na(prod)

    # supplement from parsed headings when useful
    def candidate_from_heading(k: str, v: str) -> str:
        s = f"{k} {v}".strip()
        s = re.sub(r"\s{2,}", " ", s)
        m = re.search(r"(Instance\s*Type\s*[:=]?.*|Will\s+use.*|Will\s+provision.*|\(XSPL[^\)]+\).*|Sanofi[_\w\-\s,()0-9]+434273790685.*)", s, re.IGNORECASE)
        if m:
            return m.group(0).strip()
        m2 = re.search(r"Production[:\s\-]*(.*)$", s, re.IGNORECASE)
        if m2:
            return m2.group(1).strip()
        s2 = re.sub(r"\b(Application|Configuration|Database|Configuration\s*Instance\s*Type)\b[:\-\s]*", "", s, flags=re.IGNORECASE).strip()
        return s2

    if isinstance(parsed, dict):
        for k, v in parsed.items():
            lk = k.lower()
            cand = candidate_from_heading(k, v if isinstance(v, str) else "")
            if "application" in lk:
                if canonical["Application"] == "N/A" or (len(canonical["Application"]) < 10 and cand):
                    canonical["Application"] = nonempty_or_na(cand)
            elif "configuration instance" in lk or ("configuration" in lk and "instance" in k.lower()) or is_instance_spec(cand):
                extracted = extract_instance_spec_from_window(cand)
                if canonical["Configuration_2"] == "N/A" or (len(canonical["Configuration_2"]) < 10 and extracted):
                    canonical["Configuration_2"] = nonempty_or_na(extracted or cand)
            elif re.match(r"^configuration\b", lk):
                if canonical["Configuration"] == "N/A" or (len(canonical["Configuration"]) < 10 and cand):
                    canonical["Configuration"] = nonempty_or_na(cand)
            elif "database" in lk:
                if canonical["Database"] == "N/A" or (len(canonical["Database"]) < 10 and cand):
                    canonical["Database"] = nonempty_or_na(cand)

    # explicit fallbacks
    if canonical["Database"] == "N/A" and isinstance(parsed, dict):
        for heading, content in parsed.items():
            if "database" in heading.lower() and isinstance(content, str) and content.strip():
                canonical["Database"] = nonempty_or_na(content)
                break

    if canonical["Configuration_2"] == "N/A":
        # search for typical instance-spec lines
        m = re.search(r"(Instance\s*type\s*[:=]?.{0,200}?(db\.[\w\-\d\.]+.*?)(?=\n[A-Z][a-z]|$))", t, re.IGNORECASE | re.S)
        if m:
            extracted = extract_instance_spec_from_window(m.group(0))
            canonical["Configuration_2"] = nonempty_or_na(extracted or m.group(0).strip())
        else:
            m2 = re.search(r"Instance\s*type\s*[:=]?\s*(.+?)(?:\n[A-Z][a-z]|\n\s*\n|$)", t, re.IGNORECASE | re.S)
            if m2:
                extracted = extract_instance_spec_from_window(m2.group(0))
                canonical["Configuration_2"] = nonempty_or_na(extracted or m2.group(0).strip())

    if canonical["Configuration_2"] == "N/A" and isinstance(parsed, dict):
        for heading, content in parsed.items():
            if "configuration" in heading.lower() and isinstance(content, str) and re.search(r"instance\s*type|db\.", content, re.IGNORECASE):
                canonical["Configuration_2"] = nonempty_or_na(extract_instance_spec_from_window(content) or content)
                break

    # tidy final values
    def tidy_value(s: str) -> str:
        if not s or s.upper() == "N/A":
            return "N/A"
        s = s.strip()
        s = re.sub(r"\s*=\s*", " = ", s)
        s = re.sub(r"\s*,\s*", ", ", s)
        s = re.sub(r"\s*\(\s*", " (", s)
        s = re.sub(r"\s*\)\s*", ")", s)
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"\bvcpu\b", "vCPU", s, flags=re.IGNORECASE)
        # remove any trailing stray "N/A" fragments introduced earlier
        s = re.sub(r"(?:\bN/?A\b[\s,]*)+$", "", s, flags=re.IGNORECASE).strip()
        return s

    final = {}
    for k in ("Application", "Configuration", "Database", "Configuration_2"):
        final[k] = {
            "Production": tidy_value(canonical.get(k, "N/A")),
            "Staging": "N/A",
            "Integration": "N/A",
            "Development": "N/A"
        }

    return final
   