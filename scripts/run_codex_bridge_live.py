"""Run one Website-native multi-turn Codex bridge trace on a public-shaped fixture.

The fixture is only a deterministic transport harness; every decision still
arrives through the bridge response files and every URL/image/dimension result
is produced by the same Website agent tool executor used for live scans.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.services.brain_provider import BrainSettings, WebsiteBrainProvider
from app.services.native_site_analysis import NativeSiteAnalyzer


class FixtureClient:
    def __init__(self, **_: object) -> None:
        self.html = """
        <html><body>
          <nav><a href='/chairs'>Chairs</a><a href='/tables'>Tables</a></nav>
          <h1>Oak Lounge Chair</h1>
          <a href='/products/oak-chair'>Oak Lounge Chair</a>
          <script type='application/ld+json'>{"@type":"Product","name":"Oak Lounge Chair","sku":"oak-1","url":"https://bridge.example/products/oak-chair","image":["https://bridge.example/media/oak-main.jpg","https://bridge.example/media/oak-lifestyle.jpg"]}</script>
          <p>Overall: 30 W x 32 D x 34 H in</p>
          <img src='/media/oak-main.jpg'><img src='/media/oak-lifestyle.jpg'>
        </body></html>
        """

    def get_html(self, _: str) -> str:
        return self.html

    def get_sitemap(self, _: str) -> str:
        return "<urlset></urlset>"

    def robots_sitemaps(self, _: str) -> list[str]:
        return []

    def telemetry(self) -> dict[str, int]:
        return {"request_count": 0, "request_budget": 48, "cache_hits": 0, "redirect_count": 0}


def main() -> int:
    root = Path(os.environ["CODEX_BRIDGE_ROOT"]).resolve()
    brain = WebsiteBrainProvider(settings=BrainSettings(
        model_mode="CODEX_DEVELOPMENT_BRIDGE",
        bridge_root=str(root),
        bridge_timeout_seconds=900,
        bridge_poll_seconds=0.1,
        agent_max_steps=8,
    ))
    analyzer = NativeSiteAnalyzer(root / "website-output", brain=brain, client_factory=FixtureClient)
    result, receipt = analyzer._brain_agent_taxonomy(
        "https://bridge.example/",
        {
            "platform": "UNKNOWN",
            "navigation_items": [{"label": "Chairs", "path": "/chairs", "count": None}],
            "navigation_tree": [],
        },
        [],
        FixtureClient(),
        output_dir=root / "website-output",
    )
    print({"validated": result.model_dump(mode="json") if result else None, "receipt": receipt})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
