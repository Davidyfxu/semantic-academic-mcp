# Semantic Academic MCP (Paper & Author Search)

Python [Cloudflare Worker](https://developers.cloudflare.com/workers/) that exposes an MCP server for scholarly paper and author search. It wraps the Semantic Scholar–compatible graph HTTP API (default host `https://ai4scholar.net`) using [FastMCP](https://github.com/jlowin/fastmcp) and serves clients over **SSE** (`mcp.sse_app()`).

The entrypoint is `src/worker.py`: a **Durable Object** (`FastMCPServer`) runs the Starlette ASGI app so the MCP session stays on one isolate.

## MCP tools

| Tool            | Description |
|-----------------|-------------|
| `paper_search`  | Search papers by natural language `query`. Returns title, abstract (truncated), year, citation count, fields of study, canonical URL, and open-access PDF link when available. Optional `limit` (1–100, default 30). |
| `author_search` | Search authors by plain-text name (`GET …/graph/v1/author/search`). Without `fields`, the API returns `authorId` and `name` per hit. Optional comma-separated `fields` (dot notation for nested paper fields, e.g. `papers.title,papers.year`), `limit` (1–1000, default 30), and `offset` for pagination. Use a small `limit` when requesting `papers` to limit payload size and latency (responses are capped around 10 MB). |

## Configuration

| Variable / header | Role |
|-------------------|------|
| `PAPER_URL` | Base URL for the paper API (Worker binding or env). Default: `https://ai4scholar.net`. |
| `PAPER_API_KEY` | Bearer token for the paper API (Worker secret or env). Used when the client does not send a key. |
| `X-Paper-API-Key` | Request header from the MCP client; if set, it overrides the Worker fallback for that request. |

If no key is available, `paper_search` and `author_search` return a clear error asking you to set the header or `PAPER_API_KEY`.

For production, set the API key as a Worker secret, for example:

```console
uv run pywrangler secret put PAPER_API_KEY
```

## MCP client configuration (`mcpServers` in `mcp.json`)

This server speaks **SSE**; the MCP stream is served at path **`/sse`** (for example `https://…/sse`).

In [Cursor](https://cursor.com/docs/mcp), add a file named `mcp.json` either at **`.cursor/mcp.json`** in the repo (project-wide) or at **`~/.cursor/mcp.json`** (global). The top-level object should contain an **`mcpServers`** map.

**Deployed Worker** — replace the URL with your real `*.workers.dev` (or custom domain) origin:

```json
{
  "mcpServers": {
    "paper-search": {
      "url": "https://semantic-academic-mcp.<your-subdomain>.workers.dev/sse",
      "headers": {
        "X-Paper-API-Key": "${env:PAPER_API_KEY}"
      }
    }
  }
}
```

If the Worker already has **`PAPER_API_KEY`** set as a secret, you can omit **`headers`** so the server uses the Worker env only.

**Local dev** — after `uv run pywrangler dev`, use the URL and port printed in the terminal (Wrangler’s default is often `http://127.0.0.1:8787`):

```json
{
  "mcpServers": {
    "paper-search-local": {
      "url": "http://127.0.0.1:8787/sse",
      "headers": {
        "X-Paper-API-Key": "${env:PAPER_API_KEY}"
      }
    }
  }
}
```

Set **`PAPER_API_KEY`** in your shell (or another env source Cursor reads) so **`${env:PAPER_API_KEY}`** resolves; avoid committing real keys into the JSON file.

Other MCP hosts that support remote HTTP/SSE often use the same **`url`** + optional **`headers`** shape under their own `mcpServers` configuration.

## Developing and deploying

```console
uv run pywrangler dev
```

```console
uv run pywrangler deploy
```

> [!NOTE]
> Python Workers bundles can be large. If deployment fails on the free tier, check [Worker size limits](https://developers.cloudflare.com/workers/platform/limits/#worker-size); you may need a paid plan.

## Testing

```console
uv run pytest tests
```

## Linting and formatting

This project uses Ruff:

```console
uv ruff format . --check
uv ruff check .
```

## IDE integration

After `uv sync`, point your editor at the project virtualenv interpreter (for example `.venv/bin/python`) for completions and type checking.
