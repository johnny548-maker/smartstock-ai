# -*- coding: utf-8 -*-
"""Regression guard for the 2026-06-23 live outage: main.py passed build_PAYLOAD-only kwargs
(validated_portfolio / factor_validation) to report_builder.build_report, which rejects them →
every daily cron run crashed at the report step. Unit tests never invoke main(), so it was latent.

This statically checks that EVERY keyword main.py passes to build_report / build_payload is a real
parameter of the callee (unless the callee takes **kwargs). Catches the whole 'main calls a function
with a kwarg it doesn't accept' bug class without running the full network pipeline.
"""
import ast
import inspect
import unittest

import report_builder
import web_export


def _call_kwarg_sets(src, func_attr):
    """All keyword-name sets for calls like `X.<func_attr>(...)` in `src`."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == func_attr):
            out.append({k.arg for k in node.keywords if k.arg})   # k.arg is None for **kwargs
    return out


class TestCallSignatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("main.py", encoding="utf-8") as f:
            cls.src = f.read()

    def _assert_calls_valid(self, attr, func):
        sig = inspect.signature(func)
        params = set(sig.parameters)
        has_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        calls = _call_kwarg_sets(self.src, attr)
        self.assertTrue(calls, f"no {attr}() call found in main.py")
        for kws in calls:
            if has_var_kw:
                continue
            extra = kws - params
            self.assertEqual(extra, set(),
                             f"main.py calls {attr}() with kwargs not in its signature: {sorted(extra)}")

    def test_build_report_call_kwargs_valid(self):
        self._assert_calls_valid("build_report", report_builder.build_report)

    def test_build_payload_call_kwargs_valid(self):
        self._assert_calls_valid("build_payload", web_export.build_payload)


if __name__ == "__main__":
    unittest.main()
