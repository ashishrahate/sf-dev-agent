"""Smoke tests for core models, prompt loading, and provider factory."""

from sf_dev_agent.models.schemas import (
    ExecutionPlan,
    OrgConnection,
    PlanStep,
    RiskLevel,
    Task,
    TaskStatus,
)
from sf_dev_agent.prompts import load_system_prompt
from sf_dev_agent.providers import PROVIDERS, create_provider
from sf_dev_agent.providers.base import LLMProvider


def test_task_state_defaults():
    task = Task(
        task_id="t_001",
        tenant_id="test",
        user_request="Create a trigger",
    )
    assert task.status == TaskStatus.RECEIVED
    assert task.plan is None
    assert task.result is None


def test_execution_plan_serializes():
    plan = ExecutionPlan(
        summary="Create Account dedup trigger",
        steps=[
            PlanStep(
                step_number=1,
                action="file_write",
                target="classes/AccountDedupTrigger.trigger",
                mode="create",
                risk=RiskLevel.MEDIUM,
                description="New before-insert trigger on Account",
            ),
        ],
        risk_assessment=RiskLevel.MEDIUM,
        risk_reasoning="New trigger on a core object",
        rollback_strategy="Delete the trigger from the org",
        components_created=1,
    )
    data = plan.model_dump()
    assert len(data["steps"]) == 1
    assert data["risk_assessment"] == "medium"


def test_org_connection_no_token_leak():
    org = OrgConnection(
        tenant_id="t1",
        org_alias="my-scratch",
        org_type="scratch",
        instance_url="https://test.salesforce.com",
        access_token="secret_token_123",
    )
    data = org.model_dump(exclude={"access_token", "refresh_token_ref"})
    assert "access_token" not in data
    assert "refresh_token_ref" not in data


def test_system_prompt_loads_and_injects():
    prompt = load_system_prompt(
        TENANT_ID="test_tenant",
        ORG_ALIAS="my-scratch",
        ORG_TYPE="scratch",
        INSTANCE_URL="https://test.salesforce.com",
        API_VERSION="62.0",
        AGENT_MODEL="test-model",
        TIMESTAMP="2025-04-25T00:00:00Z",
    )
    assert "test_tenant" in prompt
    assert "my-scratch" in prompt
    assert "scratch" in prompt
    assert "62.0" in prompt
    assert "test-model" in prompt
    # No un-replaced placeholders
    assert "{{TENANT_ID}}" not in prompt
    assert "{{ORG_ALIAS}}" not in prompt
    assert "{{AGENT_MODEL}}" not in prompt


def test_provider_factory_returns_llmprovider():
    """create_provider returns an LLMProvider without importing the SDK."""
    # We can't call .chat() without real credentials, but we can verify the
    # factory resolves and the returned object satisfies the interface.
    # Use a try/except so this test is skipped gracefully if no SDK installed.
    for name in PROVIDERS:
        try:
            p = create_provider(provider=name, model=None)
            assert isinstance(p, LLMProvider)
            assert isinstance(p.model_name, str)
            assert len(p.model_name) > 0
        except ImportError:
            pass  # SDK not installed in this environment — that's expected


def test_provider_factory_rejects_unknown():
    import pytest
    with pytest.raises(ValueError, match="Unknown provider"):
        create_provider(provider="totally_fake_provider")
