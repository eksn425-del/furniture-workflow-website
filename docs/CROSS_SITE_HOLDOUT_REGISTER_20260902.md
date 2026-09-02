# Cross-Site Maturity V2 — Holdout Register

Date: 2026-09-02  
Repository baseline: `bda39ea1e088f7f4a0d8dee690aaa92273c0a984`  
Purpose: freeze the zero-shot evaluation set before Generic Core changes.

These sites are reserved for the final holdout pass. During Development Set work,
the implementation must not add host-specific rules, adapters, selectors, or
fixtures for these hosts. A holdout may be classified as blocked when the public
site requires human verification, login, robots authorization, or an access
change; those outcomes are not counted as product success.

| Holdout site | Technical family hypothesis | Final zero-shot probe |
|---|---|---|
| Arhaus | Direct Brand / Retail | S0 → S1 → Exact-1/3 |
| Nathan James | SSR / Retail | S0 → S1 → Exact-1/3 |
| Kayu | Direct Brand / Retail | S0 → S1 → Exact-1/3 |
| Indian Hub | Retail / regional catalog | S0 → S1 → Exact-1/3 |
| Archive3D | 3D Asset Catalog | S0 → S1 → Exact-1/3 |
| GrabCAD | Marketplace / Asset Repository | S0 → S1 → Exact-1/3 |
| NASA 3D | Asset Repository | S0 → S1 → Exact-1/3 |
| Safavieh | Direct Brand / Retail | S0 → S1 → Exact-1/3 |

The list is intentionally cross-family and contains no dedicated V2 adapter
allowance. If a holdout fails, the first remediation decision must be whether a
Generic Core strategy is missing. A site-specific adapter is permissible only
after the report records evidence that the structure is genuinely unique.

Development and regression sites are defined separately in the final maturity
report. This register is immutable for the V2 run unless a site becomes
permanently unavailable; any replacement must be recorded as a new revision,
not silently swapped.
