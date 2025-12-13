#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 用法（中文 / 脱敏版，适合社区分享）：
# 1) 推荐用环境变量传入账号信息：
#    export RESI_SERVER="YOUR_RESI_IP"
#    export RESI_PORT="443"
#    export RESI_USERNAME="YOUR_USERNAME"
#    export RESI_PASSWORD="YOUR_PASSWORD"
#    python3 "patch_resi_chain_sanitized.py" "/绝对路径/你的订阅.yaml"
# 2) 干跑（只看结果不写入）：
#    python3 "patch_resi_chain_sanitized.py" "/绝对路径/你的订阅.yaml" --dry-run
# 3) 不写备份：
#    python3 "patch_resi_chain_sanitized.py" "/绝对路径/你的订阅.yaml" --no-backup
#
# 说明：
# - 幂等：如果住宅链路配置已经存在且一致，不会重复插入。
# - 你也可以直接修改本文件 CONFIG 区域（RESI_PROXY）来写死参数。
# - ⚠️ 美国节点筛选默认使用“通配符模式”从订阅里自动挑选；如果你的订阅命名不含“美国/US/United States/🇺🇸”，请改下面的 `US_NODE_PATTERNS`。
"""
Sanitized version of patch_resi_chain.py (safe to share).

Fill in credentials via env vars OR edit the CONFIG section.

Env vars (preferred):
  RESI_SERVER
  RESI_PORT
  RESI_USERNAME
  RESI_PASSWORD
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
from pathlib import Path
from typing import Tuple, Optional, List
import fnmatch
import re


# ====== CONFIG (sanitized) ======
RESI_PROXY = {
    "name": "🚀 前置-SOCKS5",
    "type": "socks5",
    "server": os.getenv("RESI_SERVER", "YOUR_RESI_IP"),
    "port": int(os.getenv("RESI_PORT", "443")),
    "username": os.getenv("RESI_USERNAME", "YOUR_USERNAME"),
    "password": os.getenv("RESI_PASSWORD", "YOUR_PASSWORD"),
}

# 用“通配符”自动筛选美国节点（从 `proxies:` 里的所有节点 name 中匹配）
# - 支持 glob：* ? [abc]
# - 也支持正则：以 "re:" 开头（例如 r"re:^🇺🇲\\s*美国\\s*\\d+$"）
# - 如果你的订阅命名风格不同，请在这里加/改模式
US_NODE_PATTERNS = [
    "🇺🇲 美国 *",
    "🇺🇸 *",
    "US *",
    "United States *",
]

# 如果模式没匹配到任何节点，就回退到这个列表（避免生成空组）
US_NODES_FALLBACK = ["🇺🇲 美国 01", "🇺🇲 美国 02", "🇺🇲 美国 03"]

GROUP_US_RELAY = "🇺🇲 美国-中继择优"
GROUP_ONECLICK = "🏠 住宅+美国择优"
GROUP_NODE_SELECT = "🚀 节点选择"

TOPLEVEL_PORT = 7891
HEALTHCHECK_URL = "http://www.gstatic.com/generate_204"
HEALTHCHECK_INTERVAL = 300
HEALTHCHECK_TOLERANCE = 50


def _split_lines_keepends(s: str) -> List[str]:
    return s.splitlines(keepends=True)


def _section_bounds(text: str, header: str, next_header: str) -> Tuple[int, int]:
    i = text.find(header)
    if i < 0:
        raise ValueError(f"Missing section header: {header!r}")
    j = text.find(next_header, i + len(header))
    if j < 0:
        raise ValueError(f"Missing section header: {next_header!r}")
    return i, j


def _extract_proxy_names_from_proxies_section(proxies_section_text: str) -> List[str]:
    names: List[str] = []
    for ln in _split_lines_keepends(proxies_section_text):
        s = ln.strip()
        if not s.startswith("name: "):
            continue
        if s.startswith("name: '") and s.endswith("'"):
            names.append(s[len("name: '") : -1])
        elif s.startswith('name: "') and s.endswith('"'):
            names.append(s[len('name: "') : -1])
    return names


def _match_name_patterns(name: str, patterns: List[str]) -> bool:
    for pat in patterns:
        if pat.startswith("re:"):
            if re.search(pat[3:], name):
                return True
        else:
            if fnmatch.fnmatchcase(name, pat):
                return True
    return False


def _resolve_us_nodes(all_proxy_names_in_order: List[str]) -> List[str]:
    matched = [n for n in all_proxy_names_in_order if _match_name_patterns(n, US_NODE_PATTERNS)]
    matched = [n for n in matched if n != RESI_PROXY["name"]]
    return matched if matched else US_NODES_FALLBACK


def _ensure_toplevel_port(text: str, desired_port: int) -> Tuple[str, bool]:
    lines = _split_lines_keepends(text)
    stop_idx = len(lines)
    for k, ln in enumerate(lines):
        if ln.startswith("dns:") or ln.startswith("proxies:"):
            stop_idx = k
            break

    changed = False
    port_line_idx: Optional[int] = None
    for k in range(stop_idx):
        if lines[k].startswith("port: "):
            port_line_idx = k
            break

    if port_line_idx is None:
        insert_after = None
        for k in range(stop_idx):
            if lines[k].startswith("mixed-port: "):
                insert_after = k
                break
        if insert_after is not None:
            lines.insert(insert_after + 1, f"port: {desired_port}\n")
            changed = True
        else:
            lines.insert(stop_idx, f"port: {desired_port}\n")
            changed = True
    else:
        desired = f"port: {desired_port}"
        if lines[port_line_idx].strip() != desired:
            lines[port_line_idx] = desired + "\n"
            changed = True

    return "".join(lines), changed


def _find_list_item_block(lines: List[str], start_idx: int, dash_indent: str, name: str) -> Tuple[Optional[int], Optional[int]]:
    i = start_idx
    dash_line = dash_indent + "-\n"
    dash_line_no_nl = dash_indent + "-"

    def is_item_start(ln: str) -> bool:
        return ln == dash_line or ln.rstrip("\n") == dash_line_no_nl

    while i < len(lines):
        if is_item_start(lines[i]):
            j = i + 1
            found = False
            while j < len(lines) and not is_item_start(lines[j]):
                if lines[j].lstrip().startswith("name: "):
                    if f"name: '{name}'" in lines[j] or f'name: "{name}"' in lines[j]:
                        found = True
                        break
                j += 1
            if found:
                k = i + 1
                while k < len(lines) and not is_item_start(lines[k]):
                    k += 1
                return i, k
            i = j if j > i else i + 1
        else:
            i += 1
    return None, None


def _render_proxy_block() -> str:
    return (
        "  -\n"
        f"    name: '{RESI_PROXY['name']}'\n"
        f"    type: {RESI_PROXY['type']}\n"
        f"    server: {RESI_PROXY['server']}\n"
        f"    port: {RESI_PROXY['port']}\n"
        f"    username: '{RESI_PROXY['username']}'\n"
        f"    password: '{RESI_PROXY['password']}'\n"
        f"    dialer-proxy: '{GROUP_US_RELAY}'\n"
    )


def _render_group_us_relay() -> str:
    raise RuntimeError("Use _render_group_us_relay_with_nodes(nodes) instead")


def _render_group_us_relay_with_nodes(us_nodes: List[str]) -> str:
    proxies_lines = "".join([f"      - '{n}'\n" for n in us_nodes])
    return (
        "  -\n"
        f"    name: '{GROUP_US_RELAY}'\n"
        "    type: url-test\n"
        f"    url: '{HEALTHCHECK_URL}'\n"
        f"    interval: {HEALTHCHECK_INTERVAL}\n"
        f"    tolerance: {HEALTHCHECK_TOLERANCE}\n"
        "    proxies:\n"
        f"{proxies_lines}"
    )


def _render_group_oneclick() -> str:
    return (
        "  -\n"
        f"    name: '{GROUP_ONECLICK}'\n"
        "    type: select\n"
        "    proxies:\n"
        f"      - '{RESI_PROXY['name']}'\n"
        "      - DIRECT\n"
    )


def _ensure_in_section_list(section_text: str, item_name: str, rendered_block: str, dash_indent: str) -> Tuple[str, bool]:
    lines = _split_lines_keepends(section_text)
    start_idx = 1
    bs, be = _find_list_item_block(lines, start_idx=start_idx, dash_indent=dash_indent, name=item_name)
    changed = False
    rendered_lines = _split_lines_keepends(rendered_block)
    if bs is None:
        lines[start_idx:start_idx] = rendered_lines
        changed = True
    else:
        if "".join(lines[bs:be]) != rendered_block:
            lines[bs:be] = rendered_lines
            changed = True
    return "".join(lines), changed


def _ensure_node_select_contains(section_text: str, group_name: str, entry_name: str) -> Tuple[str, bool]:
    lines = _split_lines_keepends(section_text)
    bs, be = _find_list_item_block(lines, start_idx=1, dash_indent="  ", name=group_name)
    if bs is None:
        raise ValueError(f"Missing proxy group {group_name!r} in proxy-groups section")
    block = lines[bs:be]
    block_text = "".join(block)
    if f"      - '{entry_name}'\n" in block_text or f'      - "{entry_name}"\n' in block_text:
        return section_text, False
    proxies_idx = None
    for i in range(len(block)):
        if block[i].startswith("    proxies:"):
            proxies_idx = i
            break
    if proxies_idx is None:
        raise ValueError(f"Group {group_name!r} missing proxies list")
    block.insert(proxies_idx + 1, f"      - '{entry_name}'\n")
    lines[bs:be] = block
    return "".join(lines), True


def patch_file(path: Path, dry_run: bool, backup: bool) -> Tuple[bool, str]:
    original = path.read_text(encoding="utf-8")
    text = original
    changed_any = False

    text, ch = _ensure_toplevel_port(text, TOPLEVEL_PORT)
    changed_any |= ch

    proxies_start, proxy_groups_start = _section_bounds(text, "proxies:\n", "proxy-groups:\n")
    proxies_section = text[proxies_start:proxy_groups_start]
    proxies_section, ch = _ensure_in_section_list(
        section_text=proxies_section,
        item_name=RESI_PROXY["name"],
        rendered_block=_render_proxy_block(),
        dash_indent="  ",
    )
    changed_any |= ch
    all_proxy_names = _extract_proxy_names_from_proxies_section(proxies_section)
    us_nodes = _resolve_us_nodes(all_proxy_names)

    proxy_groups_end = text.find("\nrules:\n", proxy_groups_start)
    if proxy_groups_end < 0:
        proxy_groups_end = text.find("rules:\n", proxy_groups_start)
    if proxy_groups_end < 0:
        raise ValueError("Missing rules: section; can't safely patch proxy-groups.")
    proxy_groups_section = text[proxy_groups_start:proxy_groups_end + 1]

    proxy_groups_section, ch1 = _ensure_in_section_list(
        section_text=proxy_groups_section,
        item_name=GROUP_US_RELAY,
        rendered_block=_render_group_us_relay_with_nodes(us_nodes),
        dash_indent="  ",
    )
    proxy_groups_section, ch2 = _ensure_in_section_list(
        section_text=proxy_groups_section,
        item_name=GROUP_ONECLICK,
        rendered_block=_render_group_oneclick(),
        dash_indent="  ",
    )
    changed_any |= (ch1 or ch2)

    proxy_groups_section, ch3 = _ensure_node_select_contains(proxy_groups_section, GROUP_NODE_SELECT, GROUP_ONECLICK)
    changed_any |= ch3

    text = text[:proxies_start] + proxies_section + text[proxy_groups_start:]
    pg_start = text.find("proxy-groups:\n", proxies_start)
    pg_end = text.find("\nrules:\n", pg_start)
    if pg_end < 0:
        pg_end = text.find("rules:\n", pg_start)
    if pg_start < 0 or pg_end < 0:
        raise ValueError("Failed to re-locate proxy-groups/rules sections after patch.")
    text = text[:pg_start] + proxy_groups_section + text[pg_end + 1 :]

    if not changed_any:
        return False, "No changes needed (already patched)."
    if dry_run:
        return True, "Dry-run: changes would be applied (not written)."

    if backup:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = path.with_suffix(path.suffix + f".bak.{ts}")
        bak.write_text(original, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return True, "Patched successfully."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("yaml_path", type=str, help="Path to the subscription YAML to patch")
    ap.add_argument("--dry-run", action="store_true", help="Do not write file; only report")
    ap.add_argument("--no-backup", action="store_true", help="Do not write .bak backup")
    args = ap.parse_args()

    p = Path(os.path.expanduser(args.yaml_path))
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    changed, msg = patch_file(p, dry_run=args.dry_run, backup=not args.no_backup)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


