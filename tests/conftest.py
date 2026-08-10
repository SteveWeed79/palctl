"""Fixtures shared across the suite.

`sim` lives here rather than being imported into each scenario module: pytest
resolves fixtures by name from conftest, so importing it explicitly would work
but reads as an unused name and trips F811 in every module that defines a test
taking `sim`.
"""

from tests.sim.harness import sim  # noqa: F401  (re-exported as a fixture)
