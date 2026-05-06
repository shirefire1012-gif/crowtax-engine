"""Worked-example tests for crowtax_engine.simple.estimate()."""

from __future__ import annotations


def test_module_imports():
    from crowtax_engine.simple import estimate, SimpleEstimateInput  # noqa: F401
    from crowtax_engine.simple import SimpleEstimateResult  # noqa: F401
