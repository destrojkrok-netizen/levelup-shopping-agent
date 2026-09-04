# Evals

No harness yet: cases follow the shape in the commerce-evals skill and are graded by hand or
by the runner `/author-commerce-evals` builds. `tests/test_smoke.py` checks every file here
parses and uses the catalog's real ids.

- `search-discovery.json` — nine starter cases; those needing an out-of-stock or poisoned
  listing carry `skip` until an eval-only overlay fixture exists.

Catalog ids (2026-09-04):

- `gid://shopify/ProductVariant/55146976870742` — The 13 Virtues Tracker, 12.90 USD
- `gid://shopify/ProductVariant/55158541910358` — Habit Tracker, 12.00 USD
