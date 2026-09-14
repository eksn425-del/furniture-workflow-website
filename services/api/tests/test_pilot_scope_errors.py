from pathlib import Path

import pytest

from app.services.brain_provider import AgentToolError
from app.services.native_site_analysis import NativeSiteAnalyzer
from workers.scrape.http_client import AccessControlDetected, HttpStatusError, RobotsDenied


@pytest.mark.parametrize("failure", [
    RobotsDenied("robots.txt disallows URL"),
    AccessControlDetected("waf"),
    AccessControlDetected("login"),
    HttpStatusError(403, retryable=False),
])
def test_ambiguous_denial_remains_terminal_even_after_success(tmp_path: Path, failure: Exception) -> None:
    class Client:
        urls: list[str] = []

        def get_html(self, url: str) -> str:
            self.urls.append(url)
            if url.endswith("/blocked"):
                raise failure
            return "<h1>Public catalog</h1>"

    client = Client()
    execute = NativeSiteAnalyzer(tmp_path)._agent_tool_executor(client, "https://example.test/")
    assert execute("browse", {"url": "/public"})["html_bytes"] > 0
    with pytest.raises(AgentToolError) as denied:
        execute("browse", {"url": "/blocked"})
    assert denied.value.terminal
    assert denied.value.code == type(failure).__name__
    for target in ("/blocked", "/public", "https://other.test/"):
        with pytest.raises(AgentToolError) as repeated:
            execute("browse", {"url": target})
        assert repeated.value is denied.value
    assert client.urls == ["https://example.test/public", "https://example.test/blocked"]


def test_browse_skips_malformed_href_and_retains_valid_navigation(tmp_path: Path) -> None:
    class Client:
        def get_html(self, url: str) -> str:
            return '<a href="http://[">Invalid</a><a href="/chairs">Chairs</a>'

    execute = NativeSiteAnalyzer(tmp_path)._agent_tool_executor(Client(), "https://example.test/")
    result = execute("browse", {"url": "/"})
    assert result["nav_items"] == [{"href": "https://example.test/chairs", "label": "Chairs"}]
