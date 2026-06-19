"""Minimal MCP stdio client smoke test: spawn the server, call tools, print results.

Run: PYTHONPATH=src python tests/mcp_smoke.py
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    params = StdioServerParameters(command=sys.executable, args=["-m", "cpkg.mcp_server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])

            async def call(name, **args):
                res = await session.call_tool(name, args)
                text = res.content[0].text if res.content else "{}"
                return json.loads(text)

            for label, (name, args) in {
                "resolve(Vietcombank)": ("resolve_ticker", {"query": "Vietcombank"}),
                "resolve(Ngân hàng Ngoại thương)": ("resolve_ticker", {"query": "Ngân hàng Ngoại thương"}),
                "list_companies": ("list_companies", {}),
                "get_governance(VCB)": ("get_governance", {"ticker_or_name": "VCB", "include_narrative": False}),
                "get_company_profile(Vietcombank)": ("get_company_profile", {"ticker_or_name": "Vietcombank", "include_narrative": False}),
                "get_dividends(VCB)": ("get_dividends", {"ticker_or_name": "VCB"}),
                "search_facts": ("search_facts", {"query": "mô hình kinh doanh bán buôn", "ticker_or_name": "VCB", "limit": 3}),
            }.items():
                out = await call(name, **args)
                print(f"\n## {label}")
                print(json.dumps(out, ensure_ascii=False)[:600])


if __name__ == "__main__":
    asyncio.run(main())
