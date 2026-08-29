# Public Website development boundary

This repository is the Website control plane. It is intentionally independent of the separate Skills package and must not import, spawn, copy, or read a Skills checkout.

Preserve these contracts when changing code:

- `workflow-event.v2` and receipt-backed delivery records
- durable job, scan, queue, resume, cancel, and human-review state
- Source Policy, product identity, media binding, and production-gate semantics
- exact-count evidence rules: inaccessible data is `UNKNOWN` or `ESTIMATED`, never a guessed zero
- provider safety: explicit configuration, idempotency, submission-unknown quarantine, and cost approval

Public-site collection must respect the site's terms, robots policy, rate limits, and access controls. Do not bypass CAPTCHA/WAF/login, copy cookies or browser profiles, or infer private data. Keep local configuration, databases, captured media, logs, and generated models out of Git.

Every change to taxonomy, counts, identity, queue recovery, provider safety, or delivery behavior needs a focused regression test. Run the Python tests and the Web typecheck/lint/build before publishing.
