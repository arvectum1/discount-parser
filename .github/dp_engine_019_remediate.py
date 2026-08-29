from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement, got {count}")
    p.write_text(text.replace(old, new), encoding="utf-8")


path = "arvectum_data/engine/html_records.py"
replace_once(
    path,
    '''def _is_action_node(node: _Node) -> bool:\n    if node.tag not in {"a", "button"}:\n        return False\n    href = node.attrs.get("href", "")\n    if "offer_id=" in href:\n        return True\n    text = node.text()\n    if _ACTION_RE.search(text):\n        return True\n    # Some production sites label the actionable element with the benefit itself\n    # rather than an imperative verb. Keep this generic and bounded: only links\n    # with an href and buttons qualify, and very long/navigation-like text is out.\n    return bool(\n        text\n        and len(text) <= 280\n        and _OFFER_SIGNAL_RE.search(text)\n        and (node.tag == "button" or bool(href))\n    )\n''',
    '''def _is_navigation_node(node: _Node) -> bool:\n    current: _Node | None = node\n    while current is not None:\n        if current.tag in {"nav", "header", "footer"}:\n            return True\n        current = current.parent\n    return False\n\n\ndef _is_action_node(node: _Node) -> bool:\n    if node.tag not in {"a", "button"}:\n        return False\n    href = node.attrs.get("href", "")\n    if "offer_id=" in href:\n        return True\n    text = node.text()\n    if _ACTION_RE.search(text):\n        return not _is_navigation_node(node)\n    # Some production sites label the actionable element with the benefit itself\n    # rather than an imperative verb. Keep this generic and bounded, and do not\n    # promote global navigation/chrome into business records.\n    return bool(\n        text\n        and len(text) <= 280\n        and not _is_navigation_node(node)\n        and _OFFER_SIGNAL_RE.search(text)\n        and (node.tag == "button" or bool(href))\n    )\n''',
)
replace_once(
    path,
    '''        groups: tuple[tuple[int, str, float, Sequence[_Node]], ...] = (\n            (5, "machine", 0.99, [node for node in nodes if _has_machine_marker(node)]),\n            (5, "action", 0.99, [node for node in nodes if _is_offer_id_action(node)]),\n            (4, "heading", 0.97, [node for node in nodes if _is_linked_benefit_heading(node)]),\n            (3, "action", 0.96, [\n                node for node in nodes if _is_action_node(node) and not _is_offer_id_action(node)\n            ]),\n            (2, "heading", 0.94, [\n                node for node in nodes if _is_benefit_heading(node) and not _is_linked_benefit_heading(node)\n            ]),\n        )\n''',
    '''        groups: tuple[tuple[int, str, float, Sequence[_Node]], ...] = (\n            # Exact-card arbitration: offer-id is strongest; a linked benefit\n            # heading carries the canonical target link; an explicit action keeps\n            # its href while still exposing machine data from the card.\n            (9, "action", 0.99, [node for node in nodes if _is_offer_id_action(node)]),\n            (8, "heading", 0.97, [node for node in nodes if _is_linked_benefit_heading(node)]),\n            (7, "action", 0.96, [\n                node for node in nodes if _is_action_node(node) and not _is_offer_id_action(node)\n            ]),\n            (6, "machine", 0.99, [node for node in nodes if _has_machine_marker(node)]),\n            (5, "heading", 0.94, [\n                node for node in nodes if _is_benefit_heading(node) and not _is_linked_benefit_heading(node)\n            ]),\n        )\n''',
)
