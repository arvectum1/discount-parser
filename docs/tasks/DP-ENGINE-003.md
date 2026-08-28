# DP-ENGINE-003 — Acquisition/fetch + rendered-page layer

**Status:** implemented

## Goal

Add the transport layer that turns a URL into the `RawAsset.html` / `RawAsset.text` input introduced by `DP-ENGINE-002`, while preserving the Data Platform rule that normal operation should not require per-site manual transport configuration.

The default acquisition path is progressive:

1. try the cheap static HTTP path;
2. keep it when it is useful;
3. automatically invoke a browser renderer only when HTTP failed or the response looks like an obvious client-rendered shell;
4. normalize the selected snapshot into `RawAsset` with provenance;
5. pass that asset unchanged to the existing candidate-discovery/extraction layer.

## In scope

- `AcquisitionRequest`, `PageSnapshot`, `AcquisitionAttempt` and `AcquisitionResult` contracts;
- `HTTPTransport` and `PageRenderer` protocols;
- zero-dependency `UrllibHTTPTransport`;
- optional lazy `PlaywrightRenderer`;
- `AcquisitionEngine` orchestration;
- `RenderMode.AUTO`, `RenderMode.NEVER` and `RenderMode.ALWAYS`;
- automatic rendered-page fallback after HTTP failure;
- automatic rendered-page fallback for obvious JavaScript/client-rendered shells;
- bounded payload size and request timeout;
- HTTP(S)-only requested and final URL contract;
- URL-embedded credential rejection;
- redirect/final-URL provenance;
- charset-aware decoding;
- HTML-vs-text normalization to `RawAsset`;
- acquisition metadata on `RawAsset.metadata` without polluting source `attributes`;
- retention of a successful static snapshot when an optional render attempt fails;
- deterministic URL-derived asset id when the caller does not supply one;
- regression-oriented fake transport/renderer tests that require no network or browser binary.

## Default acquisition policy

### `AUTO`

`AUTO` is the normal production mode.

- HTTP is attempted first.
- A normal useful static page is returned immediately.
- Browser rendering is requested after an HTTP failure.
- Browser rendering is also requested when generic page heuristics detect an obvious client-rendered shell, such as a near-empty `root`/`app` container with multiple scripts or an explicit JavaScript-required message.
- No CSS selector, site name or domain rule participates in this decision.
- If HTTP succeeded but the optional browser render fails, the static snapshot is retained and the failure is recorded as a warning instead of losing all evidence.

`AcquisitionEngine()` creates a lazy `PlaywrightRenderer` by default. Playwright itself is imported only when a render is actually needed. Applications that install the `browser` extra and a Playwright browser therefore receive automatic browser fallback without wiring another adapter.

### `NEVER`

Only HTTP is allowed. This mode is useful for restricted runtimes, diagnostics and deterministic acceptance tests.

### `ALWAYS`

HTTP is skipped and the renderer is used directly. This is an explicit override for callers that already know a rendered document is required; it is not intended as per-site customer configuration.

## Provenance

Every successful acquisition produces `RawAsset.metadata["acquisition"]` with:

- requested URL;
- final URL after redirects/navigation;
- successful method;
- rendered/static flag;
- HTTP status;
- content type.

`AcquisitionResult.attempts` separately records successful and failed attempts together with fallback reasons. This makes transport behavior diagnosable without asking an operator to inspect page structure.

## Safety / resource boundaries

The baseline rejects:

- non-HTTP(S) initial URLs;
- URL-embedded credentials;
- non-HTTP(S) final URLs returned by custom adapters/redirects;
- responses larger than `max_bytes`;
- HTTP/browser statuses >= 400.

Timeout and payload-size controls are request-level inputs. These are transport/resource controls, not domain extraction configuration.

## Human participation rule

No new normal-operation human step is introduced.

An operator/customer is not asked to:

- decide whether a site needs HTTP or browser mode;
- inspect JavaScript bundles;
- identify DOM nodes/selectors;
- specify redirect targets;
- copy page source manually.

`AUTO` makes the static-vs-rendered decision. Human confirmation remains exclusively in the `DP-ENGINE-001` extraction decision path for weak/conflicting field candidates.

## Optional browser dependency

The core HTTP path has no third-party runtime dependency. Browser rendering is an optional extra:

```bash
python -m pip install -e '.[browser]'
playwright install chromium
```

If rendering is recommended but unavailable/fails after a successful static fetch, the static snapshot is retained with a warning. If HTTP failed as well, acquisition fails explicitly with both failure contexts.

## Explicit non-goals

`DP-ENGINE-003` does **not** implement:

- login/session/cookie orchestration;
- CAPTCHA or anti-bot bypass;
- proxy rotation;
- robots/policy/compliance decision logic;
- per-site browser scripts or selectors;
- screenshot/OCR/VLM extraction;
- network retry/backoff queues;
- persistent browser pools;
- distributed acquisition workers;
- reviewer-learning or site-profile learning;
- Arvectum OS scheduling/deployment.

Those capabilities should be introduced as governed layers/adapters rather than weakening the domain-neutral acquisition contract.

## Acceptance evidence

The local acquisition harness verifies:

- rich static HTML stays on HTTP only;
- an obvious client-rendered shell invokes the renderer automatically;
- HTTP failure invokes the renderer automatically in `AUTO`;
- `NEVER` does not invoke the renderer;
- `ALWAYS` skips HTTP;
- renderer failure retains a successful static snapshot with a warning;
- dual HTTP/browser failure is explicit;
- renderer-unavailable handling is explicit;
- non-HTTP(S) input and URL-embedded credentials are rejected;
- custom adapters cannot bypass the response-size guard;
- text content becomes `RawAsset.text`;
- final redirect URL and requested URL are preserved as provenance;
- URL-derived asset ids are deterministic;
- non-HTTP(S) final URLs are rejected;
- declared charset is honored;
- an explicit JavaScript-required page triggers rendering.

Local `DP-ENGINE-003` harness: **16 tests passed**.

The change to `RawAsset` is backward compatible: `metadata` is a defaulted field and existing `DP-ENGINE-001/002` constructors do not require modification.
