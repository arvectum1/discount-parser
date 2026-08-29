# DP-ENGINE-019 — live 5-source production acceptance & remediation

## Goal

Exercise the shipped hybrid Discount Parser runtime against all five production sources on the live internet, remediate generic multi-record extraction using domain-generic structural/semantic rules, and separate customer-path safety from generic-engine adapter-retirement readiness.

This task does **not** require legacy adapters to be retired. The safety contract remains: generic output is used only when the whole discovered record set is READY and strict safe-superset parity against the current adapter passes; otherwise the adapter result is returned automatically.

## Production acceptance

Final live acceptance workflow run: `33267337587` (GitHub-hosted Ubuntu runner, 2026-08-29).

| source | customer safe | fetched | errors | selected | decoded | generic pages | legacy pages | parity failures | engine retirement ready |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| promokood | yes | 122 | 0 | 40 | 40 | 0 | 40 | 40 | no |
| promokodik | yes | 108 | 0 | 40 | 40 | 0 | 40 | 40 | no |
| berikod | yes | 550 | 0 | 32 | 32 | 0 | 32 | 32 | no |
| promokodi_net_ru | yes | 120 | 0 | 40 | 39 | 0 | 39 | 39 | no |
| promko | yes | 46 | 0 | 10 | 10 | 0 | 10 | 10 | no |

Acceptance result for the current customer product path: **PASS (5/5 sources customer-safe, 0 collection errors).**

Generic adapter-retirement result: **NOT READY (0/5 sources).** This is intentionally not converted into a false PASS. Current adapters remain the automatic production fallback/oracle until future live parity evidence satisfies the existing retirement policy.

## Remediation implemented

### Structural record discovery

`SemanticHTMLRecordProvider` was hardened using patterns observed across the five live sources without host checks, CSS/XPath selectors, or operator configuration:

- mixed machine/action/heading offer signals are arbitrated per card rather than selecting one signal type for the whole page;
- wrappers containing multiple distinct offer identities are prevented from becoming one record;
- repeated copies of the same coupon identity inside a card are treated as one offer unit;
- explicit promo-value markers can identify an individual promotion without allowing a broad surrounding action to swallow sibling offers;
- offer-id/value-bearing actions preserve action semantics when the action itself is the individual card anchor;
- validity/status text can bound a single promotion card;
- navigation/header/footer/aside and pseudo/contact/control links are excluded from business records;
- bare benefit-labelled filter buttons are not treated as offers;
- deterministic overlap arbitration reduces parent/child representations of one business card.

### Discount offer semantics

`DiscountOfferCandidateProvider` was hardened with promotion-domain rules that remain source-neutral:

- status/counter `<strong>` text is not accepted as a merchant;
- heading-led records derive merchant only from semantic heading evidence rather than surrounding UI text;
- action cards can derive merchant from the human-readable `от <merchant>` segment;
- promo-code scanning starts after the semantic heading even when UI text precedes it;
- digit-only currency/percent values are rejected as inferred promo codes;
- explicit promo-value machine records use business-field identity rather than blindly substituting routing/widget identifiers.

## Safety invariants

- The DP-016 `all records READY` usability contract is restored and retained.
- Strict safe-superset parity is unchanged.
- No generic mismatch is allowed into the customer output; live mismatches automatically returned legacy adapter output.
- No per-site CSS/XPath/DOM configuration was added.
- No customer/operator action is required for fallback or parity decisions.
- Temporary live-probe/remediation workflows and scripts used while developing DP-ENGINE-019 are not part of the final change set.

## Regression evidence

Before final live acceptance, the targeted remediation suite plus canonical generic fixture parity passed, including:

- semantic HTML record regressions;
- DP-019 live-root-cause regressions;
- five-source canonical fixture parity;
- production hybrid source runtime regressions.

The final PR must additionally pass the repository's normal cross-platform CI, delivery build, Windows reproducibility, and installed Windows acceptance gates before merge.

## Outcome

DP-ENGINE-019 establishes that the current Discount Parser production path is safe on all five live sources while preserving an honest distinction between product delivery readiness and future removal of legacy adapters. Full generic-native parity remains follow-up work and is not allowed to delay or weaken the customer-safe delivery path.
