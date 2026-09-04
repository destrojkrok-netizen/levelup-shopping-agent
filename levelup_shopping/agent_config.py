"""The levelup.peoples deployment's config and credentials; the only place the service
reads deployment knobs from the environment."""

from __future__ import annotations

import os

from shopping_agent import ShoppingAgentConfig
from shopping_agent.config import ShoppingAgentConfig as _Defaults

from .shopify import DEFAULT_API_VERSION, ShopifyCredentials

_defaults = _Defaults()

DOMAIN_SEARCH_NOTES = (
    "The catalog is digital downloads (Notion templates for habits, discipline, and "
    "productivity), delivered instantly after payment; nothing ships. Search matches "
    'titles, descriptions, and tags; a tag such as "habit tracker" or "13 virtues" '
    "can be passed as a filter attribute."
)


def build_config() -> ShoppingAgentConfig:
    return ShoppingAgentConfig(
        brand_name="Levelup",
        assistant_name="Levelup Assistant",
        brand_voice="direct, encouraging, and plain about what each template does",
        domain_search_notes=DOMAIN_SEARCH_NOTES,
        # Systems: search and details are the floor. Cart, orders live; policies and
        # fulfillment exist but are not wired yet (backend.py raises). The store sells
        # digital downloads only, so enable_fulfillment=False is the likely final answer.
        enable_cart=True,
        enable_orders=True,
        enable_policies=True,
        enable_fulfillment=True,
        # Digital goods: one copy per template is the sensible cap.
        max_quantity_per_item=5,
        # Lexicon additions (assigning replaces the defaults, so extend them).
        policy_intent_terms=(
            *_defaults.policy_intent_terms,
            "license",
            "licence",
            "commercial use",
            "resell",
            "duplicate",
            "download link",
            "re-download",
        ),
        order_intent_terms=(
            *_defaults.order_intent_terms,
            "download",
            "downloads",
            "purchase",
            "purchases",
            "receipt",
        ),
        product_id_patterns=(
            *_defaults.product_id_patterns,
            r"\bNOTION-[A-Z0-9]{3,16}\b",  # SKUs such as NOTION-13VIRTUES
            r"gid://shopify/(?:Product|ProductVariant)/\d+",
        ),
    )


def build_credentials() -> ShopifyCredentials:
    return ShopifyCredentials(
        store_domain=os.environ.get("SHOPIFY_STORE_DOMAIN", "levelup-peoples.myshopify.com"),
        admin_token=os.environ.get("SHOPIFY_ADMIN_TOKEN") or None,
        storefront_token=os.environ.get("SHOPIFY_STOREFRONT_TOKEN") or None,
        api_version=os.environ.get("SHOPIFY_API_VERSION", DEFAULT_API_VERSION),
    )


def dev_customer_id() -> str:
    """The development principal bound at session start. TODO: replace with the id the
    storefront's sign-in resolves; a guest who signs in starts a new session."""
    return os.environ.get("DEV_CUSTOMER_ID", "guest")
