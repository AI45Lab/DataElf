"""Typed AI Index acquisition plans and executor."""

from dataelf_server.scope_v2.client import ScopeV2AIIndexClient, ScopeV2AIIndexError
from dataelf_server.scope_v2.integration import (
    ScopeV2IntegrationError,
    ScopeV2PrefetchResult,
    materialize_filtered_ai_index_envelopes,
    prefetch_scope_v2,
)
from dataelf_server.scope_v2.contracts import ScopePlan, ScopeV2Error
from dataelf_server.scope_v2.runner import ScopeV2Executor

__all__ = [
    "ScopePlan",
    "ScopeV2AIIndexClient",
    "ScopeV2AIIndexError",
    "ScopeV2Error",
    "ScopeV2Executor",
    "ScopeV2IntegrationError",
    "ScopeV2PrefetchResult",
    "materialize_filtered_ai_index_envelopes",
    "prefetch_scope_v2",
]
