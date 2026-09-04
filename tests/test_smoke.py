"""Step 4 checks: the agent runs a turn on the scripted client over a stub backend, the
chat route stores the turn in the session, the backend maps Shopify shapes, and no
request field names a customer."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from commerce_common.memory import InMemoryMemoryStore
from commerce_common.testing import FakeClient, text_message
from fastapi.testclient import TestClient
from levelup_shopping.agent_config import build_config
from levelup_shopping.backend import ShopifyStorefrontBackend, SignInRequired
from levelup_shopping.executor import LevelupToolExecutor
from levelup_shopping.main import build_agent, create_app
from levelup_shopping.shopify import ShopifyCredentials
from shopping_agent import Cart, ShoppingSessionContext, ShoppingSessionState, UserPreferences

PROJECT = Path(__file__).resolve().parents[1]

SINGLE_VARIANT = {
    "id": "gid://shopify/Product/1",
    "title": "Habit Tracker — Notion Template",
    "handle": "habit-tracker-notion-template",
    "vendor": "Levelup",
    "productType": "Digital Download",
    "status": "ACTIVE",
    "tags": ["habit tracker", "notion template"],
    "description": "One page, your habits.",
    "descriptionHtml": "<p>One page, your habits.</p>",
    "featuredMedia": {"preview": {"image": {"url": "https://cdn.shopify.com/x.png"}}},
    "options": [{"name": "Title", "values": ["Default Title"]}],
    "priceRangeV2": {"minVariantPrice": {"amount": "12.0", "currencyCode": "USD"}},
    "variants": {
        "nodes": [
            {
                "id": "gid://shopify/ProductVariant/11",
                "title": "Default Title",
                "sku": "NOTION-HABIT",
                "price": "12.00",
                "inventoryQuantity": 0,
                "inventoryPolicy": "DENY",
                "inventoryItem": {"tracked": False},
                "selectedOptions": [{"name": "Title", "value": "Default Title"}],
                "image": None,
            }
        ]
    },
}

FAMILY = {
    **SINGLE_VARIANT,
    "id": "gid://shopify/Product/2",
    "title": "Planner",
    "options": [{"name": "Tier", "values": ["Basic", "Pro"]}],
    "variants": {
        "nodes": [
            {
                "id": "gid://shopify/ProductVariant/21",
                "title": "Basic",
                "sku": None,
                "price": "9.00",
                "inventoryQuantity": 0,
                "inventoryPolicy": "DENY",
                "inventoryItem": {"tracked": True},
                "selectedOptions": [{"name": "Tier", "value": "Basic"}],
                "image": None,
            },
            {
                "id": "gid://shopify/ProductVariant/22",
                "title": "Pro",
                "sku": None,
                "price": "19.00",
                "inventoryQuantity": 3,
                "inventoryPolicy": "DENY",
                "inventoryItem": {"tracked": True},
                "selectedOptions": [{"name": "Tier", "value": "Pro"}],
                "image": None,
            },
        ]
    },
}


class StubBackend(ShopifyStorefrontBackend):
    """The Shopify backend with its network replaced: mapping runs, no API is called."""

    def __init__(self) -> None:
        super().__init__(ShopifyCredentials("example.myshopify.com", None, None))

    async def currency(self) -> str:
        return "USD"

    async def search_products(self, session, query, filters=None, limit=8):
        return [self._map_product(SINGLE_VARIANT)]

    async def get_product_details(self, session, product_id):
        if product_id == "gid://shopify/Product/2":
            return self._map_product(FAMILY)
        return self._map_product(SINGLE_VARIANT)

    async def get_cart(self, session):
        return Cart(currency="USD")

    async def get_preferences(self, session):
        return UserPreferences(user_id=session.user_id, display_name="Dev")


def fake_agent(backend, *responses):
    """The agent on a scripted client; memory extraction off, since the fake has no
    ``messages.create`` for the post-turn extraction call."""
    return build_agent(
        backend,
        memory_store=InMemoryMemoryStore(),
        client=FakeClient(list(responses)),
        config=build_config().model_copy(update={"enable_memory": False}),
    )


def session(user_id: str = "guest") -> ShoppingSessionContext:
    return ShoppingSessionContext(session_id="s-1", user_id=user_id)


def test_single_variant_product_is_plain_under_variant_id() -> None:
    details = StubBackend()._map_product(SINGLE_VARIANT)
    assert details.product_id == "gid://shopify/ProductVariant/11"
    assert details.options == {} and details.variants == []
    assert details.price == 12.0 and details.in_stock  # untracked digital stock
    assert details.specs == {"sku": "NOTION-HABIT"}


def test_multi_variant_product_is_family_under_product_id() -> None:
    details = StubBackend()._map_product(FAMILY)
    assert details.product_id == "gid://shopify/Product/2"
    assert details.options == {"Tier": ["Basic", "Pro"]}
    assert [v.product_id for v in details.variants] == [
        "gid://shopify/ProductVariant/21",
        "gid://shopify/ProductVariant/22",
    ]
    assert details.variants[0].in_stock is False and details.variants[1].in_stock is True
    assert details.price == 19.0  # lowest in-stock variant
    assert details.variants[1].variant_of == "gid://shopify/Product/2"


async def test_guest_orders_ask_to_sign_in() -> None:
    backend = StubBackend()
    with pytest.raises(SignInRequired):
        await backend.get_orders(session("guest"))
    agent = fake_agent(backend)
    executor = LevelupToolExecutor(
        backend=backend,
        config=agent.config,
        skills=agent.skills,
        session=session("guest"),
        state=ShoppingSessionState(),
        memory=agent.memory,
    )
    outcome = await executor.execute("get_orders", {"limit": 3})
    assert "sign in" in outcome.result_text


async def test_turn_on_fake_client_yields_text() -> None:
    backend = StubBackend()
    agent = fake_agent(backend, text_message("hello"))
    events = [
        e
        async for e in agent.stream_turn(
            [{"role": "user", "content": "hi"}], session(), ShoppingSessionState()
        )
    ]
    assert any(e.type == "text_delta" for e in events)


def test_chat_route_stores_turn_in_session() -> None:
    backend = StubBackend()
    agent = fake_agent(backend, text_message("hello"))
    app = create_app(backend, agent)
    with TestClient(app) as client:
        started = client.post("/api/session").json()
        headers = {"X-Session-Id": started["session_id"]}
        response = client.post("/api/chat", json={"message": "hi"}, headers=headers)
        assert response.status_code == 200
        assert "event: text_delta" in response.text
        record = app.state.sessions.require(started["session_id"])
        roles = [m["role"] for m in record.messages]
        assert roles[:2] == ["user", "assistant"]


def test_no_request_names_a_customer() -> None:
    """commerce-trust-safety rule 12: after session start, requests carry the session id only."""
    source = (PROJECT / "levelup_shopping" / "main.py").read_text()
    bodies = re.findall(r"class \w+\(BaseModel\):\n((?:    .*\n)+)", source)
    for body in bodies:
        assert not re.search(r"\b(user_id|customer_id|customer|operator|merchant_id)\b", body)


def test_skills_index() -> None:
    staged = {p.name for p in (PROJECT / "levelup_shopping" / "skills" / "_staged").iterdir()}
    assert staged == {"purchase-research", "customer-care", "memory-personalization"}
    backend = StubBackend()
    agent = fake_agent(backend)
    assert agent.skills.names == ["planning-goals", "search-discovery"]
