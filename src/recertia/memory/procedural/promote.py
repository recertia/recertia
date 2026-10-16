"""Compatibility shim. Promotion lives in :mod:`recertia.review.promotion`."""

from recertia.review.promotion import PromotionError, promote_to_approved

__all__ = ["PromotionError", "promote_to_approved"]
