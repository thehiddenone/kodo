"""Vendor-dispatching USD cost computation — see :attr:`~kodo.llms._interface.Usage.usd_cost`.

Each cloud vendor package (``kodo.llms.anthropic``, ``kodo.llms.openai``, ...)
owns its own ``compute_cost(usage) -> float`` with a hand-maintained pricing
table. This module is the single place that picks *which* vendor's table
applies to a given :class:`~kodo.llms._interface.Usage`, so
``Usage.usd_cost`` itself never has to hardcode one vendor.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from ._cloud_registry import get_cloud_vendor_for_model_prefix, get_cloud_vendor_module

if TYPE_CHECKING:
    from ._interface import Usage

__all__ = ["compute_cost"]


def compute_cost(usage: Usage) -> float:
    """Dispatch to the owning vendor's pricing table, by ``usage.model``'s prefix.

    Args:
        usage: Token usage record from any :class:`~kodo.llms._interface.LLMPlugin`.

    Returns:
        float: Estimated USD cost, or ``0.0`` for a local model / a model
        whose id matches no known cloud vendor's prefix.
    """
    vendor = get_cloud_vendor_for_model_prefix(usage.model)
    module_name = get_cloud_vendor_module(vendor) if vendor else None
    if module_name is None:
        return 0.0
    vendor_pkg = importlib.import_module(module_name)
    compute = getattr(vendor_pkg, "compute_cost", None)
    return float(compute(usage)) if compute is not None else 0.0
