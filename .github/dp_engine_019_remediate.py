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
    '''def _is_action_node(node: _Node) -> bool:\n    if node.tag not in {"a", "button"}:\n        return False\n    href = node.attrs.get("href", "")\n    if "offer_id=" in href:\n        return True\n    return bool(_ACTION_RE.search(node.text()))\n''',
    '''def _is_action_node(node: _Node) -> bool:\n    if node.tag not in {"a", "button"}:\n        return False\n    href = node.attrs.get("href", "")\n    if "offer_id=" in href:\n        return True\n    text = node.text()\n    if _ACTION_RE.search(text):\n        return True\n    # Some production sites label the actionable element with the benefit itself\n    # rather than an imperative verb. Keep this generic and bounded: only links\n    # with an href and buttons qualify, and very long/navigation-like text is out.\n    return bool(\n        text\n        and len(text) <= 280\n        and _OFFER_SIGNAL_RE.search(text)\n        and (node.tag == "button" or bool(href))\n    )\n\n\ndef _is_offer_id_action(node: _Node) -> bool:\n    return node.tag == "a" and "offer_id=" in node.attrs.get("href", "")\n\n\ndef _is_linked_benefit_heading(node: _Node) -> bool:\n    if not _is_benefit_heading(node):\n        return False\n    href, _ = _parent_link(node)\n    return bool(href)\n''',
)
replace_once(
    path,
    '''        anchored = self._strong_records(nodes)\n        if not anchored:\n            anchored = self._heading_records(nodes)\n        if not anchored:\n            anchored = self._fallback_records(nodes)\n''',
    '''        anchored = self._mixed_records(nodes)\n        if not anchored:\n            anchored = self._fallback_records(nodes)\n''',
)
replace_once(
    path,
    '''    def _strong_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:\n        actions = [node for node in nodes if _is_action_node(node)]\n        machine = [node for node in nodes if _has_machine_marker(node)]\n        action_records = self._dedupe_cards(actions, kind="action", confidence=0.97)\n        machine_records = self._dedupe_cards(machine, kind="machine", confidence=0.99)\n        result = list(action_records)\n        for proposed in machine_records:\n            if any(\n                proposed.card is existing.card\n                or _is_descendant(proposed.card, existing.card)\n                or _is_descendant(existing.card, proposed.card)\n                for existing in action_records\n            ):\n                continue\n            result.append(proposed)\n        return result\n\n    def _heading_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:\n        anchors = [node for node in nodes if _is_benefit_heading(node)]\n        return self._dedupe_cards(anchors, kind="heading", confidence=0.94)\n''',
    '''    def _mixed_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:\n        """Collect independent record signals per card instead of per page.\n\n        Real pages may mix machine coupon ids, offer-id links, explicit actions\n        and linked benefit headings. A page-wide winner discards valid records.\n        Here every generic signal proposes a bounded card; proposals resolving to\n        the exact same structural card are arbitrated by evidence strength while\n        distinct cards survive for the strict parity gate to validate.\n        """\n\n        groups: tuple[tuple[int, str, float, Sequence[_Node]], ...] = (\n            (5, "machine", 0.99, [node for node in nodes if _has_machine_marker(node)]),\n            (5, "action", 0.99, [node for node in nodes if _is_offer_id_action(node)]),\n            (4, "heading", 0.97, [node for node in nodes if _is_linked_benefit_heading(node)]),\n            (3, "action", 0.96, [\n                node for node in nodes if _is_action_node(node) and not _is_offer_id_action(node)\n            ]),\n            (2, "heading", 0.94, [\n                node for node in nodes if _is_benefit_heading(node) and not _is_linked_benefit_heading(node)\n            ]),\n        )\n        proposed: list[tuple[int, _AnchoredRecord]] = []\n        for priority, kind, confidence, anchors in groups:\n            proposed.extend(\n                (priority, record)\n                for record in self._dedupe_cards(anchors, kind=kind, confidence=confidence)\n            )\n\n        # Exact structural-card arbitration only. Ancestor/descendant proposals are\n        # deliberately left visible: collapsing them without proof can hide a real\n        # sibling offer. Duplicate business identities are still deduplicated later,\n        # and DP-016 exact parity remains the final adoption authority.\n        by_path: dict[str, tuple[int, _AnchoredRecord]] = {}\n        for priority, record in proposed:\n            previous = by_path.get(record.card.path)\n            if previous is None or priority > previous[0]:\n                by_path[record.card.path] = (priority, record)\n        return [item[1] for item in by_path.values()]\n''',
)

tests = Path("tests/dp_engine/test_semantic_html_records.py")
text = tests.read_text(encoding="utf-8")
addition = r'''


def test_mixed_page_keeps_linked_heading_and_unrelated_action_record() -> None:
    result = _records(
        """
        <main>
          <a href='/heading'><h3>Промокод Alpha на август</h3><span>SAVE10</span></a>
          <article><h3>Скидка 20% в Beta</h3><a href='/action'>Открыть промокод</a></article>
        </main>
        """
    )
    assert len(result.records) == 2
    kinds = {record.asset.attributes["record_anchor_kind"] for record in result.records}
    assert kinds == {"heading", "action"}
    assert {record.asset.attributes["record_href"] for record in result.records} == {"/heading", "/action"}


def test_offer_word_link_is_action_signal_without_imperative_verb() -> None:
    result = _records(
        """
        <article>
          <strong>Demo Shop</strong>
          <a href='/deal'>Скидка 25% и промокод</a>
        </article>
        """
    )
    assert len(result.records) == 1
    attrs = result.records[0].asset.attributes
    assert attrs["record_anchor_kind"] == "action"
    assert attrs["record_action_href"] == "/deal"
'''
if "test_mixed_page_keeps_linked_heading_and_unrelated_action_record" not in text:
    tests.write_text(text + addition, encoding="utf-8")
