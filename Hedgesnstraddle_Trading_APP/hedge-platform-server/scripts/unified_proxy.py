#!/usr/bin/env python3
"""Single-origin local proxy for Frappe + embedded trading panels."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from aiohttp import ClientSession, WSMsgType, web

HOP_BY_HOP_HEADERS = {
	"connection",
	"keep-alive",
	"proxy-authenticate",
	"proxy-authorization",
	"te",
	"trailer",
	"transfer-encoding",
	"upgrade",
}


def _target_url(base: str, path: str, query_string: str = "") -> str:
	url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
	if query_string:
		url = f"{url}?{query_string}"
	return url


def _copy_headers(headers) -> dict[str, str]:
	return {
		key: value
		for key, value in headers.items()
		if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {"host", "content-length"}
	}


def _runtime_prefix(request: web.Request) -> tuple[str | None, str | None, str]:
	path = request.path
	if path == "/hedge-runtime" or path.startswith("/hedge-runtime/"):
		stripped = path.removeprefix("/hedge-runtime") or "/panel"
		return "hedge", request.app["hedge_target"], stripped
	if path == "/straddle-runtime" or path.startswith("/straddle-runtime/"):
		stripped = path.removeprefix("/straddle-runtime") or "/"
		return "straddle", request.app["straddle_target"], stripped
	return None, request.app["frappe_target"], path


def _inject_runtime_prefix(html: str, prefix: str) -> str:
	script = f"""
<script>
(function() {{
  const prefix = "{prefix}";
  const rewrite = function(value) {{
    if (typeof value === "string" && value.startsWith("/api/")) return prefix + value;
    if (value && value.url && value.url.startsWith(window.location.origin + "/api/")) {{
      return new Request(prefix + new URL(value.url).pathname + new URL(value.url).search, value);
    }}
    return value;
  }};
  const nativeFetch = window.fetch;
  window.fetch = function(input, init) {{ return nativeFetch.call(this, rewrite(input), init); }};
  const NativeWebSocket = window.WebSocket;
  window.WebSocket = function(url, protocols) {{
    try {{
      const parsed = new URL(String(url), window.location.href);
      if (parsed.pathname === "/ws") parsed.pathname = prefix + "/ws";
      return protocols ? new NativeWebSocket(parsed.toString(), protocols) : new NativeWebSocket(parsed.toString());
    }} catch (e) {{
      return protocols ? new NativeWebSocket(url, protocols) : new NativeWebSocket(url);
    }}
  }};
}})();
</script>
"""
	html = html.replace('href="/api/', f'href="{prefix}/api/')
	html = html.replace("href='/api/", f"href='{prefix}/api/")
	html = html.replace('action="/api/', f'action="{prefix}/api/')
	html = html.replace("action='/api/", f"action='{prefix}/api/")
	if "</head>" in html:
		return html.replace("</head>", script + "\n</head>", 1)
	return script + html


async def _proxy_websocket(request: web.Request, target: str, path: str) -> web.WebSocketResponse:
	ws_server = web.WebSocketResponse()
	await ws_server.prepare(request)

	target_url = _target_url(target.replace("http://", "ws://").replace("https://", "wss://"), path, request.query_string)
	async with request.app["client"].ws_connect(target_url) as ws_client:
		async def client_to_server() -> None:
			async for msg in ws_server:
				if msg.type == WSMsgType.TEXT:
					await ws_client.send_str(msg.data)
				elif msg.type == WSMsgType.BINARY:
					await ws_client.send_bytes(msg.data)
				elif msg.type == WSMsgType.CLOSE:
					await ws_client.close()

		async def server_to_client() -> None:
			async for msg in ws_client:
				if msg.type == WSMsgType.TEXT:
					await ws_server.send_str(msg.data)
				elif msg.type == WSMsgType.BINARY:
					await ws_server.send_bytes(msg.data)
				elif msg.type == WSMsgType.CLOSE:
					await ws_server.close()

		tasks = [asyncio.create_task(client_to_server()), asyncio.create_task(server_to_client())]
		done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
		for task in pending:
			task.cancel()
		for task in done:
			with contextlib.suppress(asyncio.CancelledError):
				await task

	return ws_server


async def proxy(request: web.Request) -> web.StreamResponse:
	runtime, target, path = _runtime_prefix(request)
	if runtime and path == "/ws":
		return await _proxy_websocket(request, target, path)

	body = await request.read()
	target_url = _target_url(target, path, request.query_string)
	headers = _copy_headers(request.headers)

	try:
		async with request.app["client"].request(
			request.method,
			target_url,
			headers=headers,
			data=body if body else None,
			allow_redirects=False,
		) as upstream:
			response_headers = _copy_headers(upstream.headers)
			content = await upstream.read()
			content_type = upstream.headers.get("content-type", "")
			if runtime and "text/html" in content_type:
				text = content.decode(upstream.charset or "utf-8", errors="replace")
				content = _inject_runtime_prefix(text, f"/{runtime}-runtime").encode("utf-8")
				response_headers["content-type"] = "text/html; charset=utf-8"
				response_headers.pop("content-length", None)

			return web.Response(status=upstream.status, headers=response_headers, body=content)
	except Exception as exc:
		return web.Response(
			status=502,
			text=f"Runtime proxy could not reach {target_url}: {exc}",
			content_type="text/plain",
		)


async def _client_ctx(app: web.Application) -> AsyncIterator[None]:
	app["client"] = ClientSession()
	try:
		yield
	finally:
		await app["client"].close()


def build_app(args) -> web.Application:
	app = web.Application(client_max_size=50 * 1024 * 1024)
	app["frappe_target"] = args.frappe_target
	app["hedge_target"] = args.hedge_target
	app["straddle_target"] = args.straddle_target
	app.cleanup_ctx.append(_client_ctx)
	app.router.add_route("*", "/{tail:.*}", proxy)
	return app


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Unified local proxy for Frappe and panel runtimes")
	parser.add_argument("--host", default=os.environ.get("UNIFIED_PROXY_HOST", "127.0.0.1"))
	parser.add_argument("--port", type=int, default=int(os.environ.get("UNIFIED_PROXY_PORT", "9100")))
	parser.add_argument("--frappe-target", default=os.environ.get("FRAPPE_TARGET", "http://127.0.0.1:8000"))
	parser.add_argument("--hedge-target", default=os.environ.get("HEDGE_TARGET", "http://127.0.0.1:8100"))
	parser.add_argument("--straddle-target", default=os.environ.get("STRADDLE_TARGET", "http://127.0.0.1:8080"))
	return parser


def main() -> None:
	args = build_parser().parse_args()
	web.run_app(build_app(args), host=args.host, port=args.port)


if __name__ == "__main__":
	main()
