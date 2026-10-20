import json
from contextlib import asynccontextmanager


@asynccontextmanager
async def open_call(url: str):
    """One initialized Scalable MCP session, yielding `call(tool, args) -> dict` (parsed JSON; an `is_error`
    result raises RuntimeError). `auth_hermes.bootstrap()` must have run: it precedes any `mcp` import."""
    from fetcher.auth_hermes import build_auth
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    http_client = create_mcp_http_client(auth=build_auth(url))
    async with http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()

                async def call(tool: str, args: dict) -> dict:
                    result = await session.call_tool(tool, args)
                    text = result.content[0].text
                    if result.is_error:
                        raise RuntimeError(f"{tool} failed: {text}")
                    return json.loads(text)

                call.session = session   # run.py reads the tool list from it
                yield call
