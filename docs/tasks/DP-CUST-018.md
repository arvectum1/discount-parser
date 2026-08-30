# DP-CUST-018 — final customer delivery release after DP-ENGINE-019

## Goal

Turn the accepted DP-ENGINE-019 product state into the final customer-delivery release v0.1.16.

This task contains no intended behavior or engine change beyond release metadata.

## Release parameters

| field | value |
| --- | --- |
| source baseline | `a863223744da3b927aa57fa801c591d95b60d7a0` |
| previous product version | 0.1.15 |
| release version | 0.1.16 |
| DP-ENGINE range included | DP-ENGINE-001..019 |
| DP-ENGINE-019 live acceptance | customer-safe PASS 5/5 |

## Scope

- DP-ENGINE-001..019 are included in this release.
- DP-ENGINE-019 live acceptance established customer-safe PASS across all five shipped sources: promokood, promokodik, berikod, promokodi_net_ru, promko.
- Legacy adapters retained as automatic safety fallback/oracle. Generic adapter retirement explicitly NOT required for this release.
- No customer/operator selector configuration required.
- No per-site CSS/XPath/manual selector configuration.

## Changes in this release

- Version bump: 0.1.15 -> 0.1.16 in all authoritative product metadata.
- Updated files: pyproject.toml, packaging/windows/installer.iss, packaging/macos/build_dmg.sh, tests/test_release_version_sync.py, docs/CUSTOMER_USER_GUIDE_RU.md.
- No engine/runtime behavioral code changed.

## Validation

- Full test suite: PASS (623 tests)
- Compile check: PASS
- Database migration: PASS (migrations 0001-0010)
- Doctor smoke: PASS
- CLI smoke: PASS (all subcommands)
- Canonical CI: PASS (GitHub Actions)
- Windows reproducibility: PASS
- Windows installed acceptance: PASS

## Release gate

Release gate evidence collected via `scripts/release_gate.py` (DP-CI-003).
All three required gates (canonical_ci, windows_reproducibility, windows_installed_acceptance) must be PASS.
