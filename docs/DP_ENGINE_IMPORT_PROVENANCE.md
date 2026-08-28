# DP Engine import provenance

**Target repository:** `arvectum1/discount-parser`

**Target base:** `discount-parser/main` at `af0521a7a5598440d71ff13e60181764e4d86fe6` (`DP-CUST-017`).

**Source repository:** `arvectum1/data-platform`

**Source snapshot:** `7b33e445cb93e0dc4088b3a31b5dc53c80823f2d` (`DP-ENGINE-011`).

The source snapshot contains the cumulative implementation of `DP-ENGINE-001` through `DP-ENGINE-011`, developed and reviewed through Data Platform PRs on 2026-08-28.

## Imported scope

- `data-platform/src/arvectum_data/**` -> `discount-parser/arvectum_data/**`;
- Data Platform engine regressions -> `discount-parser/tests/dp_engine/**`;
- `data-platform/docs/tasks/DP-ENGINE-001.md` through `DP-ENGINE-011.md` -> `discount-parser/docs/tasks/**`.

Data Platform repository-level README, license, GitHub workflows, mirror configuration and project `pyproject.toml` are intentionally not imported.

## Integration adaptations

Production files under `arvectum_data/**` are copied byte-for-byte from the source snapshot. The target packaging configuration is extended to discover the top-level `arvectum_data` package because Discount Parser uses repository-root package discovery.

During the first target regression run, `tests/test_orchestration.py` exposed pre-existing source test debt: its `FakeExtractionResult` predated the required `ExtractionResult.decisions` field. The imported test double is therefore given `decisions = {}` so it conforms to the production interface. No production engine behavior is changed by this adaptation.

## History and ownership boundary

This is a snapshot port rooted in Discount Parser history, not an unrelated-history merge. The source repository and exact source SHA remain recorded as provenance. Current customer/product integration work is owned by Discount Parser. Data Platform remains intact as reusable-engine provenance and may receive explicitly promoted domain-neutral improvements later.

## Post-import canonical continuation

The Data Platform snapshot is provenance for **DP-ENGINE-001..011 only**.

Starting with **DP-ENGINE-012**, new product-development commits, task contracts and regression evidence are authored canonically in `arvectum1/discount-parser` from the reconciled Discount Parser `main` line.

Future reusable improvements may be explicitly promoted/reconciled back to `arvectum1/data-platform`, but Data Platform is not the source of truth for post-import product-specific DP Engine work unless a later reconciliation task states otherwise.

## Readiness boundary

Import and regression PASS do not by themselves mean the engine is wired into the customer production runtime. Runtime wiring, replacement/adaptation of legacy source-specific paths, browser packaging and customer acceptance remain separate governed integration work.
