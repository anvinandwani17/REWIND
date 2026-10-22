"""PII masking for what the agent sees, what the ledger stores as args, and audit exports."""
import re

PII_KEYS = {"name", "phone"}
IDENTIFIER_KEYS = {"account_id", "new_status", "action_id"}  # never free text

_PHONE_LIKE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def mask_name(name):
    return " ".join(word[0] + "***" for word in name.split()) if name else name


def mask_phone(phone):
    """Keep only the last two digits: +91 90000 00102 -> +** ***** ***02."""
    if not phone:
        return phone
    total = sum(ch.isdigit() for ch in phone)
    seen, out = 0, []
    for ch in phone:
        if ch.isdigit():
            seen += 1
            out.append(ch if seen > total - 2 else "*")
        else:
            out.append(ch)
    return "".join(out)


def mask_pii(value):
    """Recursively mask every "name" / "phone" field in dicts and lists."""
    if isinstance(value, dict):
        return {
            key: (mask_name(val) if key == "name" else mask_phone(val))
            if key in PII_KEYS and isinstance(val, str) else mask_pii(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [mask_pii(item) for item in value]
    return value


def mask_text(text, known_names):
    """Mask phone numbers and known customer name words inside free text.

    ponytail: heuristic. Phones = 9+ digits in a run (dates have 8, so they
    survive); names = words of known customer names, so an unknown name in
    free text is not caught. Upgrade to an NER pass if free text grows.
    """
    def phone(match):
        found = match.group()
        return mask_phone(found) if sum(ch.isdigit() for ch in found) >= 9 else found

    text = _PHONE_LIKE.sub(phone, text)
    words = sorted({word for name in known_names for word in name.split() if len(word) >= 3},
                   key=len, reverse=True)
    if words:
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.IGNORECASE)
        text = pattern.sub(lambda m: m.group()[0] + "***", text)
    return text


def mask_args(args, known_names):
    return {
        key: mask_text(val, known_names) if isinstance(val, str) and key not in IDENTIFIER_KEYS else val
        for key, val in args.items()
    }
