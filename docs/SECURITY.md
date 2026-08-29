# Security and publication policy

This repository is intended to be safe for public source review.

## Never commit

- API keys, access tokens, passwords, cookies, session headers, or private certificates;
- `.env.local`, production databases, browser profiles, captured customer/product evidence, logs, or generated GLBs;
- personal names, contact details, account identifiers, internal hostnames, local absolute paths, or private company documents;
- unredacted incident reports or screenshots that contain any of the above.

The repository keeps provider variable names and adapter interfaces so the architecture is understandable, but all values in the templates are empty or local-only placeholders.

## Runtime rules

- Provider calls are disabled by default.
- Paid calls require explicit UI authorization and a persisted cost ceiling.
- A provider create request with an unknown outcome is not automatically retried.
- Public collection never bypasses CAPTCHA, WAF, authentication, robots policy, rate limits, or terms of service.
- Exact counts require direct evidence; uncertainty is recorded instead of hidden.

## Before a public push

```powershell
git grep -n -I -E "(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]|Bearer[[:space:]]+[A-Za-z0-9._-]{16,}|BEGIN (RSA|EC|OPENSSH) PRIVATE KEY)" -- . ':!package-lock.json'
git status --short
```

Review the diff manually. A clean grep is necessary but not sufficient: inspect new screenshots, fixtures, reports, and configuration files as well.
