from __future__ import annotations

from pathlib import Path

PATH = Path("arvectum_data/engine/html_records.py")
text = PATH.read_text(encoding="utf-8")
old = '''            (12, "action", 0.99, [node for node in nodes if _is_offer_id_action(node)]),\n            (11, "machine", 0.995, [node for node in nodes if _has_promo_value_marker(node)]),\n            (10, "heading", 0.97, [node for node in nodes if _is_linked_benefit_heading(node)]),'''
new = '''            (12, "action", 0.99, [node for node in nodes if _is_offer_id_action(node)]),\n            # A value-bearing action is already the individual card anchor and\n            # preserves action semantics. Promo-only machine nodes outrank only\n            # surrounding/broad actions.\n            (11, "action", 0.995, [\n                node for node in nodes\n                if _has_promo_value_marker(node) and _is_action_node(node)\n            ]),\n            (10, "machine", 0.995, [\n                node for node in nodes\n                if _has_promo_value_marker(node) and not _is_action_node(node)\n            ]),\n            (9, "heading", 0.97, [node for node in nodes if _is_linked_benefit_heading(node)]),'''
if text.count(old) != 1:
    raise SystemExit(f"expected one arbitration target, got {text.count(old)}")
text = text.replace(old, new, 1)
# Shift lower priorities only for readability; ordering is tuple order + score.
text = text.replace('(9, "action", 0.96, [', '(8, "action", 0.96, [', 1)
text = text.replace('(8, "heading", 0.95, [', '(7, "heading", 0.95, [', 1)
text = text.replace('(7, "machine", 0.99, [', '(6, "machine", 0.99, [', 1)
PATH.write_text(text, encoding="utf-8")
