"""Canonical order policy definitions shared by both interfaces."""

from __future__ import annotations

from .locks import OrderPolicyLock, make_order_policy_lock


WORKFLOW_VERSION = "8.8.1"
STREAMING_POLICY_REVISION = "8.8.streaming-furniture.v1"
INDOOR_FURNITURE_WHITELIST = "INDOOR_FURNITURE_WHITELIST"
DEFAULT_PROGRESSIVE_GATES = (1, 3, 10, 20)
W1A_SKILLS_QUOTA = {
    "Chair": 110,
    "Table": 80,
    "Storage": 65,
    "Stool/Bench": 45,
}
W1B_WEBSITE_QUOTA = {
    "Chair": 70,
    "Table": 50,
    "Storage": 45,
    "Stool/Bench": 35,
}
W1_ALLOWED_CATEGORIES = {
    "Chair / Dining Chair / Armchair": 180,
    "Table / Desk / Coffee Table / Side Table / Console Table": 130,
    "Cabinet / Dresser / Nightstand / TV Stand / Storage": 110,
    "Stool / Bench": 80,
}


def progressive_gates_for(target: int) -> tuple[int, ...]:
    """Return 1→3→10→20 clipped to a small Exact-N target."""
    target = int(target)
    if target < 1:
        raise ValueError("Exact-N target must be positive")
    return tuple(gate for gate in DEFAULT_PROGRESSIVE_GATES if gate <= target) or (target,)


def build_order_policy(
    *,
    source: str,
    categories: dict[str, int],
    exact_n: int,
    provider: str,
    registry_identity: str,
    registry_version: str,
    authorization_mode: str = "EXACT_COUNT_AUTHORIZATION",
    ruleset: str = "furniture-workflow-8.8.1",
    image_policy: str = "clean-single-product",
    five_year_policy: str = "published-within-five-years",
    naming_policy: str = "deterministic-product-name.v2",
    dimension_policy: str = "official-or-dual-agent",
    quality_policy: str = "raw-glb-only",
    category_quota_mode: str = "REQUIRED",
    policy_revision: str = "8.8.1",
    allowed_product_scope: str = "LEGACY",
    created_at: str | None = None,
) -> OrderPolicyLock:
    if category_quota_mode.upper() != "NONE" and sum(int(value) for value in categories.values()) != int(exact_n):
        raise ValueError("category quotas must sum to Exact-N")
    return make_order_policy_lock(
        source=source,
        categories={str(key): int(value) for key, value in categories.items()},
        exact_n=int(exact_n),
        provider=provider,
        ruleset=ruleset,
        image_policy=image_policy,
        five_year_policy=five_year_policy,
        naming_policy=naming_policy,
        dimension_policy=dimension_policy,
        registry_identity=registry_identity,
        registry_version=registry_version,
        authorization_mode=authorization_mode,
        quality_policy=quality_policy,
        category_quota_mode=category_quota_mode,
        policy_revision=policy_revision,
        allowed_product_scope=allowed_product_scope,
    )


def build_streaming_order_policy(
    *,
    source: str,
    exact_n: int,
    provider: str,
    registry_identity: str,
    registry_version: str,
    authorization_mode: str = "EXACT_COUNT_AUTHORIZATION",
    ruleset: str = "furniture-workflow-8.8.1",
    image_policy: str = "clean-single-product",
    five_year_policy: str = "published-within-five-years",
    naming_policy: str = "deterministic-product-name.v2",
    dimension_policy: str = "official-or-dual-agent",
    quality_policy: str = "raw-glb-only",
) -> OrderPolicyLock:
    """Build the 8.8 Exact-N policy with no category hard quotas."""

    return build_order_policy(
        source=source,
        categories={},
        exact_n=exact_n,
        provider=provider,
        registry_identity=registry_identity,
        registry_version=registry_version,
        authorization_mode=authorization_mode,
        ruleset=ruleset,
        image_policy=image_policy,
        five_year_policy=five_year_policy,
        naming_policy=naming_policy,
        dimension_policy=dimension_policy,
        quality_policy=quality_policy,
        category_quota_mode="NONE",
        policy_revision=STREAMING_POLICY_REVISION,
        allowed_product_scope=INDOOR_FURNITURE_WHITELIST,
    )


__all__ = [
    "DEFAULT_PROGRESSIVE_GATES",
    "W1A_SKILLS_QUOTA",
    "W1B_WEBSITE_QUOTA",
    "W1_ALLOWED_CATEGORIES",
    "INDOOR_FURNITURE_WHITELIST",
    "STREAMING_POLICY_REVISION",
    "WORKFLOW_VERSION",
    "build_order_policy",
    "build_streaming_order_policy",
    "progressive_gates_for",
]
