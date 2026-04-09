"""
간단한 인터넷 검색 도구.

기본은 DuckDuckGo HTML 검색을 사용하고,
SERPAPI_API_KEY 가 있으면 SerpAPI 도 사용할 수 있다.
"""

import os
from dataclasses import dataclass
from html import unescape
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from ..logging_utils import get_logger

logger = get_logger("tools.web_search")


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str
    source: str


class WebSearchTool:
    """웹 검색 결과를 텍스트 형태로 수집한다."""

    def __init__(
        self,
        timeout: int = 15,
        provider: Optional[str] = None,
        serpapi_api_key: Optional[str] = None,
    ):
        self.timeout = timeout
        self.provider = provider or os.environ.get("WEB_SEARCH_PROVIDER", "duckduckgo")
        self.serpapi_api_key = serpapi_api_key or os.environ.get("SERPAPI_API_KEY")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0 Safari/537.36"
                )
            }
        )

    def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        """쿼리에 대한 검색 결과 목록을 반환한다."""
        query = query.strip()
        if not query:
            return []

        if self.provider == "serpapi" and self.serpapi_api_key:
            return self._search_serpapi(query, max_results=max_results)

        try:
            return self._search_duckduckgo(query, max_results=max_results)
        except Exception as exc:
            logger.warning("DuckDuckGo 검색 실패: %s", exc)
            if self.serpapi_api_key:
                return self._search_serpapi(query, max_results=max_results)
            raise

    def search_as_markdown(self, query: str, max_results: int = 5) -> str:
        """검색 결과를 마크다운 텍스트로 반환한다."""
        results = self.search(query, max_results=max_results)
        if not results:
            return f"'{query}' 검색 결과가 없습니다."

        lines = [f"## 웹 검색 결과: {query}"]
        for result in results:
            lines.append(f"- [{result.title}]({result.url})")
            if result.snippet:
                lines.append(f"  {result.snippet}")
        return "\n".join(lines)

    def _search_duckduckgo(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        """DuckDuckGo HTML 결과를 파싱한다."""
        response = self.session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=self.timeout,
        )
        response.raise_for_status()

        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:
            raise ImportError(
                "웹 검색 파싱에는 'beautifulsoup4' 패키지가 필요합니다. requirements.txt 를 다시 설치해 주세요."
            ) from exc

        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        for result_node in soup.select(".result"):
            link_node = result_node.select_one(".result__a")
            if not link_node:
                continue

            raw_href = link_node.get("href", "")
            resolved_url = self._unwrap_duckduckgo_url(raw_href)
            snippet_node = result_node.select_one(".result__snippet")

            results.append(
                WebSearchResult(
                    title=link_node.get_text(" ", strip=True),
                    url=resolved_url,
                    snippet=snippet_node.get_text(" ", strip=True) if snippet_node else "",
                    source="duckduckgo",
                )
            )

            if len(results) >= max_results:
                break

        return results

    def _search_serpapi(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        """SerpAPI JSON 검색."""
        response = self.session.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google",
                "q": query,
                "api_key": self.serpapi_api_key,
                "num": max_results,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("organic_results", [])[:max_results]:
            results.append(
                WebSearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source="serpapi",
                )
            )
        return results

    def _unwrap_duckduckgo_url(self, raw_href: str) -> str:
        """DuckDuckGo redirect 링크를 실제 URL 로 푼다."""
        if not raw_href:
            return ""

        parsed = urlparse(raw_href)
        if parsed.netloc and parsed.scheme:
            return raw_href

        query = parse_qs(parsed.query)
        uddg = query.get("uddg")
        if uddg:
            return unescape(uddg[0])

        if raw_href.startswith("//"):
            return "https:" + raw_href

        if raw_href.startswith("/"):
            return "https://duckduckgo.com" + raw_href

        return raw_href
