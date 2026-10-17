"""Compatibility shim. Promotion lives in :mod:`recertia.review.promotion`."""

from recertia.review.promotion import PromotionError, PromotionWriter, promote_to_approved

__all__ = ["PromotionError", "PromotionWriter", "promote_to_approved"]
