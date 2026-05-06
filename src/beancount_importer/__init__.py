"""Resolve Pydantic forward references across the package.

`CategoryProposal` and `ImportResult` reference `CategorizationRule` and
`TagStateDelta` via TYPE_CHECKING, so pydantic needs `model_rebuild()` once those
modules are loaded. Importing this package ensures the rebuild has happened.
"""

from beancount_importer.models import CategoryProposal, ImportResult
from beancount_importer.rules.models import CategorizationRule  # noqa: F401
from beancount_importer.rules.tags import TagStateDelta  # noqa: F401

CategoryProposal.model_rebuild()
ImportResult.model_rebuild()
