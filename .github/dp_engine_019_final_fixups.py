from __future__ import annotations

from pathlib import Path

PATH = Path("arvectum_data/engine/html_records.py")
text = PATH.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one fixup target, got {count}: {old[:100]!r}")
    text = text.replace(old, new, 1)


replace_once(
    '''def _unique_coupon_identity_count(node: _Node) -> int:\n    # Count distinct strong business identities, not DOM copies. A wrapper with\n    # several different promo values is a collection even when it has no\n    # data-coupon-id; repeated copies of one value remain one offer unit.\n    values = {\n        (key, _compact(current.attrs.get(key, "")))\n        for current in node.walk()\n        for key in _MACHINE_DATA_KEYS\n        if current.attrs.get(key)\n    }\n    if values:\n        return len(values)\n    return _count_descendants(node, _has_machine_marker)''',
    '''def _machine_unit_identity(current: _Node) -> str | None:\n    coupon = _compact(current.attrs.get("data-coupon-id", ""))\n    if coupon:\n        return f"coupon:{coupon}"\n    promo = _compact(current.attrs.get("data-promocode", "") or current.attrs.get("data-promo-code", ""))\n    if promo:\n        return f"promo:{promo}"\n    return None\n\n\ndef _unique_coupon_identity_count(node: _Node) -> int:\n    # One DOM node may expose both coupon-id and promo value for the same offer.\n    # Prefer coupon-id when present, otherwise promo value. Repeated copies of the\n    # same identity stay one unit; sibling distinct promo values split a wrapper.\n    values = {value for current in node.walk() if (value := _machine_unit_identity(current))}\n    if values:\n        return len(values)\n    return _count_descendants(node, _has_machine_marker)\n\n\ndef _has_multi_machine_ancestor(node: _Node) -> bool:\n    current = node.parent\n    while current is not None and current.tag not in {"document", "html", "body"}:\n        if _unique_coupon_identity_count(current) > 1:\n            return True\n        current = current.parent\n    return False''',
)
replace_once(
    '''                node for node in nodes\n                if _is_action_node(node)\n                and not _is_offer_id_action(node)\n                and not _has_promo_value_marker(node)\n            ]),''',
    '''                node for node in nodes\n                if _is_action_node(node)\n                and not _is_offer_id_action(node)\n                and not _has_promo_value_marker(node)\n                and not _has_multi_machine_ancestor(node)\n            ]),''',
)
replace_once(
    '''def _fallback_score(node: _Node, text: str) -> tuple[float, int] | None:\n    if len(text) < 6 or not _OFFER_SIGNAL_RE.search(text):''',
    '''def _fallback_score(node: _Node, text: str) -> tuple[float, int] | None:\n    if _is_navigation_node(node):\n        return None\n    if len(text) < 6 or not _OFFER_SIGNAL_RE.search(text):''',
)

PATH.write_text(text, encoding="utf-8")
