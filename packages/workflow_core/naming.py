"""Single deterministic implementation for public product names.

The raw website title is evidence only.  It is never an input to the final
four-part public name except when it is used as evidence to identify a
standardized product type.  Style, color, and material must be explicitly
provided from governed evidence and must be members of the shared vocabulary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .source_policy import MAX_FINAL_NAME_CHARS


VOCABULARY_PATH: Final[Path] = Path(__file__).with_name("naming_vocabulary.json")
NAMING_RULE_VERSION: Final[str] = "deterministic-product-name.v2"
NAMING_CONTRACT_V1: Final[str] = "naming-contract.v1"
NAMING_CONTRACT_V2: Final[str] = "naming-contract.v2"
NAMING_CONTRACT_V2_BRAND_DIRECT: Final[str] = "naming-contract.v2-brand-direct"
NAMING_CONTRACT_V3_MARKETPLACE: Final[str] = "naming-contract.v3-marketplace"
MAX_FINAL_NAME_CHARS: Final[int] = 50
BRAND_PREFIX_POLICIES: Final[tuple[str, ...]] = ("REQUIRED", "OPTIONAL", "NONE")


class NamingReviewRequired(ValueError):
    """Raised when deterministic naming lacks valid governed evidence."""


def _load_vocabulary() -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    try:
        payload = json.loads(VOCABULARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"naming vocabulary unavailable: {VOCABULARY_PATH}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("naming vocabulary must be a JSON object")
    result: dict[str, tuple[str, ...]] = {}
    for key in ("style", "color", "material", "type"):
        values = payload.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(item, str) for item in values):
            raise RuntimeError(f"naming vocabulary field {key!r} is invalid")
        result[key] = tuple(item.strip() for item in values if item.strip())
    raw_aliases = payload.get("aliases", {})
    aliases = {
        re.sub(r"\s+", " ", alias.strip()).casefold(): str(target).strip()
        for alias, target in raw_aliases.items()
        if isinstance(alias, str) and isinstance(target, str) and target.strip()
    } if isinstance(raw_aliases, dict) else {}
    return result, aliases


_VOCABULARY, _ALIASES = _load_vocabulary()
STYLE_VOCABULARY: Final[tuple[str, ...]] = _VOCABULARY["style"]
COLOR_VOCABULARY: Final[tuple[str, ...]] = _VOCABULARY["color"]
MATERIAL_VOCABULARY: Final[tuple[str, ...]] = _VOCABULARY["material"]
TYPE_VOCABULARY: Final[tuple[str, ...]] = _VOCABULARY["type"]
FEATURE_VOCABULARY: Final[tuple[str, ...]] = tuple(
    item.strip() for item in json.loads(VOCABULARY_PATH.read_text(encoding="utf-8")).get("feature", [])
    if isinstance(item, str) and item.strip()
)


def _key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


_MARKETING_NAME_WORDS = frozenset({
    "new", "exclusive", "best", "sale", "limited", "collection", "classic", "signature",
    "premium", "outdoor", "indoor", "home", "furniture", "available", "popular",
})
_DIMENSION_WORD = re.compile(r"^(?:\d+(?:\.\d+)?|\d+[/-]\d+)(?:in|inch|inches|cm|mm|m|w|d|h)?$", re.I)


def _name_text(value: object) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" -–—,;:")
    if not text:
        raise NamingReviewRequired("product_name_missing")
    if "  " in text or text != text.strip():
        raise NamingReviewRequired("product_name_whitespace_invalid")
    return text


def _phrase_tokens(value: object) -> list[str]:
    return _key(value).split() if _key(value) else []


def shorten_name_to_limit(value: object, *, max_chars: int = MAX_FINAL_NAME_CHARS, required_prefix: object = "", required_type: object = "", removable_phrases: tuple[object, ...] = ()) -> str:
    """Fit a name by removing whole optional words; never slice a word."""
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars_invalid")
    text = _name_text(value)
    if len(text) <= max_chars:
        return text
    words = text.split()
    prefix = _phrase_tokens(required_prefix)
    type_words = _phrase_tokens(required_type)
    folded = [_key(word) for word in words]
    protected: set[int] = set()

    def rebuild_protected() -> None:
        protected.clear()
        if prefix and folded[:len(prefix)] == prefix:
            protected.update(range(len(prefix)))
        if type_words and folded[-len(type_words):] == type_words:
            protected.update(range(len(words) - len(type_words), len(words)))

    rebuild_protected()
    for phrase in removable_phrases:
        target = _phrase_tokens(phrase)
        if not target:
            continue
        while len(" ".join(words)) > max_chars:
            removed = False
            for start in range(len(words) - len(target), -1, -1):
                end = start + len(target)
                if set(range(start, end)) & protected or folded[start:end] != target:
                    continue
                del words[start:end]
                del folded[start:end]
                rebuild_protected()
                removed = True
                break
            if not removed:
                break
    while len(" ".join(words)) > max_chars:
        removable = [index for index, word in enumerate(words) if index not in protected and (_DIMENSION_WORD.fullmatch(word) or _key(word) in _MARKETING_NAME_WORDS)]
        if not removable:
            removable = [index for index in range(len(words)) if index not in protected]
        if not removable:
            raise NamingReviewRequired("product_name_exceeds_50_characters")
        del words[removable[-1]]
        del folded[removable[-1]]
        rebuild_protected()
    result = " ".join(words)
    if len(result) > max_chars:
        raise NamingReviewRequired("product_name_exceeds_50_characters")
    return result


def _canonical(value: object, field: str, vocabulary: tuple[str, ...]) -> str:
    normalized = _key(value)
    if not normalized:
        raise NamingReviewRequired(f"{field}_missing")
    choices = {_key(item): item for item in vocabulary}
    alias_target = _ALIASES.get(normalized)
    if alias_target:
        normalized = _key(alias_target)
    if normalized not in choices:
        raise NamingReviewRequired(f"{field}_outside_formal_vocabulary:{value}")
    return choices[normalized]


def canonicalize_attribute(value: object, field: str) -> str:
    """Canonicalize one governed attribute for shared resolver callers."""

    vocabulary = {
        "style": STYLE_VOCABULARY,
        "color": COLOR_VOCABULARY,
        "material": MATERIAL_VOCABULARY,
        "type": TYPE_VOCABULARY,
        "feature": FEATURE_VOCABULARY,
    }.get(str(field).strip().casefold())
    if vocabulary is None:
        raise ValueError(f"unsupported naming attribute: {field}")
    try:
        return _canonical(value, str(field), vocabulary)
    except NamingReviewRequired as error:
        raise ValueError(str(error)) from error


@dataclass(frozen=True)
class ProductNameParts:
    style: str
    color: str
    material: str
    product_type: str
    feature: str | None = None
    brand: str = ""
    naming_contract_version: str = NAMING_CONTRACT_V1
    brand_prefix_policy: str = "NONE"

    @property
    def product_name(self) -> str:
        return compose_product_name(
            style=self.style,
            color=self.color,
            material=self.material,
            product_type=self.product_type,
            feature=self.feature,
            brand=self.brand,
            brand_prefix_policy=self.brand_prefix_policy,
            contract_version=self.naming_contract_version,
        )


def _canonical_brand(value: object, *, required: bool = False) -> str:
    brand = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ").strip())
    if not brand:
        if required:
            raise NamingReviewRequired("brand_missing_for_required_prefix")
        return ""
    if len(brand) > 80 or any(ord(char) < 32 for char in brand):
        raise NamingReviewRequired("brand_display_name_invalid")
    if re.search(r"\d|\b(?:inch|in|cm|mm|w|d|h)\b", brand.casefold()):
        raise NamingReviewRequired("brand_display_name_contains_dimension")
    return brand


def _resolve_brand_policy(brand_prefix_policy: object, brand: object) -> str:
    policy = str(brand_prefix_policy or "NONE").strip().upper()
    if policy not in BRAND_PREFIX_POLICIES:
        raise NamingReviewRequired(f"brand_prefix_policy_invalid:{policy}")
    if policy == "REQUIRED":
        _canonical_brand(brand, required=True)
    return policy


def compose_product_name(
    *, style: object, color: object, material: object, product_type: object,
    feature: object | None = None,
    brand: object = "",
    brand_prefix_policy: object = "NONE",
    contract_version: object | None = None,
) -> str:
    """Compose the canonical public name for a source profile.

    Contract v1 is ``Style + Color + Material + Optional Feature + Type``.
    Contract v2 prepends ``Brand`` when the source profile requires or opts
    into a brand prefix.  The source title, model number, dimensions and
    arbitrary qualifiers are never accepted as name components.

    No source title, brand, model word, dimension, or free-form qualifier is
    accepted here.  Invalid or missing evidence is a review condition.
    """

    policy = _resolve_brand_policy(brand_prefix_policy, brand)
    canonical_brand = _canonical_brand(brand, required=policy == "REQUIRED")
    version = str(contract_version or (NAMING_CONTRACT_V2 if policy != "NONE" else NAMING_CONTRACT_V1)).strip()
    if version not in {NAMING_CONTRACT_V1, NAMING_CONTRACT_V2, NAMING_CONTRACT_V2_BRAND_DIRECT, NAMING_CONTRACT_V3_MARKETPLACE}:
        raise NamingReviewRequired(f"naming_contract_version_invalid:{version}")
    if version in {NAMING_CONTRACT_V2, NAMING_CONTRACT_V2_BRAND_DIRECT} and policy == "REQUIRED" and not canonical_brand:
        raise NamingReviewRequired("brand_missing_for_naming_contract_v2")
    if version == NAMING_CONTRACT_V3_MARKETPLACE and policy != "NONE":
        raise NamingReviewRequired("marketplace_contract_cannot_require_brand_prefix")
    if version == NAMING_CONTRACT_V2_BRAND_DIRECT and policy != "REQUIRED":
        raise NamingReviewRequired("brand_direct_contract_requires_brand_prefix")
    canonical_feature = None
    if feature not in (None, ""):
        if version not in {NAMING_CONTRACT_V1, NAMING_CONTRACT_V3_MARKETPLACE}:
            raise NamingReviewRequired("feature_not_allowed_by_contract")
        canonical_feature = _canonical(feature, "feature", FEATURE_VOCABULARY)
    parts = ProductNameParts(
        style=_canonical(style, "style", STYLE_VOCABULARY),
        color=_canonical(color, "color", COLOR_VOCABULARY),
        material=_canonical(material, "material", MATERIAL_VOCABULARY),
        product_type=_canonical(product_type, "type", TYPE_VOCABULARY),
        feature=canonical_feature,
        brand=canonical_brand,
        naming_contract_version=version,
        brand_prefix_policy=policy,
    )
    values = [parts.style, parts.color, parts.material]
    if parts.feature:
        values.append(parts.feature)
    values.append(parts.product_type)
    core_name = " ".join(values)
    result = f"{parts.brand} {core_name}".strip() if parts.brand and policy in {"REQUIRED", "OPTIONAL"} else core_name
    return shorten_name_to_limit(
        result,
        required_prefix=parts.brand if parts.brand and policy in {"REQUIRED", "OPTIONAL"} else "",
        required_type=parts.product_type,
        removable_phrases=tuple(item for item in (parts.feature, parts.material, parts.color, parts.style) if item),
    )


def compose_official_name(
    *,
    source_name: object,
    verified_type: object,
    brand: object,
    max_chars: int = MAX_FINAL_NAME_CHARS,
) -> str:
    """Build a direct-brand name from official title evidence and verified type.

    Direct-brand pages are allowed to retain an official series/product name;
    they do not have to be forced through the four-attribute marketplace
    vocabulary.  The brand and verified type remain mandatory, and shortening
    removes complete optional words only.
    """

    canonical_brand = _canonical_brand(brand, required=True)
    source = _name_text(source_name)
    verified = standardize_product_type(verified_type)
    if source.casefold().startswith(f"{canonical_brand} ".casefold()):
        source = source[len(canonical_brand):].strip()
    if not _key(source).endswith(_key(verified)):
        source = f"{source} {verified}".strip()
    result = f"{canonical_brand} {source}".strip()
    return shorten_name_to_limit(
        result,
        max_chars=max_chars,
        required_prefix=canonical_brand,
        required_type=verified,
        removable_phrases=("New", "Exclusive", "Collection", "Premium", "Signature", "Limited Edition"),
    )


def compose_brand_official_name(
    *,
    brand: object,
    official_name: object,
    style: object = "",
    color: object = "",
    material: object = "",
    product_type: object = "",
    max_chars: int = MAX_FINAL_NAME_CHARS,
) -> str:
    """品牌库命名：品牌前缀 + 官方名 + 风格/颜色/材质/类型。

    有官方名时先提取官方名（产品系列身份），再用四段式（风格/颜色/材质/类型）
    表达，品牌前缀必填。名称过长时优先移除风格/颜色/材质等可选词，
    始终保留品牌前缀、官方名与类型。
    """

    canonical_brand = _canonical_brand(brand, required=True)
    official = _name_text(official_name)
    if official.casefold().startswith(f"{canonical_brand} ".casefold()):
        official = official[len(canonical_brand):].strip()
    type_text = ""
    if str(product_type or "").strip():
        try:
            verified = standardize_product_type(product_type)
        except NamingReviewRequired:
            verified = ""
        if verified and not _key(official).endswith(_key(verified)):
            type_text = verified
    attrs = [str(item).strip() for item in (style, color, material) if str(item or "").strip()]
    words = [canonical_brand, official] + attrs
    if type_text:
        words.append(type_text)
    candidate = " ".join(words)
    return shorten_name_to_limit(
        candidate,
        max_chars=max_chars,
        required_prefix=canonical_brand,
        required_type=type_text,
        removable_phrases=tuple(attrs) + ("New", "Exclusive", "Collection", "Premium", "Signature", "Limited Edition"),
    )


def _type_candidates(evidence: object) -> list[str]:
    text = _key(evidence)
    if not text:
        return []
    matches = [
        item for item in TYPE_VOCABULARY
        if re.search(rf"(?<![a-z]){re.escape(_key(item))}(?![a-z])", text)
    ]
    return sorted(matches, key=lambda item: (-len(_key(item)), TYPE_VOCABULARY.index(item)))


def standardize_product_type(evidence: object) -> str:
    """Deterministically identify one standard type from explicit evidence."""

    direct = _key(evidence)
    for item in TYPE_VOCABULARY:
        if _key(item) == direct:
            return item
    alias_target = _ALIASES.get(direct)
    if alias_target and _key(alias_target) in {_key(item) for item in TYPE_VOCABULARY}:
        return next(item for item in TYPE_VOCABULARY if _key(item) == _key(alias_target))
    candidates = _type_candidates(evidence)
    if not candidates:
        raise NamingReviewRequired("type_missing_or_unrecognized")
    if len({_key(item) for item in candidates}) > 1:
        longest = len(_key(candidates[0]))
        if any(len(_key(item)) == longest for item in candidates[1:]):
            raise NamingReviewRequired("type_ambiguous_in_source_evidence")
    return candidates[0]


def validate_product_name(
    value: object,
    *,
    brand: object = "",
    brand_prefix_policy: object = "NONE",
    contract_version: object | None = None,
) -> ProductNameParts:
    """Validate an existing public name under an explicit source contract."""

    candidate = re.sub(r"\s+", " ", str(value or "").strip())
    if not candidate:
        raise NamingReviewRequired("product_name_missing")
    if len(candidate) > MAX_FINAL_NAME_CHARS:
        raise NamingReviewRequired("product_name_exceeds_50_characters")
    if candidate != str(value or "").strip() or "  " in str(value or ""):
        raise NamingReviewRequired("product_name_whitespace_invalid")
    policy = _resolve_brand_policy(brand_prefix_policy, brand)
    expected_brand = _canonical_brand(brand, required=policy == "REQUIRED")
    version = str(contract_version or (NAMING_CONTRACT_V2 if policy != "NONE" else NAMING_CONTRACT_V1)).strip()
    if version not in {NAMING_CONTRACT_V1, NAMING_CONTRACT_V2, NAMING_CONTRACT_V2_BRAND_DIRECT, NAMING_CONTRACT_V3_MARKETPLACE}:
        raise NamingReviewRequired(f"naming_contract_version_invalid:{version}")
    if version == NAMING_CONTRACT_V3_MARKETPLACE and policy != "NONE":
        raise NamingReviewRequired("marketplace_contract_cannot_require_brand_prefix")
    if version == NAMING_CONTRACT_V2_BRAND_DIRECT and policy != "REQUIRED":
        raise NamingReviewRequired("brand_direct_contract_requires_brand_prefix")
    core_candidate = candidate
    if policy == "REQUIRED":
        prefix = expected_brand + " "
        if not candidate.casefold().startswith(prefix.casefold()):
            raise NamingReviewRequired("product_name_missing_required_brand_prefix")
        core_candidate = candidate[len(prefix):].strip()
    elif policy == "NONE" and expected_brand:
        raise NamingReviewRequired("brand_not_allowed_by_source_profile")

    # Try the finite vocabulary product space; this also prevents dimensions,
    # model words and extra free-form qualifiers from entering the name.
    folded = _key(core_candidate)
    for style in STYLE_VOCABULARY:
        for color in COLOR_VOCABULARY:
            for material in MATERIAL_VOCABULARY:
                prefix = _key(f"{style} {color} {material}")
                if not folded.startswith(prefix + " "):
                    continue
                remainder = core_candidate[len(f"{style} {color} {material}"):].strip()
                for type_value in sorted(TYPE_VOCABULARY, key=lambda item: (-len(item.split()), TYPE_VOCABULARY.index(item))):
                    type_suffix = " " + type_value
                    if not remainder.casefold().endswith(type_suffix.casefold()) and _key(remainder) != _key(type_value):
                        continue
                    before_type = remainder[:-len(type_suffix)].strip() if _key(remainder) != _key(type_value) else ""
                    feature = None
                    for feature_value in sorted(FEATURE_VOCABULARY, key=lambda item: (-len(item.split()), item.casefold())):
                        if _key(before_type).endswith(_key(feature_value)):
                            feature = feature_value
                            before_type = before_type[: -(len(feature_value))].strip()
                            break
                    if not before_type and feature:
                        # A feature cannot replace the required style/color/material tuple.
                        continue
                    product_type = type_value
                    if _key(remainder) == _key(type_value) or before_type or feature is not None:
                        canonical = compose_product_name(
                            style=style,
                            color=color,
                            material=material,
                            feature=feature,
                            product_type=type_value,
                            brand=expected_brand,
                            brand_prefix_policy=policy,
                            contract_version=version,
                        )
                        if candidate != canonical:
                            raise NamingReviewRequired("product_name_not_title_case")
                        return ProductNameParts(
                            style, color, material, type_value, feature=feature,
                            brand=expected_brand,
                            naming_contract_version=version,
                            brand_prefix_policy=policy,
                        )
    raise NamingReviewRequired("product_name_not_exactly_style_color_material_type")


def validate_official_name(value: object, *, brand: object, verified_type: object | None = None) -> str:
    """Validate a direct-brand official name without inventing a vocabulary title."""

    candidate = re.sub(r"\s+", " ", str(value or "").strip())
    if not candidate:
        raise NamingReviewRequired("product_name_missing")
    if len(candidate) > MAX_FINAL_NAME_CHARS:
        raise NamingReviewRequired("product_name_exceeds_50_characters")
    if candidate != str(value or "").strip() or "  " in str(value or ""):
        raise NamingReviewRequired("product_name_whitespace_invalid")
    expected_brand = _canonical_brand(brand, required=True)
    prefix = expected_brand + " "
    if not candidate.casefold().startswith(prefix.casefold()):
        raise NamingReviewRequired("product_name_missing_required_brand_prefix")
    core = candidate[len(prefix):].strip()
    if not core:
        raise NamingReviewRequired("official_product_name_missing")
    if re.search(r"(?:\bcgtrader\b|\b3d\s*model\b|\bfree\b|\b(?:sku|model)\s*[-#:]*\s*[a-z0-9-]+\b|\d+(?:\.\d+)?\s*(?:inches?|in|cm|mm|m|w|d|h)\b)", candidate, re.I):
        raise NamingReviewRequired("product_name_safety_invalid")
    if verified_type not in (None, ""):
        verified = standardize_product_type(verified_type)
        if not _key(core).endswith(_key(verified)):
            raise NamingReviewRequired("official_product_name_missing_verified_type")
    else:
        standardize_product_type(core)
    return candidate


__all__ = [
    "COLOR_VOCABULARY",
    "BRAND_PREFIX_POLICIES",
    "FEATURE_VOCABULARY",
    "MATERIAL_VOCABULARY",
    "NAMING_CONTRACT_V1",
    "NAMING_CONTRACT_V2",
    "NAMING_CONTRACT_V2_BRAND_DIRECT",
    "NAMING_CONTRACT_V3_MARKETPLACE",
    "NAMING_RULE_VERSION",
    "NamingReviewRequired",
    "ProductNameParts",
    "STYLE_VOCABULARY",
    "TYPE_VOCABULARY",
    "VOCABULARY_PATH",
    "canonicalize_attribute",
    "compose_brand_official_name",
    "compose_official_name",
    "compose_product_name",
    "shorten_name_to_limit",
    "standardize_product_type",
    "validate_official_name",
    "validate_product_name",
]
