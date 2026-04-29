"""Parser registry for metadata files.

Adding a new component type — ValidationRule, Flow, CustomMetadataType, LWC,
RecordType, PermissionSet, etc. — is a two-step change:

  1. Write a Parser subclass in this directory (e.g. validation_rule.py)
     that registers itself via `register(ValidationRuleParser())`.
  2. Add `from . import validation_rule` below.

No other code in the project needs to change. The orchestrator iterates
`get_parsers()`; the index reads any extracted fields from `metadata_json`.
"""

from sf_dev_agent.context.parsers.base import (
    ParsedComponent,
    ParsedRelationship,
    ParseResult,
    Parser,
    discovered_component_types,
    dispatch,
    get_parsers,
    register,
)

# Side-effect imports: each module calls `register(...)` at the bottom.
from sf_dev_agent.context.parsers import apex_class  # noqa: F401
from sf_dev_agent.context.parsers import apex_trigger  # noqa: F401
from sf_dev_agent.context.parsers import custom_object  # noqa: F401
from sf_dev_agent.context.parsers import flow  # noqa: F401
from sf_dev_agent.context.parsers import lwc  # noqa: F401
from sf_dev_agent.context.parsers import record_type  # noqa: F401
from sf_dev_agent.context.parsers import validation_rule  # noqa: F401

__all__ = [
    "ParsedComponent",
    "ParsedRelationship",
    "ParseResult",
    "Parser",
    "discovered_component_types",
    "dispatch",
    "get_parsers",
    "register",
]
