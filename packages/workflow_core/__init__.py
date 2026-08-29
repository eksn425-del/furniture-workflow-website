"""Canonical contracts shared by the web workflow workers.

The Skills bundle is an interface to this package; it must not maintain a
second implementation of provider, lineage, retry, or delivery rules.
"""

__version__ = "0.16.0"

from .order_policy import (
    DEFAULT_PROGRESSIVE_GATES,
    W1A_SKILLS_QUOTA,
    W1B_WEBSITE_QUOTA,
    W1_ALLOWED_CATEGORIES,
    build_order_policy,
    build_streaming_order_policy,
    progressive_gates_for,
)
from .runtime_files import initialize_runtime
from .frozen_catalog import (
    FROZEN_CATALOG_SCHEMA_VERSION,
    FrozenCatalogConflict,
    FrozenCatalogValidationError,
    catalog_content_hash,
    freeze_catalog_snapshot,
    frozen_catalog_path,
    read_frozen_catalog,
)
from .generation_scope import (
    GENERATION_SCOPE_SCHEMA_VERSION,
    generation_scope_key,
    generation_scope_payload,
)
from .source_profiles import (
    CGTRADER_PROFILE,
    ROOM_AND_BOARD_PROFILE,
    SourceProfile,
)
from .production_gate import (
    L2_BROWSER_REQUIRED,
    MEDIA_IDENTITY_MISMATCH,
    ProductionGate,
    ProductionGateDecision,
    READY_FOR_MODELING,
    REVIEW_REQUIRED,
    SCOPE_VISUAL_CONFLICT,
    evaluate_production_gate,
)
from .source_policy import MAX_FINAL_NAME_CHARS, SOURCE_POLICY_VERSION, resolve_source_policy

__all__ = [
    "__version__",
    "DEFAULT_PROGRESSIVE_GATES",
    "W1A_SKILLS_QUOTA",
    "W1B_WEBSITE_QUOTA",
    "W1_ALLOWED_CATEGORIES",
    "build_order_policy",
    "build_streaming_order_policy",
    "progressive_gates_for",
    "initialize_runtime",
    "FROZEN_CATALOG_SCHEMA_VERSION",
    "FrozenCatalogConflict",
    "FrozenCatalogValidationError",
    "catalog_content_hash",
    "freeze_catalog_snapshot",
    "frozen_catalog_path",
    "read_frozen_catalog",
    "GENERATION_SCOPE_SCHEMA_VERSION",
    "generation_scope_key",
    "generation_scope_payload",
    "CGTRADER_PROFILE",
    "ROOM_AND_BOARD_PROFILE",
    "SourceProfile",
    "MAX_FINAL_NAME_CHARS",
    "SOURCE_POLICY_VERSION",
    "resolve_source_policy",
    "ProductionGate",
    "ProductionGateDecision",
    "evaluate_production_gate",
    "READY_FOR_MODELING",
    "REVIEW_REQUIRED",
    "L2_BROWSER_REQUIRED",
    "MEDIA_IDENTITY_MISMATCH",
    "SCOPE_VISUAL_CONFLICT",
]
