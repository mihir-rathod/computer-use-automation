"""An MCP server that exposes the platform's recorded capabilities as tools, so any MCP-capable assistant (Claude Desktop, an IDE
agent, an agent someone else built) can use a legacy system through them.

What it is: a thin, honest client of the authenticated `/v1` API. It adds no abilities of its own.
  * Every tool is one saved capability, with its input schema and risk metadata taken from the artifact. Nothing can be called that has
    not been recorded and reviewed; discovery (a model exploring a page) is not exposed.
  * A call is an ordinary run: same validation, caps, idempotency and approvals. The assistant uses its own API key, so everything it does
    is recorded under that key's name, and a key's role is its ceiling. There is deliberately no tool to approve, so an assistant can never
    approve its own request.
  * A tool that commits something REQUIRES an `idempotency_key`, so an assistant that retries (they do) cannot post twice.
  * A request that needs an approval returns at once with "pending approval" and the run id; `get_run_status` follows it up.

Run it:   CUA_API_URL=http://127.0.0.1:8020 CUA_API_KEY=cua_... uv run python mcp_server.py        (stdio transport)
Claude Desktop config: see README. Streamable-HTTP transport is not implemented."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx
import mcp.types as types
from mcp.server.lowlevel import Server

SERVER_NAME = "capability-platform"
_TERMINAL = {"success", "business_outcome", "hard_failure", "needs_review", "dry_run", "abandoned", "rejected"}
GET_RUN_STATUS = "get_run_status"
INSTRUCTIONS = (
    "Each tool is a recorded, reviewed operation on a back-office system. Read-only tools return their answer directly. Tools that change "
    "something may need a person's approval first: you will be told the run is pending approval, and you cannot approve it yourself. For tools "
    "that commit something you must pass an idempotency_key; reuse the SAME key if you retry. If a result says the outcome is unknown "
    "(needs_review), do not retry: report it to the user."
)


class Bridge:
    """The logic behind the tools, separate from the MCP plumbing so it can be tested directly."""

    def __init__(self, api_url: str, api_key: str, targets: dict[str, str] | None = None, wait_seconds: float = 60.0,
                 client: httpx.AsyncClient | None = None):
        self.targets = targets or {"clinic": "clinic", "mockbank": "mockbank"}
        self.wait_seconds = wait_seconds
        self.http = client or httpx.AsyncClient(base_url=api_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
        self._catalog: dict[str, dict[str, Any]] = {}

    # ---- tools -------------------------------------------------------------------------------------------------

    async def tools(self) -> list[types.Tool]:
        response = await self.http.get("/v1/capabilities")
        self._raise(response)
        tools = []
        self._catalog = {}
        for cap in response.json()["capabilities"]:
            if cap["login"] is None:
                continue  # a sign-on capability is plumbing, not something to hand an assistant
            self._catalog[cap["capability_id"]] = cap
            tools.append(self._tool(cap))
        tools.append(types.Tool(
            name=GET_RUN_STATUS, title="Check on a run", description="Look up the status and result of a run you submitted earlier, for example one that was waiting for approval.",
            inputSchema={"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"], "additionalProperties": False},
            annotations=types.ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)))
        return tools

    @staticmethod
    def _tool(cap: dict[str, Any]) -> types.Tool:
        risk, schema = cap["risk"], cap["input_schema"]
        commits = risk["has_irreversible_step"]
        properties = {k: {kk: vv for kk, vv in v.items()} for k, v in schema["properties"].items()}
        required = list(schema["required"])
        if commits:
            properties["idempotency_key"] = {"type": "string", "minLength": 8, "description": "A unique id for this request. If you retry it, send the SAME key, so it cannot be done twice."}
            properties["dry_run"] = {"type": "boolean", "description": "Rehearse up to, but not including, the irreversible step. Nothing is changed."}
            required.append("idempotency_key")
        input_schema: dict[str, Any] = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
        if schema.get("at_least_one_of"):
            input_schema["anyOf"] = [{"required": [f]} for f in schema["at_least_one_of"]]
        if commits:
            note = (f"CHANGES STATE AND CANNOT BE UNDONE FROM HERE. Needs a {risk['approval_required']} approval before it runs: the call returns "
                    "'pending approval' immediately and a person must approve it; you cannot.")
        elif risk["level"] == "state_changing":
            note = "Changes state (reversible)."
        else:
            note = "Read-only."
        return types.Tool(
            name=cap["capability_id"], title=cap["name"], description=f"{cap['description']} {note}", inputSchema=input_schema,
            annotations=types.ToolAnnotations(read_only_hint=(risk["level"] == "read_only" and not commits), destructive_hint=commits,
                                              idempotent_hint=(risk["level"] == "read_only" or commits), open_world_hint=True))

    # ---- calls -------------------------------------------------------------------------------------------------

    async def call(self, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        arguments = dict(arguments or {})
        try:
            if name == GET_RUN_STATUS:
                run_id = arguments.get("run_id")
                if not run_id:
                    return self._error("run_id is required")
                response = await self.http.get(f"/v1/runs/{run_id}")
                self._raise(response)
                return self._present(response.json())
            if not self._catalog:
                await self.tools()
            cap = self._catalog.get(name)
            if cap is None:
                return self._error(f"unknown tool '{name}'")
            return await self._run(cap, arguments)
        except httpx.HTTPStatusError as exc:
            return self._error(self._explain(exc.response))
        except httpx.HTTPError as exc:
            return self._error(f"could not reach the platform API: {exc}")

    async def _run(self, cap: dict[str, Any], arguments: dict[str, Any]) -> types.CallToolResult:
        key = arguments.pop("idempotency_key", None)
        dry_run = bool(arguments.pop("dry_run", False))
        if cap["risk"]["has_irreversible_step"] and not key:
            return self._error("this tool commits something, so it needs an idempotency_key (any unique string, reused if you retry)")
        target = self.targets.get(cap["app"])
        if target is None:
            return self._error(f"no target is configured for the '{cap['app']}' system")
        headers = {"Idempotency-Key": key} if key else {}
        response = await self.http.post("/v1/runs", json={"capability_id": cap["capability_id"], "params": arguments, "target": target, "dry_run": dry_run}, headers=headers)
        self._raise(response)
        body = response.json()
        deadline = time.monotonic() + self.wait_seconds
        while body["status"] not in _TERMINAL and body["status"] != "pending_approval" and time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            polled = await self.http.get(f"/v1/runs/{body['id']}")
            self._raise(polled)
            body = polled.json()
        return self._present(body, cap)

    # ---- formatting -----------------------------------------------------------------------------------------------

    @staticmethod
    def _error(message: str) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        response.raise_for_status()

    @staticmethod
    def _explain(response: httpx.Response) -> str:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        if response.status_code == 401:
            return "the platform rejected this server's API key (401); it is missing, wrong or revoked"
        if response.status_code == 403:
            return f"this server's API key is not allowed to do that (403): {detail}"
        return f"the platform refused the request ({response.status_code}): {detail}"

    def _present(self, run: dict[str, Any], cap: dict[str, Any] | None = None) -> types.CallToolResult:
        result = run.get("result") or {}
        status = run["status"]
        error = result.get("error") or {}
        structured = {"run_id": run.get("id") or result.get("run_id"), "status": status, "outputs": result.get("outputs"),
                      "business_outcome": result.get("business_outcome"), "committed": bool(run.get("committed")),
                      "deduplicated": bool(result.get("deduplicated")), "error": ({"code": error.get("code"), "message": error.get("message")} if error else None)}
        is_error = False
        if status == "pending_approval":
            tier = (result.get("approval_tier") or (run.get("approvals") or [{}])[-1].get("tier") or "an authorised")
            text = (f"Submitted but NOT done: this needs a {tier} approval from a person (run {structured['run_id']}). Nothing has changed. "
                    f"Tell the user; check later with {GET_RUN_STATUS}.")
        elif status == "needs_review":
            is_error = True
            text = (f"The outcome is UNKNOWN. The step was issued but could not be confirmed, so it was not retried. Do NOT retry or resubmit; "
                    f"tell the user a person must check the target system (run {structured['run_id']}). {error.get('message', '')}")
        elif status == "hard_failure":
            is_error = True
            text = f"Failed ({error.get('code')}): {error.get('message')}"
        elif status == "business_outcome":
            text = f"Completed with the answer '{result.get('business_outcome')}' (a normal result, not a system error)."
        elif status == "dry_run":
            text = "Dry run only: stopped before the irreversible step. Nothing was changed."
        elif status in ("queued", "running"):
            text = f"Still {status} after waiting; check later with {GET_RUN_STATUS} (run {structured['run_id']})."
        elif status == "rejected":
            text = f"A person rejected this request (run {structured['run_id']})."
        else:
            text = "Done." + (" It was already done by an earlier request with the same idempotency_key." if structured["deduplicated"] else "")
        text += "\n" + json.dumps(structured, indent=2)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structured_content=structured, is_error=is_error)


def build_server(bridge: Bridge) -> Server:
    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=await bridge.tools())

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await bridge.call(params.name, params.arguments)

    return Server(SERVER_NAME, version="1.0.0", instructions=INSTRUCTIONS, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def bridge_from_env() -> Bridge:
    key = os.environ.get("CUA_API_KEY")
    if not key:
        raise SystemExit("CUA_API_KEY is not set. Create one with: uv run python cli.py keys create --name my-assistant --role operator")
    targets = json.loads(os.environ["CUA_TARGETS"]) if os.environ.get("CUA_TARGETS") else None
    return Bridge(os.environ.get("CUA_API_URL", "http://127.0.0.1:8020"), key, targets, float(os.environ.get("CUA_WAIT_SECONDS", "60")))


async def main() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(bridge_from_env())
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    print("capability-platform MCP server starting on stdio", file=sys.stderr)
    asyncio.run(main())
