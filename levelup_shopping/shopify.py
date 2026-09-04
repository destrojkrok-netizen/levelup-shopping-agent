"""Thin GraphQL clients for the two Shopify APIs the backend calls, each holding the one
service credential the host passes in. The Admin API serves the catalog, the customer
profile, and orders; the Storefront API owns carts, because the Admin API has no cart and
a Storefront cart carries the hosted checkout URL the ``checkout`` card hands off to.
Nothing here places an order or moves money.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_API_VERSION = "2025-10"


class ShopifyError(RuntimeError):
    """A GraphQL-level error from either API; the executor reports the tool unavailable."""


@dataclass(frozen=True)
class ShopifyCredentials:
    store_domain: str  # levelup-peoples.myshopify.com
    admin_token: str | None
    storefront_token: str | None
    api_version: str = DEFAULT_API_VERSION

    @property
    def admin_url(self) -> str:
        return f"https://{self.store_domain}/admin/api/{self.api_version}/graphql.json"

    @property
    def storefront_url(self) -> str:
        return f"https://{self.store_domain}/api/{self.api_version}/graphql.json"


class _GraphQLClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(base_url=url, headers=headers, timeout=timeout)
        self._url = url

    async def run(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._client.post("", json={"query": query, "variables": variables or {}})
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise ShopifyError(str(body["errors"])[:500])
        return body.get("data") or {}

    async def aclose(self) -> None:
        await self._client.aclose()


class AdminClient(_GraphQLClient):
    def __init__(self, credentials: ShopifyCredentials) -> None:
        if not credentials.admin_token:
            raise ShopifyError("SHOPIFY_ADMIN_TOKEN is not set")
        super().__init__(
            credentials.admin_url,
            {"X-Shopify-Access-Token": credentials.admin_token, "Content-Type": "application/json"},
        )


class StorefrontClient(_GraphQLClient):
    def __init__(self, credentials: ShopifyCredentials) -> None:
        if not credentials.storefront_token:
            raise ShopifyError("SHOPIFY_STOREFRONT_TOKEN is not set")
        super().__init__(
            credentials.storefront_url,
            {
                "X-Shopify-Storefront-Access-Token": credentials.storefront_token,
                "Content-Type": "application/json",
            },
        )


def raise_user_errors(payload: dict[str, Any] | None, what: str) -> dict[str, Any]:
    """Storefront cart mutations report business-rule failures as ``userErrors``."""
    if payload is None:
        raise ShopifyError(f"{what}: empty response")
    errors = payload.get("userErrors") or []
    if errors:
        raise ShopifyError(f"{what}: " + "; ".join(e.get("message", "") for e in errors)[:300])
    return payload


# -- Admin API documents ------------------------------------------------------------------

PRODUCT_FIELDS = """
  id
  title
  handle
  vendor
  productType
  status
  tags
  description(truncateAt: 240)
  descriptionHtml
  featuredMedia { preview { image { url } } }
  options { name values }
  priceRangeV2 { minVariantPrice { amount currencyCode } }
  variants(first: 60) {
    nodes {
      id
      title
      sku
      price
      availableForSale
      inventoryQuantity
      inventoryPolicy
      inventoryItem { tracked }
      selectedOptions { name value }
      image { url }
    }
  }
"""

SEARCH_PRODUCTS = f"""
query SearchProducts($query: String!, $first: Int!) {{
  products(first: $first, query: $query, sortKey: RELEVANCE) {{
    nodes {{ {PRODUCT_FIELDS} }}
  }}
}}
"""

GET_PRODUCT = f"""
query GetProduct($id: ID!) {{
  product(id: $id) {{ {PRODUCT_FIELDS} }}
}}
"""

GET_VARIANT_PRODUCT = f"""
query GetVariantProduct($id: ID!) {{
  productVariant(id: $id) {{ id product {{ {PRODUCT_FIELDS} }} }}
}}
"""

GET_CUSTOMER = """
query GetCustomer($id: ID!) {
  customer(id: $id) {
    id
    displayName
    numberOfOrders
    defaultAddress { city country }
    tags
  }
}
"""

ORDER_FIELDS = """
  id
  name
  createdAt
  cancelledAt
  displayFulfillmentStatus
  displayFinancialStatus
  currentTotalPriceSet { shopMoney { amount currencyCode } }
  fulfillments(first: 5) { trackingInfo { url } estimatedDeliveryAt }
  lineItems(first: 50) {
    nodes {
      title
      quantity
      variant { id product { id } selectedOptions { name value } }
      originalUnitPriceSet { shopMoney { amount } }
    }
  }
"""

LIST_ORDERS = f"""
query ListOrders($query: String!, $first: Int!) {{
  orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: true) {{
    nodes {{ {ORDER_FIELDS} }}
  }}
}}
"""

GET_ORDER = f"""
query GetOrder($id: ID!) {{
  order(id: $id) {{ customer {{ id }} {ORDER_FIELDS} }}
}}
"""

SHOP_CURRENCY = """
query ShopCurrency { shop { currencyCode } }
"""

# -- Storefront API documents ---------------------------------------------------------------

CART_FIELDS = """
  id
  checkoutUrl
  cost { subtotalAmount { amount currencyCode } }
  lines(first: 100) {
    nodes {
      id
      quantity
      merchandise {
        ... on ProductVariant {
          id
          title
          price { amount currencyCode }
          image { url }
          selectedOptions { name value }
          product { id title featuredImage { url } }
        }
      }
    }
  }
"""

CART_CREATE = f"""
mutation CartCreate($lines: [CartLineInput!]) {{
  cartCreate(input: {{ lines: $lines }}) {{
    cart {{ {CART_FIELDS} }}
    userErrors {{ field message }}
  }}
}}
"""

CART_GET = f"""
query CartGet($id: ID!) {{
  cart(id: $id) {{ {CART_FIELDS} }}
}}
"""

CART_LINES_ADD = f"""
mutation CartLinesAdd($cartId: ID!, $lines: [CartLineInput!]!) {{
  cartLinesAdd(cartId: $cartId, lines: $lines) {{
    cart {{ {CART_FIELDS} }}
    userErrors {{ field message }}
  }}
}}
"""

CART_LINES_UPDATE = f"""
mutation CartLinesUpdate($cartId: ID!, $lines: [CartLineUpdateInput!]!) {{
  cartLinesUpdate(cartId: $cartId, lines: $lines) {{
    cart {{ {CART_FIELDS} }}
    userErrors {{ field message }}
  }}
}}
"""

CART_LINES_REMOVE = f"""
mutation CartLinesRemove($cartId: ID!, $lineIds: [ID!]!) {{
  cartLinesRemove(cartId: $cartId, lineIds: $lineIds) {{
    cart {{ {CART_FIELDS} }}
    userErrors {{ field message }}
  }}
}}
"""
