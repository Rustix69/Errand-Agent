"""Composition root: the ONLY agent-side module that may import from ``errand.mocks``.

It hands the agent a ``ContractTools`` (exactly the 8 contract tools) and nothing else.
tests/test_boundaries.py allows this one import (``MockWorld``) and fails on any other.
"""

from __future__ import annotations

from errand.contract.port import ContractTools
from errand.mocks import MockWorld


def tools_for(world: MockWorld, user_id: str) -> ContractTools:
    return ContractTools(world.tools(user_id))
