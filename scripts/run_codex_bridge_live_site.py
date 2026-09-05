"""Run the existing Website Brain agent loop through the Codex bridge on a live site."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.services.brain_provider import BrainSettings, WebsiteBrainProvider
from app.services.native_site_analysis import NativeSiteAnalyzer
from workers.scrape.http_client import SafeHttpClient


def main() -> int:
    url = os.getenv("CODEX_BRIDGE_LIVE_URL", "https://www.interiordefine.com/").strip()
    root = Path(os.environ["CODEX_BRIDGE_ROOT"]).resolve()
    client = SafeHttpClient(source_url=url, request_budget=24, timeout=25, request_delay=0.15)
    html = client.get_html(url)
    analyzer = NativeSiteAnalyzer(
        root / "website-output",
        brain=WebsiteBrainProvider(settings=BrainSettings(
            model_mode="CODEX_DEVELOPMENT_BRIDGE",
            bridge_root=str(root),
            bridge_timeout_seconds=900,
            bridge_poll_seconds=0.1,
            agent_max_steps=8,
        )),
    )
    categories, signals = analyzer._l0_l1(url, html)
    result, receipt = analyzer._brain_agent_taxonomy(
        url,
        {**signals, "live_http": client.telemetry(), "html_sha256": hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()},
        categories,
        client,
        output_dir=root / "website-output",
    )
    print({"url": url, "validated": result.model_dump(mode="json") if result else None, "receipt": receipt, "http": client.telemetry()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
