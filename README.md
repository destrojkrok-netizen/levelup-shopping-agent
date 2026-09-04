# levelup-shopping-agent

A shopping assistant for the levelup.peoples Shopify store, built on the
[anthropics/commerce-agents](https://github.com/anthropics/commerce-agents) shopping agent
(Messages API runtime). Design decisions live in `CLAUDE.md` under
"Commerce agent decision record".

## Run

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # add ANTHROPIC_API_KEY, SHOPIFY_ADMIN_TOKEN, SHOPIFY_STOREFRONT_TOKEN
.venv/bin/uvicorn levelup_shopping.main:app --port 8000
```

One worker only: sessions and the Storefront cart ids live in process memory.

```bash
curl -s -X POST localhost:8000/api/session
curl -N -X POST localhost:8000/api/chat -H 'X-Session-Id: <id>' \
  -H 'Content-Type: application/json' -d '{"message":"what habit trackers do you have?"}'
```

## Check

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest
```

## Layout

```
levelup_shopping/
  backend.py       ShopifyStorefrontBackend (Admin API catalog/customer/orders, Storefront API cart)
  shopify.py       the two GraphQL clients and their documents
  executor.py      LevelupToolExecutor: SignInRequired -> "ask the customer to sign in"
  agent_config.py  ShoppingAgentConfig, credentials, the dev principal
  sessions.py      session record and store (reference copy; subclass for a shared store)
  host.py          app, SSE turn streaming (reference copy)
  main.py          routes
  skills/          indexed flows; skills/_staged/ holds the flows not yet in the index
evals/  tests/
```
