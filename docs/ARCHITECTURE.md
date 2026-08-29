# Architecture overview

The Website is a local-first control plane with a small number of explicit boundaries.

## Request and state flow

```text
Next.js UI
  → FastAPI control plane
  → durable SQLite read/write model
  → native scan/production runtime
  → append-only workflow-event.v2 stream
  → candidate pool, receipts, and delivery read model
```

The UI never treats a page label or stdout line as durable truth. The API persists scans, category evidence, job policy, provider qualification, runtime events, and delivery records. A job can be resumed or cancelled without losing its audit trail.

## Acquisition

`native_site_analysis.py` performs URL preflight and category discovery. `product_acquisition.py` discovers public product candidates using bounded HTTP/HTML, sitemap, structured-data, and site-owned public API paths when a site exposes them. Each candidate carries source evidence, identity fields, media binding status, scope status, and deduplication keys.

The adapter is deliberately extensible: generic sites use the conservative fallback; a site-specific protocol is added only when it is public, reproducible, and covered by a regression fixture. Access challenges remain visible and do not become a guessed empty result.

## Decision boundary

`brain_provider.py` keeps the future deployment modes separate from local testing:

- `LOCAL_AGENT`: a local reviewer supplies an explicit, traceable review receipt;
- `MULTIMODAL_SINGLE_MODEL`: one configured model can reason over text and images;
- `TEXT_BRAIN_PLUS_VISION`: text planning and image review are separate configured roles.

The Website does not silently alias legacy provider credentials into the Brain namespace.

## Modeling and delivery

`production_pipeline.py` applies the same Source Policy and Production Gate before a provider call. `modeling_provider.py` is an optional Lux3D adapter. The default provider is OFF. A configured provider still needs a qualification receipt, explicit cost authorization, an idempotency key, and a known-task resume path. Unknown submission outcomes are quarantined rather than blindly retried.
