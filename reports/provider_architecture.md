# Provider Architecture — Multi-LLM Abstraction

**Date:** 2026-04-25
**Status:** Implemented

---

## Overview

The agent is provider-agnostic. Any LLM that supports tool/function calling can be wired in by implementing the `LLMProvider` abstract base class. Three providers ship out of the box.

---

## Supported Providers

| Provider | Default Model | SDK Package | API Key Env Var |
|----------|--------------|-------------|-----------------|
| `anthropic` | `claude-sonnet-4-6` | `anthropic>=0.97.0` | `ANTHROPIC_API_KEY` |
| `openai` | `gpt-4o` | `openai>=1.0.0` | `OPENAI_API_KEY` |
| `gemini` | `gemini-2.0-flash` | `google-generativeai>=0.8.0` | `GOOGLE_API_KEY` |

---

## Installation

Each provider SDK is an optional extra — only install what you need:

```bash
# Anthropic
uv pip install 'sf-dev-agent[anthropic]'

# OpenAI
uv pip install 'sf-dev-agent[openai]'

# Google Gemini
uv pip install 'sf-dev-agent[gemini]'

# All three
uv pip install 'sf-dev-agent[all]'
```

---

## Selection

Provider is resolved in this order:

1. `--provider` CLI flag
2. `LLM_PROVIDER` environment variable
3. Default: `anthropic`

Model is resolved in this order:

1. `--model` CLI flag
2. `LLM_MODEL` environment variable
3. Provider's built-in default

```bash
# Use OpenAI GPT-4o
sf-agent --provider openai "Create a trigger on Account"

# Use Gemini with a specific model
sf-agent --provider gemini --model gemini-2.5-flash "Describe Account fields"

# Set provider via env var
LLM_PROVIDER=openai sf-agent "..."
```

---

## Architecture

```
AgentLoop
    │
    └── LLMProvider (abstract)
            ├── AnthropicProvider   ← claude-sonnet-4-6 (default)
            ├── OpenAIProvider      ← gpt-4o (default)
            └── GeminiProvider      ← gemini-2.5-pro (default)
```

### Internal Message Format

All conversation history is stored in **Anthropic-like format** (the most explicit for tool use). Each provider adapter converts from this format before calling its API, and converts the response back into a normalized `LLMResponse`.

**Internal format:**
```python
# User message
{"role": "user", "content": "text string"}

# Assistant message with tool calls
{"role": "assistant", "content": [
    {"type": "text", "text": "I'll query the org..."},
    {"type": "tool_use", "id": "tu_123", "name": "sf_metadata_describe", "input": {...}},
]}

# Tool results (sent as user message)
{"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "tu_123", "content": "{...}", "is_error": False},
]}
```

### Tool Definition Format

The `ToolRegistry` emits a **provider-neutral format**:
```python
{"name": "sf_metadata_describe", "description": "...", "parameters": {JSON Schema}}
```

Each provider wraps this into its native format:

| Provider | Native format |
|----------|--------------|
| Anthropic | `{"name", "description", "input_schema"}` |
| OpenAI | `{"type": "function", "function": {"name", "description", "parameters"}}` |
| Gemini | `{"function_declarations": [{"name", "description", "parameters"}]}` |

---

## Key Files

| File | Purpose |
|------|---------|
| `src/sf_dev_agent/providers/base.py` | `LLMProvider` ABC, `LLMResponse`, `ToolCall` dataclasses |
| `src/sf_dev_agent/providers/anthropic_provider.py` | Anthropic adapter |
| `src/sf_dev_agent/providers/openai_provider.py` | OpenAI adapter |
| `src/sf_dev_agent/providers/gemini_provider.py` | Gemini adapter |
| `src/sf_dev_agent/providers/__init__.py` | `create_provider()` factory |
| `src/sf_dev_agent/agent.py` | `AgentLoop` — accepts any `LLMProvider` |
| `src/sf_dev_agent/tools/registry.py` | `get_tool_definitions()` — neutral format |

---

## Adding a New Provider

1. Create `src/sf_dev_agent/providers/my_provider.py`
2. Implement `LLMProvider`:
   ```python
   from sf_dev_agent.providers.base import LLMProvider, LLMResponse, ToolCall

   class MyProvider(LLMProvider):
       @property
       def model_name(self) -> str: ...

       def chat(self, *, system, messages, tools, max_tokens=16384) -> LLMResponse:
           # Convert messages and tools to your API format
           # Call your API
           # Return LLMResponse(text_blocks=[...], tool_calls=[...], stop_reason="...")
   ```
3. Register in `providers/__init__.py` `create_provider()` and `PROVIDERS` tuple
4. Add an optional extra in `pyproject.toml`

---

## Message Conversion Notes

### Anthropic
Passes the internal format through directly after stripping internal-only keys from `tool_result` blocks.

### OpenAI
- `assistant` messages with `tool_use` blocks → `"tool_calls"` array on the message
- `tool_result` blocks → separate `{"role": "tool", "tool_call_id": ...}` messages
- System prompt → prepended as `{"role": "system", "content": ...}` message

### Gemini
- `assistant` role → `"model"` role
- `tool_use` blocks → `{"function_call": {"name", "args"}}` parts
- `tool_result` blocks → `{"function_response": {"name", "response"}}` parts
- Requires `tool_use_id → name` pre-pass since `function_response` needs the function name
- Tool call IDs are generated locally (`uuid4`) since Gemini does not issue them
- Safety filters disabled to prevent blocking Salesforce/code content
