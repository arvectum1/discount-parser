from __future__ import annotations

from pathlib import Path

path = Path("arvectum_data/engine/html_records.py")
text = path.read_text(encoding="utf-8")
old = '''def _is_benefit_heading(node: _Node) -> bool:\n    return (\n        node.tag in {"h2", "h3", "h4"}\n        and not _is_navigation_node(node)\n        and not _CONTROL_ACTION_RE.search(node.text())\n        and bool(_BENEFIT_HEADING_RE.search(node.text()))\n    )\n'''
new = '''def _is_collection_heading(node: _Node) -> bool:\n    """Identify a wrapper heading that labels several sibling offers.\n\n    A real offer heading may share a card with one reveal/action. A non-linked\n    heading whose surrounding container immediately exposes several independent\n    offer actions is instead collection chrome (for example ``active discounts``\n    above a list of merchant links) and must not become its own business record.\n    """\n\n    parent = node.parent\n    if parent is None or parent.tag in {"article", "li"}:\n        return False\n    if _parent_link(node)[0]:\n        return False\n    sibling_actions = 0\n    for sibling in parent.children:\n        if sibling is node:\n            continue\n        for current in sibling.walk():\n            if _is_action_node(current):\n                sibling_actions += 1\n                if sibling_actions > 1:\n                    return True\n    return False\n\n\ndef _is_benefit_heading(node: _Node) -> bool:\n    return (\n        node.tag in {"h2", "h3", "h4"}\n        and not _is_navigation_node(node)\n        and not _CONTROL_ACTION_RE.search(node.text())\n        and not _is_collection_heading(node)\n        and bool(_BENEFIT_HEADING_RE.search(node.text()))\n    )\n'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one heading predicate, got {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
