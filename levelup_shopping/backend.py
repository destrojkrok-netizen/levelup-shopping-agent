"""``ShopifyStorefrontBackend``: the levelup.peoples store behind ``StorefrontBackend``.

Wiring (CLAUDE.md, "Commerce agent decision record"):

- catalog and search, profile, orders: live, Shopify Admin GraphQL API
- cart: live, Shopify Storefront API cart; ``checkout_handoff`` returns its ``checkoutUrl``
- policy content: not wired yet (raises ``NotWired``; the tool answers "unavailable")
- fulfillment: not wired yet (same); the store sells digital downloads only, so the
  switch is a candidate for ``enable_fulfillment=False`` instead

Catalog shape. A Shopify product is a shell that always has variants; price and stock
live on the variant. A product with one variant (Shopify's "Default Title") is served as a
plain record under the **variant's** GID with no options, so the cart takes the same id
search returned. A product with several variants is a family under the **product's** GID
with ``options`` from ``product.options``; its ``ProductDetails.variants`` are the variant
GIDs with ``option_values`` from ``selectedOptions`` and ``variant_of`` the product GID.
Ids are Shopify global ids (``gid://shopify/ProductVariant/…``) passed through unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from shopping_agent import (
    Cart,
    CartItem,
    CheckoutHandoff,
    FulfillmentOption,
    Order,
    OrderItem,
    OrderStatus,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
)

from . import shopify as gql
from .shopify import AdminClient, ShopifyCredentials, StorefrontClient, raise_user_errors

logger = logging.getLogger(__name__)

GUEST_ID = "guest"


class NotWired(RuntimeError):
    """A system the store has that this backend does not call yet; the executor's default
    wording ("temporarily unavailable") is right for it."""


class SignInRequired(RuntimeError):
    """A read that needs a signed-in customer arrived on a guest session; the executor
    subclass (``executor.py``) tells the model to ask the customer to sign in."""


def _money(node: dict[str, Any] | None) -> float:
    return float((node or {}).get("amount") or 0.0)


def _variant_in_stock(variant: dict[str, Any], product_active: bool) -> bool:
    if not product_active:
        return False
    if variant.get("availableForSale") is not None:
        return bool(variant["availableForSale"])
    tracked = bool((variant.get("inventoryItem") or {}).get("tracked"))
    if not tracked or variant.get("inventoryPolicy") == "CONTINUE":
        return True
    return int(variant.get("inventoryQuantity") or 0) > 0


class ShopifyStorefrontBackend(StorefrontBackend):
    """One service credential per API, held by the backend; every method acts for the
    customer in ``session.user_id`` (a Shopify customer GID, or ``"guest"``)."""

    def __init__(self, credentials: ShopifyCredentials) -> None:
        self._credentials = credentials
        self._admin: AdminClient | None = None
        self._storefront: StorefrontClient | None = None
        self._currency: str | None = None
        # Storefront cart id per session. In-process only: one worker until this moves
        # into the session store (see the decision record's Sessions line). TODO
        self._carts: dict[str, str] = {}

    # -- Clients ----------------------------------------------------------------------

    @property
    def admin(self) -> AdminClient:
        if self._admin is None:
            self._admin = AdminClient(self._credentials)
        return self._admin

    @property
    def storefront(self) -> StorefrontClient:
        if self._storefront is None:
            self._storefront = StorefrontClient(self._credentials)
        return self._storefront

    async def currency(self) -> str:
        if self._currency is None:
            data = await self.admin.run(gql.SHOP_CURRENCY)
            self._currency = (data.get("shop") or {}).get("currencyCode") or "USD"
        return self._currency

    async def aclose(self) -> None:
        for client in (self._admin, self._storefront):
            if client is not None:
                await client.aclose()

    def reset_session(self, session_id: str) -> None:
        self._carts.pop(session_id, None)

    # -- Catalog mapping ------------------------------------------------------------------

    def _map_product(self, node: dict[str, Any]) -> ProductDetails:
        """Shopify product -> plain record (one variant) or family (several)."""
        active = node.get("status") == "ACTIVE"
        variants = (node.get("variants") or {}).get("nodes") or []
        image = (((node.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}).get(
            "url"
        )
        price_node = (node.get("priceRangeV2") or {}).get("minVariantPrice") or {}
        currency = price_node.get("currencyCode") or "USD"
        base: dict[str, Any] = {
            "title": node.get("title") or "",
            "brand": node.get("vendor") or None,
            "currency": currency,
            "image_url": image,
            "category": node.get("productType") or None,
            "labels": list(node.get("tags") or []),
            "short_description": node.get("description") or None,
            "long_description": node.get("descriptionHtml") or None,
            "attributes": {"handle": node.get("handle") or ""},
        }
        if len(variants) <= 1:
            variant = variants[0] if variants else {}
            return ProductDetails(
                product_id=variant.get("id") or node["id"],
                price=float(variant.get("price") or price_node.get("amount") or 0.0),
                in_stock=_variant_in_stock(variant, active) if variant else active,
                specs={"sku": variant["sku"]} if variant.get("sku") else {},
                **base,
            )
        options = {
            opt["name"]: list(opt.get("values") or []) for opt in (node.get("options") or [])
        }
        mapped_variants = [
            Product(
                product_id=variant["id"],
                title=f"{base['title']} — {variant.get('title') or ''}".strip(" —"),
                price=float(variant.get("price") or 0.0),
                currency=currency,
                image_url=(variant.get("image") or {}).get("url") or image,
                in_stock=_variant_in_stock(variant, active),
                option_values={
                    o["name"]: o["value"] for o in (variant.get("selectedOptions") or [])
                },
                variant_of=node["id"],
            )
            for variant in variants
        ]
        in_stock_prices = [v.price for v in mapped_variants if v.in_stock]
        return ProductDetails(
            product_id=node["id"],
            price=min(in_stock_prices) if in_stock_prices else _money(price_node),
            in_stock=bool(in_stock_prices),
            options=options,
            variants=mapped_variants,
            **base,
        )

    @staticmethod
    def _search_query(query: str, filters: SearchFilters | None) -> str:
        parts = ["status:active"]
        if query.strip():
            parts.append(query.strip().replace('"', ""))
        if filters:
            if filters.category:
                parts.append(f'product_type:"{filters.category}"')
            if filters.min_price is not None:
                parts.append(f"price:>={filters.min_price}")
            if filters.max_price is not None:
                parts.append(f"price:<={filters.max_price}")
            for value in filters.attributes.values():  # tags carry the domain dimensions
                parts.append(f'tag:"{value}"')
        return " AND ".join(parts)

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        data = await self.admin.run(
            gql.SEARCH_PRODUCTS, {"query": self._search_query(query, filters), "first": limit}
        )
        products = [
            self._map_product(node) for node in (data.get("products") or {}).get("nodes") or []
        ]
        if filters and filters.sort == "price_asc":
            products.sort(key=lambda p: p.price)
        elif filters and filters.sort == "price_desc":
            products.sort(key=lambda p: -p.price)
        # Search returns the family, not its variants; strip details to the Product shape.
        return [
            Product.model_validate(p.model_dump(exclude={"variants", "specs"})) for p in products
        ]

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        if "/ProductVariant/" in product_id:
            data = await self.admin.run(gql.GET_VARIANT_PRODUCT, {"id": product_id})
            node = (data.get("productVariant") or {}).get("product")
            if node is None:
                return None
            details = self._map_product(node)
            if details.product_id == product_id:
                return details
            variant = next((v for v in details.variants if v.product_id == product_id), None)
            if variant is None:
                return None
            return ProductDetails(**variant.model_dump(), long_description=details.long_description)
        if "/Product/" in product_id:
            data = await self.admin.run(gql.GET_PRODUCT, {"id": product_id})
            node = data.get("product")
            return None if node is None else self._map_product(node)
        return None

    # -- Cart (Storefront API) ----------------------------------------------------------

    def _map_cart(self, node: dict[str, Any]) -> Cart:
        items = []
        for line in (node.get("lines") or {}).get("nodes") or []:
            merch = line.get("merchandise") or {}
            product = merch.get("product") or {}
            title = product.get("title") or merch.get("title") or ""
            option_values = {
                o["name"]: o["value"]
                for o in (merch.get("selectedOptions") or [])
                if o.get("name") != "Title"
            }
            items.append(
                CartItem(
                    product_id=merch.get("id", ""),
                    title=f"{title} — {merch['title']}" if option_values else title,
                    price=_money(merch.get("price")),
                    quantity=int(line.get("quantity") or 1),
                    image_url=((merch.get("image") or {}).get("url"))
                    or ((product.get("featuredImage") or {}).get("url")),
                    option_values=option_values,
                    variant_of=product.get("id") if option_values else None,
                )
            )
        currency = ((node.get("cost") or {}).get("subtotalAmount") or {}).get("currencyCode")
        return Cart(items=items, currency=currency or "USD")

    async def _cart_node(self, session: ShoppingSessionContext) -> dict[str, Any] | None:
        cart_id = self._carts.get(session.session_id)
        if cart_id is None:
            return None
        data = await self.storefront.run(gql.CART_GET, {"id": cart_id})
        node = data.get("cart")
        if node is None:  # expired on Shopify's side
            self._carts.pop(session.session_id, None)
        return node

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        node = await self._cart_node(session)
        return Cart(currency=await self.currency()) if node is None else self._map_cart(node)

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        details = await self.get_product_details(session, product_id)
        if details is not None and not details.in_stock:
            siblings = []
            if details.variant_of:
                family = await self.get_product_details(session, details.variant_of)
                siblings = [v.product_id for v in (family.variants if family else []) if v.in_stock]
            raise Unavailable(
                f"{product_id} is out of stock"
                + (f"; in stock: {', '.join(siblings)}" if siblings else "")
            )
        line = {"merchandiseId": product_id, "quantity": quantity}
        node = await self._cart_node(session)
        if node is None:
            data = await self.storefront.run(gql.CART_CREATE, {"lines": [line]})
            payload = raise_user_errors(data.get("cartCreate"), "cartCreate")
            self._carts[session.session_id] = payload["cart"]["id"]
            return self._map_cart(payload["cart"])
        existing = self._line_for(node, product_id)
        if existing is not None:
            return await self._set_line(
                node["id"], existing, existing_quantity(node, product_id) + quantity
            )
        data = await self.storefront.run(
            gql.CART_LINES_ADD, {"cartId": node["id"], "lines": [line]}
        )
        return self._map_cart(raise_user_errors(data.get("cartLinesAdd"), "cartLinesAdd")["cart"])

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        node = await self._cart_node(session)
        if node is None:
            return Cart(currency=await self.currency())
        line_id = self._line_for(node, product_id)
        if line_id is None:
            return self._map_cart(node)
        return await self._set_line(node["id"], line_id, quantity)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        node = await self._cart_node(session)
        if node is None:
            return Cart(currency=await self.currency())
        line_id = self._line_for(node, product_id)
        if line_id is None:
            return self._map_cart(node)
        data = await self.storefront.run(
            gql.CART_LINES_REMOVE, {"cartId": node["id"], "lineIds": [line_id]}
        )
        return self._map_cart(
            raise_user_errors(data.get("cartLinesRemove"), "cartLinesRemove")["cart"]
        )

    @staticmethod
    def _line_for(node: dict[str, Any], product_id: str) -> str | None:
        for line in (node.get("lines") or {}).get("nodes") or []:
            if (line.get("merchandise") or {}).get("id") == product_id:
                return line["id"]
        return None

    async def _set_line(self, cart_id: str, line_id: str, quantity: int) -> Cart:
        data = await self.storefront.run(
            gql.CART_LINES_UPDATE,
            {"cartId": cart_id, "lines": [{"id": line_id, "quantity": quantity}]},
        )
        return self._map_cart(
            raise_user_errors(data.get("cartLinesUpdate"), "cartLinesUpdate")["cart"]
        )

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        node = await self._cart_node(session)
        url = (node or {}).get("checkoutUrl")
        if not url or not url.startswith("https://"):
            return []
        return [CheckoutHandoff(url=url, label="Continue to checkout")]

    # -- Customer context (Admin API) ---------------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        if session.user_id == GUEST_ID:
            return UserPreferences(user_id=GUEST_ID)
        data = await self.admin.run(gql.GET_CUSTOMER, {"id": session.user_id})
        customer = data.get("customer") or {}
        address = customer.get("defaultAddress") or {}
        location = ", ".join(p for p in (address.get("city"), address.get("country")) if p)
        return UserPreferences(
            user_id=session.user_id,
            display_name=customer.get("displayName") or None,
            default_location=location or None,
            preferences={"orders": str(customer.get("numberOfOrders") or 0)},
        )

    # -- Orders (Admin API) ---------------------------------------------------------------

    @staticmethod
    def _order_status(node: dict[str, Any]) -> OrderStatus:
        if node.get("cancelledAt"):
            return OrderStatus.CANCELLED
        financial = node.get("displayFinancialStatus") or ""
        if financial in ("REFUNDED", "PARTIALLY_REFUNDED"):
            return OrderStatus.REFUNDED
        fulfillment = node.get("displayFulfillmentStatus") or ""
        return {
            "FULFILLED": OrderStatus.DELIVERED,
            "IN_TRANSIT": OrderStatus.SHIPPED,
            "OUT_FOR_DELIVERY": OrderStatus.OUT_FOR_DELIVERY,
            "PARTIALLY_FULFILLED": OrderStatus.SHIPPED,
        }.get(fulfillment, OrderStatus.PROCESSING)

    def _map_order(self, node: dict[str, Any]) -> Order:
        items = []
        for line in (node.get("lineItems") or {}).get("nodes") or []:
            variant = line.get("variant") or {}
            option_values = {
                o["name"]: o["value"]
                for o in (variant.get("selectedOptions") or [])
                if o.get("name") != "Title"
            }
            items.append(
                OrderItem(
                    product_id=variant.get("id") or "",
                    title=line.get("title") or "",
                    quantity=int(line.get("quantity") or 1),
                    price=_money((line.get("originalUnitPriceSet") or {}).get("shopMoney")),
                    option_values=option_values,
                    variant_of=(variant.get("product") or {}).get("id") if option_values else None,
                )
            )
        total = (node.get("currentTotalPriceSet") or {}).get("shopMoney") or {}
        fulfillments = node.get("fulfillments") or []
        tracking = next(
            (
                t.get("url")
                for f in fulfillments
                for t in f.get("trackingInfo") or []
                if t.get("url")
            ),
            None,
        )
        eta = next(
            (f.get("estimatedDeliveryAt") for f in fulfillments if f.get("estimatedDeliveryAt")),
            None,
        )
        return Order(
            order_id=node["id"],
            status=self._order_status(node),
            placed_at=datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00")),
            items=items,
            total=_money(total),
            currency=total.get("currencyCode") or "USD",
            estimated_delivery=eta,
            tracking_url=tracking,
        )

    @staticmethod
    def _customer_number(user_id: str) -> str:
        return user_id.rsplit("/", 1)[-1]

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        if session.user_id == GUEST_ID:
            raise SignInRequired("order history")
        data = await self.admin.run(
            gql.LIST_ORDERS,
            {"query": f"customer_id:{self._customer_number(session.user_id)}", "first": limit},
        )
        return [self._map_order(n) for n in (data.get("orders") or {}).get("nodes") or []]

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        if session.user_id == GUEST_ID:
            raise SignInRequired("order status")
        data = await self.admin.run(gql.GET_ORDER, {"id": order_id})
        node = data.get("order")
        if node is None or (node.get("customer") or {}).get("id") != session.user_id:
            return None  # unknown, or someone else's
        return self._map_order(node)

    # -- Not wired yet ----------------------------------------------------------------------

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        # TODO: wire to Admin `shop { shopPolicies { type body } }` (refund, privacy,
        # terms of service), or to the store's help pages; then move purchase-research
        # and customer-care out of skills/_staged with /add-commerce-flow.
        raise NotWired("policy content")

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        # TODO: the store sells digital downloads only. Either set
        # `enable_fulfillment=False` in agent_config.py (removes the tool), or return a
        # single {"method": "delivery", "eta": "instant download after payment"} option.
        raise NotWired("fulfillment")


def existing_quantity(node: dict[str, Any], product_id: str) -> int:
    for line in (node.get("lines") or {}).get("nodes") or []:
        if (line.get("merchandise") or {}).get("id") == product_id:
            return int(line.get("quantity") or 0)
    return 0
