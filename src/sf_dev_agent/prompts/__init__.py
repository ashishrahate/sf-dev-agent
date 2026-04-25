"""System prompt loader for the Salesforce Developer Agent."""

from pathlib import Path


def load_system_prompt(**kwargs: str) -> str:
    """Load the system prompt template and inject environment variables.
    
    Args:
        **kwargs: Template variables to inject. Expected keys:
            TENANT_ID, ORG_ALIAS, ORG_TYPE, INSTANCE_URL,
            API_VERSION, TIMESTAMP
    
    Returns:
        The fully rendered system prompt string.
    """
    prompt_path = Path(__file__).parent / "system_prompt.md"
    template = prompt_path.read_text(encoding="utf-8")

    for key, value in kwargs.items():
        placeholder = "{{" + key + "}}"
        template = template.replace(placeholder, str(value))

    return template
