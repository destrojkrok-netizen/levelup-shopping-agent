# levelup-shopping-agent

Shopping assistant for the levelup.peoples Shopify store on the anthropics/commerce-agents
shopping agent. Lint: `ruff check . && ruff format --check .`; tests: `pytest`.

## Commerce agent decision record

Scaffolded 2026-09-04 by `/scaffold-commerce-agent` with every interview default.

- **Role:** shopping agent only.
- **Layout:** Python; the packages are imported, pinned in `requirements.txt` at reference
  commit `fd4d59224ab96b43c6dc6888207c67b3bd5a24cf` (`anthropics/commerce-agents`, main,
  2026-09). Module `levelup_shopping/`; `sessions.py` and `host.py` are copies of the
  reference's `examples/demo_common/`. Lint via `ruff.toml`, tests via `pytest.ini`.
- **Shell and client:** its own FastAPI service on the Messages API runtime
  (`ShoppingAgent`), `AsyncAnthropic` from the SDK's default credential chain
  (`ANTHROPIC_API_KEY`). Models: the config defaults (`claude-sonnet-5` for the turn loop,
  `claude-haiku-4-5-20251001` for memory extraction). Assumed: the Anthropic API directly.
- **Identity:** a fixed development principal bound at session start from
  `DEV_CUSTOMER_ID` (a Shopify customer GID, default `guest`). Source to replace it: the
  storefront's own sign-in (TODO in `main.py` `start_session`). A guest is a principal;
  a guest who signs in starts a new session. Credentials: service tokens the backend
  holds (`SHOPIFY_ADMIN_TOKEN`, `SHOPIFY_STOREFRONT_TOKEN`), attached server-side, never
  on the session or in a tool argument. Clock: the server's (`datetime.now()`); pass the
  customer's IANA `timezone` on the session context once the storefront supplies it.
- **Backend methods** (`backend.py`, `ShopifyStorefrontBackend`):

  | System | Wiring | Method(s) | Config |
  |---|---|---|---|
  | catalog and search | live, Admin GraphQL | `search_products`, `get_product_details` | — |
  | cart | live, Storefront API cart | `get_cart`, `add_to_cart`, `update_cart_item`, `remove_from_cart` | `enable_cart=True` |
  | profile | live, Admin `customer` | `get_preferences` | — |
  | orders | live, Admin `orders(query: customer_id:…)` | `get_orders`, `get_order` | `enable_orders=True` |
  | policy content | not wired (raises `NotWired`, tool answers unavailable) | `search_policies` | `enable_policies=True` |
  | fulfillment | not wired (same) | `get_fulfillment_options` | `enable_fulfillment=True` |

  Flag: the store sells digital downloads only, so fulfillment is arguably *absent*
  (`enable_fulfillment=False`, which removes the tool). Left on per the default; decide
  when wiring. Guest reads of orders raise `SignInRequired`, which
  `LevelupToolExecutor.domain_error` turns into "ask the customer to sign in".
- **Sessions:** the reference in-memory `SessionStore`, written back at request end
  (dependency) and stream end (`stream_turn`). TODO: subclass with the six storage
  methods over a shared store; the backend's Storefront cart-id map (`_carts`) moves
  there too. Until then, one uvicorn worker.
- **Posture:** single store.
- **Surfaces:** one UI, renderer mode `components`, no existing cards. Component table
  (each is a new card; payload models in `shopping_agent/tools/presentation.py`):
  `present_products` → products; `present_comparison` → comparison; `present_plan` → plan;
  `present_guide` → guide; `present_order_status` → order_status; `checkout` → checkout;
  `present_suggestions` → chips on the composer; `present_disclosure` → not registered
  (`enable_disclosures=False`). Checkout handoff: the Storefront cart's `checkoutUrl`,
  returned by `checkout_handoff` (hosted checkout; the model never sees the URL). No
  frontend yet: start from the reference's `examples/retail/storefront-web` over
  `examples/web-shared`.
- **v1 index:** indexed `search-discovery`, `planning-goals`. Copied, unindexed in
  `skills/_staged/`: `purchase-research` (opens with `search_policies`, not wired),
  `customer-care` (needs policy content), `memory-personalization` (no real principal yet).
  `/add-commerce-flow` moves a flow back up.
- **Gates:** fencing, provenance, quantity caps (`max_quantity_per_item=5`,
  `max_cart_lines=100`), grounding gates (policy, order, catalog), and the
  prompt-stability test all come with the imported executor and runtime, unchanged
  (commerce-trust-safety).
- **Memory:** `enable_memory=True`, `memory_retention_days` and
  `memory_blocked_patterns` default. Store: `JsonFileMemoryStore` at
  `data/.memory-store.json` (gitignored).
- **Domain (question 10):** digital downloads (Notion templates: habit tracker, 13
  Virtues tracker), vendor Levelup, currency USD (the Admin API reports USD; an earlier
  note said SGD — USD is authoritative), nearest example `examples/retail/`.
  (a) No options today: every product has one variant ("Default Title"), served as a
  plain record under the **variant GID**; a future multi-variant product becomes a family
  under the product GID (mapping in `backend.py` docstring). Largest family: 1.
  (b) One price per item, no request-dependent pricing, one seller.
  Digital stock is untracked, so `in_stock` follows `status == ACTIVE` unless Shopify
  tracks inventory for the variant.
- **`domain_search_notes`:** see `agent_config.py` `DOMAIN_SEARCH_NOTES`.
- **Lexicon additions** (`agent_config.py`, extended not replaced): policy terms
  `license`, `licence`, `commercial use`, `resell`, `duplicate`, `download link`,
  `re-download`; order terms `download(s)`, `purchase(s)`, `receipt`; id patterns
  `NOTION-…` SKUs and Shopify GIDs.
- **Assumptions for skipped questions:** 2 Python; 3 own service, Messages API, in-memory
  store; 4 Anthropic API; 6 dev principal, server clock; 7 single store; 9 one UI,
  `components`, hosted checkout URL (the Storefront cart made this cheaper than the TODO
  the default calls for); 11 index the subset the wired systems support; 12 memory on.
- **Reference clone:** `~/.claude/plugins/marketplaces/claude-commerce-agents` (the
  plugin marketplace checkout of the reference at the commit above); `managed-agents/`
  and `scripts/deploy_managed_agent.sh` are read from there if the hosted path is ever
  taken.
