#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""List tools exposed by the official JustRunMy remote MCP server.

The script only performs MCP initialize + tools/list. It never calls a tool and
never prints authentication header values.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

DEFAULT_MCP_URL = "https://justrunmy.app/api/mcp"
CONFIG_ENV = "JUSTRUNMY_MCP_CONFIG"
REPORT_PATH = Path(os.getenv("MCP_REPORT_PATH", "mcp-diagnostics/mcp_tools.json"))
PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")
RENEWAL_PATTERN = re.compile(r"reset|renew|timer|extend", re.IGNORECASE)


class McpError(RuntimeError):
    pass


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_remote_config(raw: str):
    raw = (raw or "").strip()
    if not raw:
        raise McpError(
            f"GitHub Secret {CONFIG_ENV} 为空。请把 JustRunMy 控制面板中 "
            "MCP Server Config -> Remote MCP Server Config 的完整内容保存到该 Secret。"
        )

    if raw.startswith(("https://", "http://")):
        return raw, {}

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise McpError(f"{CONFIG_ENV} 不是有效 JSON: {exc}") from exc

    candidates = []
    if isinstance(config, dict):
        for key in ("mcpServers", "servers"):
            servers = config.get(key)
            if isinstance(servers, dict):
                candidates.extend(value for value in servers.values() if isinstance(value, dict))
        candidates.append(config)

    for candidate in candidates:
        url = (
            candidate.get("url")
            or candidate.get("serverUrl")
            or candidate.get("server_url")
            or candidate.get("endpoint")
        )
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue

        headers = candidate.get("headers") or {}
        if not isinstance(headers, dict):
            raise McpError("Remote MCP 配置中的 headers 必须是 JSON 对象。")
        headers = {
            str(name): str(value)
            for name, value in headers.items()
            if value is not None and str(value).strip()
        }
        return url, headers

    raise McpError(
        "没有在 Remote MCP 配置中找到 url。支持顶层 url，或 "
        "mcpServers.<名称>.url / servers.<名称>.url 格式。"
    )


def decode_rpc_response(response: requests.Response, expected_id=None):
    if not response.content:
        return None

    content_type = response.headers.get("content-type", "").lower()
    objects = []

    if "text/event-stream" in content_type or response.text.lstrip().startswith(("event:", "data:")):
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                objects.append(json.loads(data))
            except json.JSONDecodeError:
                continue
    else:
        try:
            payload = response.json()
            objects.extend(payload if isinstance(payload, list) else [payload])
        except ValueError as exc:
            preview = response.text[:500].replace("\n", " ")
            raise McpError(f"MCP 返回了无法解析的内容: {preview}") from exc

    if expected_id is not None:
        for item in objects:
            if isinstance(item, dict) and item.get("id") == expected_id:
                return item
    return objects[-1] if objects else None


def rpc_post(http, url, auth_headers, payload, session_id=None, allow_empty=False):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": "myjustrunmy-mcp-probe/1.0",
        **auth_headers,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    try:
        response = http.post(url, headers=headers, json=payload, timeout=45)
    except requests.RequestException as exc:
        raise McpError(f"连接官方 MCP 接口失败: {exc}") from exc

    if response.status_code == 401:
        raise McpError(
            "官方 MCP 返回 401 Unauthorized。请重新复制 Remote MCP Server Config，"
            "并确认其中的认证 headers 已完整保存到 GitHub Secret。"
        )
    if response.status_code >= 400:
        preview = response.text[:800].replace("\n", " ")
        raise McpError(f"官方 MCP 返回 HTTP {response.status_code}: {preview}")

    new_session_id = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
    if allow_empty and not response.content:
        return None, new_session_id
    return decode_rpc_response(response, payload.get("id")), new_session_id


def initialize(http, url, headers):
    last_error = None
    for protocol_version in PROTOCOL_VERSIONS:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": "myjustrunmy-mcp-probe",
                    "version": "1.0",
                },
            },
        }
        try:
            message, session_id = rpc_post(http, url, headers, payload)
        except McpError as exc:
            last_error = exc
            continue

        if isinstance(message, dict) and message.get("error"):
            last_error = McpError(f"MCP initialize 失败: {message['error']}")
            continue
        if not isinstance(message, dict) or not isinstance(message.get("result"), dict):
            last_error = McpError(f"MCP initialize 返回格式异常: {message!r}")
            continue

        negotiated = message["result"].get("protocolVersion") or protocol_version
        notice = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        rpc_post(http, url, headers, notice, session_id=session_id, allow_empty=True)
        return negotiated, session_id, message["result"].get("serverInfo") or {}

    raise last_error or McpError("无法初始化官方 MCP 会话。")


def list_tools(http, url, headers, session_id):
    tools = []
    cursor = None
    request_id = 10

    while True:
        params = {"cursor": cursor} if cursor else {}
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/list",
            "params": params,
        }
        message, returned_session = rpc_post(
            http, url, headers, payload, session_id=session_id
        )
        session_id = returned_session or session_id

        if isinstance(message, dict) and message.get("error"):
            raise McpError(f"tools/list 失败: {message['error']}")
        result = message.get("result") if isinstance(message, dict) else None
        if not isinstance(result, dict):
            raise McpError(f"tools/list 返回格式异常: {message!r}")

        page_tools = result.get("tools") or []
        if not isinstance(page_tools, list):
            raise McpError("tools/list 的 tools 字段不是数组。")
        tools.extend(tool for tool in page_tools if isinstance(tool, dict))

        cursor = result.get("nextCursor")
        if not cursor:
            return tools
        request_id += 1


def main():
    try:
        url, auth_headers = parse_remote_config(os.getenv(CONFIG_ENV, ""))
        print(f"官方 MCP 地址: {redact_url(url)}")
        print(f"已读取认证 Header 名称: {', '.join(sorted(auth_headers)) or '(无)'}")
        print("认证 Header 的值不会写入日志或报告。")

        with requests.Session() as http:
            protocol, session_id, server_info = initialize(http, url, auth_headers)
            tools = list_tools(http, url, auth_headers, session_id)

        public_tools = []
        candidates = []
        for tool in tools:
            item = {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("inputSchema") or {},
            }
            public_tools.append(item)
            searchable = f"{item['name']} {item['description']}"
            if RENEWAL_PATTERN.search(searchable):
                candidates.append(item)

        report = {
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
            "endpoint": redact_url(url),
            "protocolVersion": protocol,
            "serverInfo": server_info,
            "toolCount": len(public_tools),
            "renewalCandidates": candidates,
            "tools": public_tools,
        }
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"\n共发现 {len(public_tools)} 个官方 MCP 工具：")
        for item in public_tools:
            print(f"  - {item['name']}")

        if candidates:
            print("\n发现名称或说明包含 reset/renew/timer/extend 的候选工具：")
            for item in candidates:
                print(f"  * {item['name']}: {item['description'][:200]}")
        else:
            print("\n未发现公开的 reset/renew/timer/extend 工具。")

        print(f"完整且已去除认证信息的报告已保存: {REPORT_PATH}")
        return 0
    except McpError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: 未预期异常: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
