# Test Report — sf-dev-agent

**Date:** 2026-04-25
**Python:** 3.12.6
**pytest:** 9.0.3
**Runner:** `uv run pytest tests/ -v`

---

## Summary

| Metric | Value |
|--------|-------|
| Total tests | 6 |
| Passed | 6 |
| Failed | 0 |
| Errors | 0 |
| Warnings | 0 |
| Duration | ~21s |

---

## Results by Test

| # | Test | File | Status |
|---|------|------|--------|
| 1 | `test_task_state_defaults` | `tests/test_core.py` | PASSED |
| 2 | `test_execution_plan_serializes` | `tests/test_core.py` | PASSED |
| 3 | `test_org_connection_no_token_leak` | `tests/test_core.py` | PASSED |
| 4 | `test_system_prompt_loads_and_injects` | `tests/test_core.py` | PASSED |
| 5 | `test_provider_factory_returns_llmprovider` | `tests/test_core.py` | PASSED |
| 6 | `test_provider_factory_rejects_unknown` | `tests/test_core.py` | PASSED |

---

## Test Descriptions

### `test_task_state_defaults`
Verifies `Task` initializes with `status = RECEIVED`, `plan = None`, `result = None`.

### `test_execution_plan_serializes`
Creates an `ExecutionPlan` with one `PlanStep`, calls `.model_dump()`, and asserts the risk enum serializes to its lowercase string value (`"medium"`).

### `test_org_connection_no_token_leak`
Verifies `access_token` and `refresh_token_ref` are excluded from serialization when using `model_dump(exclude={...})`.

### `test_system_prompt_loads_and_injects`
Calls `load_system_prompt()` with all seven template variables (including new `AGENT_MODEL`) and asserts each value is present and no `{{PLACEHOLDER}}` tokens remain.

### `test_provider_factory_returns_llmprovider`
Iterates all three providers (`anthropic`, `openai`, `gemini`) via `create_provider()`. For each installed SDK, asserts the result is an `LLMProvider` instance with a non-empty `model_name`. Gracefully skips providers whose SDK is not installed.

### `test_provider_factory_rejects_unknown`
Asserts `create_provider(provider="totally_fake_provider")` raises `ValueError` with a message matching `"Unknown provider"`.

---

## Warnings

None.

---

## Coverage

No coverage report generated in this run. To generate:

```bash
uv run pytest tests/ --cov=src/sf_dev_agent --cov-report=term-missing
```

### Known coverage gaps
- `src/sf_dev_agent/agent.py` — `AgentLoop` requires mocking of provider and tool registry
- `src/sf_dev_agent/providers/anthropic_provider.py` — requires mocking of `anthropic.Anthropic`
- `src/sf_dev_agent/providers/openai_provider.py` — requires mocking of `openai.OpenAI`
- `src/sf_dev_agent/providers/gemini_provider.py` — requires mocking of `google.generativeai`
- `src/sf_dev_agent/tools/registry.py` — tool executors require mocking of `subprocess`
- `src/sf_dev_agent/__main__.py` — CLI requires mocking of `sys.argv` and Rich console
- `src/sf_dev_agent/context/__init__.py` — stub, not yet implemented
- `src/sf_dev_agent/memory/__init__.py` — stub, not yet implemented

---

## Environment

| Item | Value |
|------|-------|
| Platform | win32 (Windows 11 Enterprise 10.0.26100) |
| Python | 3.12.6 |
| pytest | 9.0.3 |
| pytest-asyncio | 1.3.0 |
| Pydantic | 2.x |
| anthropic | >=0.97.0 (optional extra) |
| openai | >=1.0.0 (optional extra) |
| google-generativeai | >=0.8.0 (optional extra) |
