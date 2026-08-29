from __future__ import annotations

from pathlib import Path

PATH = Path("src/sources/generic_multi_record.py")
text = PATH.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one replacement target, got {count}: {old[:120]!r}")
    text = text.replace(old, new, 1)


replace_once(
    '''def _compact(value: str | None) -> str:\n    return re.sub(r"\\s+", " ", str(value or "")).strip()\n\n\ndef _candidate''',
    '''def _compact(value: str | None) -> str:\n    return re.sub(r"\\s+", " ", str(value or "")).strip()\n\n\ndef _is_inferred_code(value: str | None) -> bool:\n    token = _compact(value)\n    if not token:\n        return False\n    upper = token.upper()\n    if upper in _STOP_CODES or not _CODE_TOKEN_RE.fullmatch(upper):\n        return False\n    # Inferred values are deliberately stricter than explicit data-* values.\n    # Natural-language words after labels such as ``Промокод получите`` must\n    # never become business values. Mixed-case tokens are accepted only when\n    # they contain a digit; otherwise require an all-uppercase code shape.\n    return token == upper or any(char.isdigit() for char in token)\n\n\ndef _candidate''',
)

replace_once(
    '''    ) -> str | None:\n        if strong:\n            match = _MERCHANT_FROM_STRONG_RE.search(strong)''',
    '''    ) -> str | None:\n        # Reveal/show cards frequently contain service counters in <strong>\n        # while their logo alt carries the stable merchant label.\n        if prefer_image and image_alt and len(image_alt) <= 120:\n            return image_alt\n        if strong:\n            match = _MERCHANT_FROM_STRONG_RE.search(strong)''',
)

replace_once(
    '''        if prefer_image and image_alt and len(image_alt) <= 120:\n            return image_alt\n        if summary:''',
    '''        if summary:''',
)

replace_once(
    '''        if strong and _CODE_TOKEN_RE.fullmatch(strong) and strong.upper() not in _STOP_CODES:\n            return strong''',
    '''        if strong and _is_inferred_code(strong):\n            return strong''',
)

replace_once(
    '''        match = _CODE_AFTER_LABEL_RE.search(tail)\n        if match:\n            value = match.group(1)\n            if value.upper() not in _STOP_CODES:\n                return value\n        for match in _CODE_SCAN_RE.finditer(tail):\n            value = match.group(1)\n            if value.upper() not in _STOP_CODES:\n                return value''',
    '''        match = _CODE_AFTER_LABEL_RE.search(tail)\n        if match:\n            value = match.group(1)\n            if _is_inferred_code(value):\n                return value\n        for match in _CODE_SCAN_RE.finditer(tail):\n            value = match.group(1)\n            if _is_inferred_code(value):\n                return value''',
)

replace_once(
    '''    ) -> str:\n        coupon_id = _compact(str(data.get("data-coupon-id") or ""))\n        if coupon_id.isdigit():\n            return f"{source_key}-coupon:{coupon_id}"\n        offer_id = parse_qs(urlsplit(source_url).query).get("offer_id", [None])[0]\n        if offer_id:\n            return str(offer_id)\n        summary = _SUMMARY_RE.fullmatch(text)\n        if summary and merchant and percent is not None:\n            return external_id(source_url, merchant, str(percent))\n        if anchor_kind == "heading":\n            return external_id(source_url, title, promo_code)\n        action_signal = action_text or text\n        if _ACTION_ACTIVATE_RE.search(action_signal):\n            return external_id(source_url, merchant, title, promo_code or "")\n        if _ACTION_SHOW_RE.search(action_signal):\n            return external_id(source_url, title)\n        if _ACTION_OPEN_RE.search(action_signal):\n            return external_id(source_url, merchant, title)\n        if promo_code or record_tag == "article":''',
    '''    ) -> str:\n        # Heading-based records and explicit URL offer IDs are semantic record\n        # identities. A nested coupon marker must not replace either. For plain\n        # action records keep coupon priority until live evidence proves a\n        # stronger generic rule; this preserves existing safe-superset parity.\n        if anchor_kind == "heading":\n            return external_id(source_url, title, promo_code)\n        offer_id = parse_qs(urlsplit(source_url).query).get("offer_id", [None])[0]\n        if offer_id:\n            return str(offer_id)\n        coupon_id = _compact(str(data.get("data-coupon-id") or ""))\n        if coupon_id.isdigit():\n            return f"{source_key}-coupon:{coupon_id}"\n        summary = _SUMMARY_RE.fullmatch(text)\n        if summary and merchant and percent is not None:\n            return external_id(source_url, merchant, str(percent))\n        action_signal = action_text or text\n        if _ACTION_ACTIVATE_RE.search(action_signal):\n            return external_id(source_url, merchant, title, promo_code or "")\n        if _ACTION_SHOW_RE.search(action_signal):\n            return external_id(source_url, title)\n        if _ACTION_OPEN_RE.search(action_signal):\n            return external_id(source_url, merchant, title)\n        if promo_code or record_tag == "article":''',
)

PATH.write_text(text, encoding="utf-8")
