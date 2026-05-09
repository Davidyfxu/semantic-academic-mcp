"""
Paper Search MCP Server — wraps the paper and author graph APIs for MCP clients.

Tools exposed:
  - paper_search: Search scholarly papers by natural language query.
  - author_search: Search authors by name via the author search endpoint.

Transport: SSE via ``mcp.sse_app()`` on Cloudflare Workers (Durable Object).
"""

import asyncio
import os
from typing import Any

import httpx
from workers import DurableObject

from exceptions import HTTPException, http_exception
from logger import logger

# ---------------------------------------------------------------------------
# Constants (overridable via Worker env bindings or os.environ)
# ---------------------------------------------------------------------------

USER_AGENT = "paper-mcp-server/1.0"
REQUEST_TIMEOUT = 15.0
MAX_RETRIES = 2


def _env_str(env: Any, key: str, default: str = "") -> str:
    """Resolve a string from Worker ``env`` then ``os.environ``."""
    if env is not None:
        val = getattr(env, key, None)
        if val is not None and str(val).strip():
            return str(val)
    return os.getenv(key, default)


def setup_server(env: Any = None):
    from mcp.server.fastmcp import Context, FastMCP
    from starlette.middleware.cors import CORSMiddleware

    paper_url = _env_str(env, "PAPER_URL", "https://ai4scholar.net")
    fallback_paper_api_key = _env_str(env, "PAPER_API_KEY", "")

    mcp = FastMCP(
        name="Paper Search",
        instructions="""Use this server to search scholarly papers by query, or search
authors by name. Paper results include title, abstract, year, citations, and links.
Author search supports optional field projection (including nested paper fields).""",
    )

    def _paper_api_key_from_context(ctx: Context) -> str:
        request = ctx.request_context.request
        if request is not None:
            paper_api_key = request.headers.get("x-paper-api-key", "").strip()
            if paper_api_key:
                return paper_api_key

        return fallback_paper_api_key

    def _build_headers(paper_api_key: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        headers["Authorization"] = f"Bearer {paper_api_key}"
        return headers

    async def _paper_request(
        method: str,
        path: str,
        *,
        paper_api_key: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        url = f"{paper_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1 + MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=_build_headers(paper_api_key),
                        params=params,
                        json=json_body,
                    )
                if response.is_success:
                    return response.json()

                detail = ""
                try:
                    body = response.json()
                    detail = body.get("message", response.text)
                except Exception:
                    detail = response.text

                if response.status_code == 401:
                    raise httpx.HTTPStatusError(
                        f"Unauthorized: {detail}",
                        request=response.request,
                        response=response,
                    )
                if response.status_code == 422:
                    raise httpx.HTTPStatusError(
                        f"Validation failed: {detail}",
                        request=response.request,
                        response=response,
                    )
                raise httpx.HTTPStatusError(
                    f"Paper API error ({response.status_code}): {detail}",
                    request=response.request,
                    response=response,
                )

            except httpx.TimeoutException:
                last_exc = httpx.TimeoutException(
                    f"Request to {url} timed out after {REQUEST_TIMEOUT}s"
                )
                logger.warning(
                    "paper_request_timeout",
                    attempt=attempt + 1,
                    max_attempts=1 + MAX_RETRIES,
                    url=url,
                )
            except httpx.HTTPStatusError:
                raise
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "paper_request_network_error",
                    attempt=attempt + 1,
                    max_attempts=1 + MAX_RETRIES,
                    error=str(exc),
                )

            if attempt < MAX_RETRIES:
                await asyncio.sleep(1.0 * (attempt + 1))

        raise last_exc  # type: ignore[misc]

    @mcp.tool()
    async def paper_search(
        query: str,
        ctx: Context,
        limit: int = 30,
    ) -> str:
        """Search scholarly papers.

        Parameters
        ----------
        query : str
            Search keywords (e.g. "instruction tuning", "RAG evaluation").
        limit : int
            Number of results (1-100). Default 30.
        """
        paper_api_key = _paper_api_key_from_context(ctx)
        if not paper_api_key:
            return (
                "Error: missing paper API key. Configure X-Paper-API-Key in "
                "your MCP client headers, or set PAPER_API_KEY on the Worker."
            )

        if not query.strip():
            return "Error: 'query' must be a non-empty string."
        if not (1 <= limit <= 100):
            return "Error: 'limit' must be between 1 and 100."

        params: dict[str, Any] = {
            "query": query.strip(),
            "limit": limit,
            "fields": (
                "title,abstract,year,citationCount,url,openAccessPdf,externalIds,fieldsOfStudy"
            ),
        }

        try:
            result = await _paper_request(
                "GET",
                "/graph/v1/paper/search",
                paper_api_key=paper_api_key,
                params=params,
            )
        except httpx.HTTPStatusError as exc:
            return f"Paper API error: {exc.response.status_code} — {exc}"
        except httpx.RequestError as exc:
            return f"Network error: {exc}"

        papers = result.get("data", []) if isinstance(result, dict) else []
        if not papers:
            return f'No papers found for query: "{query}"'

        lines: list[str] = [f'Papers matching "{query}" ({len(papers)} returned):\n']
        for idx, paper in enumerate(papers, start=1):
            title = paper.get("title") or "(no title)"
            abstract = paper.get("abstract") or "(no abstract)"
            abstract = abstract.replace("\n", " ").strip()
            if len(abstract) > 300:
                abstract = f"{abstract[:297]}..."
            year = paper.get("year", "N/A")
            citation_count = paper.get("citationCount", 0)
            paper_url_field = paper.get("url") or "N/A"
            fields = paper.get("fieldsOfStudy") or []
            fields_text = ", ".join(fields) if isinstance(fields, list) and fields else "N/A"
            open_access_pdf = (paper.get("openAccessPdf") or {}).get("url", "N/A")

            lines.append(
                f"{idx}. {title}\n"
                f"   Year: {year} | Citations: {citation_count}\n"
                f"   Fields: {fields_text}\n"
                f"   URL: {paper_url_field}\n"
                f"   PDF: {open_access_pdf}\n"
                f"   Abstract: {abstract}\n"
            )

        return "\n".join(lines)

    def _format_author_block(author: dict[str, Any], max_papers_shown: int = 30) -> list[str]:
        """Turn one author JSON object into readable lines (shape depends on requested fields)."""
        lines: list[str] = []
        name = author.get("name") or "(no name)"
        author_id = author.get("authorId") or "N/A"
        lines.append(f"{name}")
        lines.append(f"   authorId: {author_id}")
        if author.get("url"):
            lines.append(f"   URL: {author['url']}")
        for key in ("paperCount", "citationCount", "hIndex", "homepage"):
            if key in author and author[key] not in (None, ""):
                lines.append(f"   {key}: {author[key]}")
        aff = author.get("affiliations")
        if isinstance(aff, list) and aff:
            lines.append(f"   affiliations: {', '.join(str(x) for x in aff)}")

        papers = author.get("papers")
        if isinstance(papers, list) and papers:
            lines.append("   Papers:")
            shown = papers[:max_papers_shown]
            for p in shown:
                if not isinstance(p, dict):
                    continue
                title = p.get("title") or "(no title)"
                year = p.get("year", "N/A")
                pid = p.get("paperId") or ""
                suffix = f"  id={pid}" if pid else ""
                lines.append(f"     - [{year}] {title}{suffix}")
            extra = len(papers) - len(shown)
            if extra > 0:
                lines.append(
                    f"     ... {extra} more paper(s) not shown (narrow `fields` or `limit`)"
                )
        return lines

    @mcp.tool()
    async def author_search(
        query: str,
        ctx: Context,
        fields: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> str:
        """Search authors by plain-text name (Semantic Scholar graph API).

        Omit ``fields`` to get only ``authorId`` and ``name`` per author. Use a
        comma-separated list to request more data; use dot notation for nested
        paper fields, e.g. ``papers.title,papers.year``. Including ``papers``
        returns linked papers per author — combine with a modest ``limit`` to
        keep responses small and fast. Hyphenated query tokens may not match;
        prefer spaces.

        Parameters
        ----------
        query : str
            Plain-text author name search (no special query syntax).
        fields : str, optional
            Comma-separated fields to return (``authorId`` is always included).
        limit : int
            Max authors in this batch (1-1000). Default 30.
        offset : int
            Pagination offset into the result list (>= 0). Default 0.
        """
        paper_api_key = _paper_api_key_from_context(ctx)
        if not paper_api_key:
            return (
                "Error: missing paper API key. Configure X-Paper-API-Key in "
                "your MCP client headers, or set PAPER_API_KEY on the Worker."
            )

        if not query.strip():
            return "Error: 'query' must be a non-empty string."
        if not (1 <= limit <= 1000):
            return "Error: 'limit' must be between 1 and 1000."
        if offset < 0:
            return "Error: 'offset' must be >= 0."

        params: dict[str, Any] = {
            "query": query.strip(),
            "limit": limit,
            "offset": offset,
        }
        if fields and fields.strip():
            params["fields"] = fields.strip()

        try:
            result = await _paper_request(
                "GET",
                "/graph/v1/author/search",
                paper_api_key=paper_api_key,
                params=params,
            )
        except httpx.HTTPStatusError as exc:
            return f"Paper API error: {exc.response.status_code} — {exc}"
        except httpx.RequestError as exc:
            return f"Network error: {exc}"

        if not isinstance(result, dict):
            return "Error: unexpected response shape from author search API."

        authors = result.get("data", [])
        if not isinstance(authors, list):
            authors = []

        total = result.get("total", "?")
        next_offset = result.get("next")

        if not authors:
            return f'No authors found for query: "{query}" (total={total}, offset={offset}).'

        header = (
            f'Authors matching "{query}" ({len(authors)} in this batch, '
            f"total≈{total}, offset={offset}"
        )
        if next_offset is not None:
            header += f", next_offset={next_offset}"
        header += "):"

        lines: list[str] = [header]
        for idx, author in enumerate(authors, start=1):
            if not isinstance(author, dict):
                continue
            block = _format_author_block(author)
            lines.append(f"{idx}. {block[0]}")
            lines.extend(block[1:])
            lines.append("")

        return "\n".join(lines).rstrip()

    app = mcp.sse_app()
    app.add_exception_handler(HTTPException, http_exception)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return mcp, app


class FastMCPServer(DurableObject):
    def __init__(self, ctx, env):
        self.ctx = ctx
        self.env = env
        self.mcp, self.app = setup_server(env)

    async def on_fetch(self, request, env, ctx):
        import asgi

        return await asgi.fetch(self.app, request, self.env, self.ctx)


async def on_fetch(request, env):
    id = env.ns.idFromName("A")
    obj = env.ns.get(id)
    return await obj.fetch(request)
