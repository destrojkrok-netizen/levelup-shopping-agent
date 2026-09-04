"""The deployment's executor: ``ShoppingToolExecutor`` plus one domain error mapping."""

from __future__ import annotations

from commerce_common.streaming import ToolOutcome
from shopping_agent.executor import ShoppingToolExecutor

from .backend import SignInRequired


class LevelupToolExecutor(ShoppingToolExecutor):
    sign_in_text = (
        "The customer is browsing as a guest, so {detail} is not available. Ask them to sign "
        "in to their store account to see it."
    )

    def domain_error(self, error: Exception) -> ToolOutcome | None:
        if isinstance(error, SignInRequired):
            detail = self._sanitize(str(error), 80) or "that"
            return ToolOutcome.error(self.sign_in_text.format(detail=detail))
        return super().domain_error(error)
