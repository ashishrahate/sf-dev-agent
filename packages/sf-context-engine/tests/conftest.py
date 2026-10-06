"""Keep the suite hermetic: never download a model or call a hosted embedding API."""

import os

os.environ["SF_CONTEXT_EMBEDDER"] = "mock"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_org(monkeypatch):
    """Helpers that would call the live org return 'nothing found' unless a test overrides them."""
    import sf_context_engine
    from sf_context_engine import delta

    monkeypatch.setattr(delta, "_query_soql", lambda org_alias, query, timeout: ([], None))
    monkeypatch.setattr(sf_context_engine, "fetch_flow_versions", lambda *a, **k: ({}, None))
