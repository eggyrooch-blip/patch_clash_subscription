#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 用法（中文 / 适合分享）：
# 1) 推荐用环境变量传入账号信息：
#    export RESI_SERVER="YOUR_RESI_IP"
#    export RESI_PORT="443"
#    export RESI_USERNAME="YOUR_USERNAME"
#    export RESI_PASSWORD="YOUR_PASSWORD"
#    python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml"
# 2) 干跑（只看结果不写入）：
#    python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --dry-run
# 3) 不写备份：
#    python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --no-backup
#
# 说明：
# - 幂等：如果住宅链路配置已经存在且一致，不会重复插入。
# - 你也可以直接修改本文件 CONFIG 区域（RESI_PROXY）来写死参数。
# - ✅ 当前脚本已切换为“极简两入口”方案：只维护 `节点择优` + `代理节点`，以及精简后的 `🚀 节点选择`。
# - （兼容性）仍会清理旧版本脚本生成的：前置拨号/港美落地/中继择优/地区择优等冗余项，避免越补越乱。
"""
Patch a Clash subscription YAML in-place (idempotent).

This script enforces the "极简两入口" layout:
  - 大分组：`节点择优`（url-test，候选=全部普通节点）
  - 代理入口：`代理节点`（select：住宅 SOCKS5 / DIRECT）
  - `🚀 节点选择`：只保留 `代理节点` / `节点择优` / DIRECT

Credentials are injected via env vars (recommended):
  RESI_SERVER / RESI_PORT / RESI_USERNAME / RESI_PASSWORD
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import ipaddress
from pathlib import Path
from typing import Tuple, Optional, List
import fnmatch
import re
from collections import OrderedDict


# ====== CONFIG (sanitized) ======
RESI_PROXY = {
    "name": "🚀 前置-SOCKS5",
    "type": "socks5",
    "server": os.getenv("RESI_SERVER", "YOUR_RESI_IP"),
    "port": int(os.getenv("RESI_PORT", "443")),
    "username": os.getenv("RESI_USERNAME", "YOUR_USERNAME"),
    "password": os.getenv("RESI_PASSWORD", "YOUR_PASSWORD"),
}

# --- resi dialer selection (which upstream nodes are used to dial the residential proxy) ---
# Default: use ALL normal nodes as dialer candidates (same as old behavior).
RESI_DIALER_MODE_ENV = "RESI_DIALER_MODE"  # all | regex
RESI_DIALER_REGEX_ENV = "RESI_DIALER_REGEX"  # only used when mode=regex
RESI_PROXY_NAME_ENV = "RESI_PROXY_NAME"
RESI_GROUP_DIALER_NAME_ENV = "RESI_GROUP_DIALER_NAME"
RESI_GROUP_ONECLICK_NAME_ENV = "RESI_GROUP_ONECLICK_NAME"
RESI_GROUP_NODE_SELECT_NAME_ENV = "RESI_GROUP_NODE_SELECT_NAME"
RESI_SKIP_NODE_SELECT_REWRITE_ENV = "RESI_SKIP_NODE_SELECT_REWRITE"  # 1/true to skip rewriting node-select group
# (legacy knobs from earlier iterations; kept for backward compatibility but no longer used)
RESI_GROUP_DIALER_SELECTOR_NAME_ENV = "RESI_GROUP_DIALER_SELECTOR_NAME"
RESI_NODE_SELECT_MODE_ENV = "RESI_NODE_SELECT_MODE"

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

GROUP_US_RELAY = "🇺🇲 美国-中继择优"  # legacy, no longer generated
GROUP_ONECLICK = "🏠 住宅出口"  # legacy, no longer generated
GROUP_NODE_SELECT = "🚀 节点选择"

# Feature toggles (open-source friendly)
FEATURE_RESI = "resi"
FEATURE_BYPASS = "bypass"

# ====== 可选：🇭🇰 香港 -> 美国住宅落地（香港做中继，住宅做出口） ======
# 开关：是否生成“香港中继落地”的一套分组/出口
ENABLE_HK_RELAY_LANDING = False

# 香港节点匹配（从 `proxies:` 里的所有节点 name 中匹配）
HK_NODE_PATTERNS = [
    "🇭🇰 香港 *",
    "HK *",
    "Hong Kong *",
    "🇭🇰 *",
]
HK_NODES_FALLBACK = ["🇭🇰 香港 01", "🇭🇰 香港 02", "🇭🇰 香港 03"]

# 香港中继组名（url-test）
GROUP_HK_RELAY = "🇭🇰 香港-中继择优"

# “经香港中继的住宅出口”节点名（和 RESI_PROXY 复用同一套账号/地址，只是 dialer-proxy 不同）
RESI_PROXY_VIA_HK_NAME = "🚀 前置-SOCKS5(经香港)"

# 一键入口：住宅（经香港中继）/DIRECT
GROUP_ONECLICK_HK = "🏠 住宅+香港落地"  # legacy, no longer generated

# ====== 新增：一个“大分组” + 明确的“前置拨号选择” ======
# “把所有节点都放在一个大分组，默认择优”
ENABLE_ALL_NODES_BEST = True
GROUP_ALL_NODES_BEST = "🧪 前置出口-择优"

# “表明代理的前置节点选择（拨号层/中继层）”
ENABLE_DIALER_SELECTOR = False  # legacy, no longer generated
GROUP_DIALER_SELECTOR = "🛰️ 前置出口(住宅拨号)"  # legacy, no longer generated

DIALER_SELECTOR_CANDIDATES = [
    GROUP_US_RELAY,
    GROUP_HK_RELAY,
    GROUP_ALL_NODES_BEST,
    "DIRECT",
]

TOPLEVEL_PORT = 7891
HEALTHCHECK_URL = "http://www.gstatic.com/generate_204"
HEALTHCHECK_INTERVAL = 300
HEALTHCHECK_TOLERANCE = 50

# ====== TUN/DNS bypass (TUN 模式下“全局也直连”) ======
# - 目标：让这些流量永远不进入代理链路（哪怕客户端看起来是“全局”）
# - 关键：TUN 路由绕过 + DNS 策略（fake-ip-filter / nameserver-policy）
# RFC1918 内网网段（局域网永远直连/不进代理链路）
# - 可通过环境变量 BYPASS_IP_CIDRS 覆盖（逗号分隔）
#   例：export BYPASS_IP_CIDRS="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
BYPASS_IP_CIDRS_ENV = "BYPASS_IP_CIDRS"
BYPASS_IP_CIDRS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

# 域名白名单（开源默认用 baidu.com 作为示例；实际使用请用环境变量覆盖）
# - 可通过环境变量 BYPASS_DOMAINS 覆盖（逗号分隔）
#   例：export BYPASS_DOMAINS="example.com,example.org"
BYPASS_DOMAINS_ENV = "BYPASS_DOMAINS"
BYPASS_DOMAINS = ["baidu.com"]

# fake-ip-filter 推荐写成 "+.domain"（匹配子域）
BYPASS_FAKEIP_FILTER: List[str] = []

# 可选：指定“白名单域名”用哪个 DNS 解析（逗号分隔），也支持 system/auto
# 例：
#   export BYPASS_INTERNAL_DNS="10.0.0.2,10.0.0.3"
#   export BYPASS_INTERNAL_DNS="system"
BYPASS_INTERNAL_DNS_ENV = "BYPASS_INTERNAL_DNS"

# 自动生成“地区-择优”分组（url-test）
# 默认关掉：有“全部节点-择优”后，再生成大量地区组会让列表太长、容易误会
AUTO_REGION_URLTEST_GROUPS = False
REGION_GROUP_SUFFIX = "-择优"
REGION_GROUP_MIN_SIZE = 2  # 至少多少节点才生成该地区组（避免单节点刷屏）
# 是否把自动生成的地区组也插入到 `🚀 节点选择` 里（不建议：会让列表爆炸）
INJECT_REGION_GROUPS_INTO_NODE_SELECT = False

#
# 注意：本脚本不做“清理旧分组/旧代理”的迁移动作，只做幂等确保（新增/更新必要项）。


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


def _parse_csv_env_list(var_name: str) -> List[str]:
    raw = os.getenv(var_name, "").strip()
    if not raw:
        return []
    # Special value: "system" / "auto" -> detect current macOS system DNS
    if raw.lower() in {"system", "auto"}:
        return _get_macos_system_dns()
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _apply_env_overrides() -> None:
    """
    Apply env overrides to bypass config (sanitized/open-source friendly).
    """
    global BYPASS_IP_CIDRS, BYPASS_DOMAINS, BYPASS_FAKEIP_FILTER
    global GROUP_ALL_NODES_BEST, GROUP_ONECLICK, GROUP_NODE_SELECT, GROUP_DIALER_SELECTOR
    global RESI_PROXY

    ip_raw = os.getenv(BYPASS_IP_CIDRS_ENV, "").strip()
    if ip_raw:
        BYPASS_IP_CIDRS = [p.strip() for p in ip_raw.split(",") if p.strip()]

    dom_raw = os.getenv(BYPASS_DOMAINS_ENV, "").strip()
    if dom_raw:
        BYPASS_DOMAINS = [p.strip().lstrip(".") for p in dom_raw.split(",") if p.strip()]

    BYPASS_FAKEIP_FILTER = [f"+.{d}" for d in BYPASS_DOMAINS]

    # resi credentials/endpoint overrides
    # NOTE: RESI_PROXY was initialized at import-time; .env is loaded later in main(),
    # so we must refresh these fields here to actually pick up values from .env.
    resi_server = os.getenv("RESI_SERVER", "").strip()
    if resi_server:
        RESI_PROXY["server"] = resi_server
    resi_port = os.getenv("RESI_PORT", "").strip()
    if resi_port:
        try:
            RESI_PROXY["port"] = int(resi_port)
        except Exception:
            # keep existing value if invalid
            pass
    resi_user = os.getenv("RESI_USERNAME", "").strip()
    if resi_user:
        RESI_PROXY["username"] = resi_user
    resi_pass = os.getenv("RESI_PASSWORD", "").strip()
    if resi_pass:
        RESI_PROXY["password"] = resi_pass

    # resi naming overrides (no secrets, safe for open-source)
    resi_proxy_name = os.getenv(RESI_PROXY_NAME_ENV, "").strip()
    if resi_proxy_name:
        RESI_PROXY["name"] = resi_proxy_name

    dialer_group_name = os.getenv(RESI_GROUP_DIALER_NAME_ENV, "").strip()
    if dialer_group_name:
        GROUP_ALL_NODES_BEST = dialer_group_name

    oneclick_group_name = os.getenv(RESI_GROUP_ONECLICK_NAME_ENV, "").strip()
    if oneclick_group_name:
        GROUP_ONECLICK = oneclick_group_name

    node_select_group_name = os.getenv(RESI_GROUP_NODE_SELECT_NAME_ENV, "").strip()
    if node_select_group_name:
        # Don't blindly assume it exists; we'll verify against YAML later and fallback safely.
        GROUP_NODE_SELECT = node_select_group_name

    dialer_selector_name = os.getenv(RESI_GROUP_DIALER_SELECTOR_NAME_ENV, "").strip()
    if dialer_selector_name:
        GROUP_DIALER_SELECTOR = dialer_selector_name


def _select_dialer_candidates(all_proxy_names_in_order: List[str]) -> Tuple[List[str], Optional[str]]:
    """
    Decide which upstream nodes are used as dialer candidates for the residential proxy.
    Returns (candidates, warning_message).
    """
    # Exclude residential proxy itself and special variants.
    candidates = [n for n in all_proxy_names_in_order if n not in {RESI_PROXY["name"], RESI_PROXY_VIA_HK_NAME}]
    if not candidates:
        return [], "resi: no dialer candidates found in proxies section"

    mode = os.getenv(RESI_DIALER_MODE_ENV, "all").strip().lower() or "all"
    if mode == "all":
        return candidates, None

    if mode == "regex":
        pattern = os.getenv(RESI_DIALER_REGEX_ENV, "").strip()
        if not pattern:
            return candidates, f"resi: {RESI_DIALER_MODE_ENV}=regex but {RESI_DIALER_REGEX_ENV} is empty; falling back to all"
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return candidates, f"resi: invalid regex in {RESI_DIALER_REGEX_ENV}: {e}; falling back to all"
        filtered = [n for n in candidates if rx.search(n)]
        if not filtered:
            return candidates, f"resi: regex matched 0 nodes; falling back to all (pattern={pattern!r})"
        return filtered, None

    return candidates, f"resi: unknown {RESI_DIALER_MODE_ENV}={mode!r}; falling back to all"


def _detect_compat_from_text(text: str) -> str:
    """
    Best-effort detection (since we patch YAML offline).
    Returns: "mihomo" or "classic".
    """
    mihomo_hints = [
        "dialer-proxy:",
        "geodata-mode:",
        "sniffer:",
        "external-controller:",
    ]
    return "mihomo" if any(h in text for h in mihomo_hints) else "classic"


def _resolve_compat(compat_arg: str, yaml_text: str) -> str:
    compat = (compat_arg or "auto").strip().lower()
    if compat == "auto":
        return _detect_compat_from_text(yaml_text)
    if compat in {"mihomo", "classic"}:
        return compat
    raise ValueError("Invalid --compat. Use: auto | mihomo | classic")


def _load_env_file(env_path: str, *, override: bool = True) -> List[str]:
    """
    Minimal .env loader (no external deps).
    Supports: KEY=VALUE, optional quotes, ignores blank lines and # comments.
    Returns a list of keys that were loaded.
    """
    p = Path(os.path.expanduser(env_path))
    if not p.exists() or not p.is_file():
        return []

    loaded: List[str] = []
    for raw_ln in p.read_text(encoding="utf-8").splitlines():
        ln = raw_ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("export "):
            ln = ln[len("export ") :].strip()
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # strip quotes
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        # By default, prefer .env for determinism (it is the explicit local config source).
        if override or os.getenv(k) is None:
            os.environ[k] = v
            loaded.append(k)
    return loaded


def _get_macos_system_dns() -> List[str]:
    """
    Best-effort: read current macOS DNS servers via `scutil --dns`.
    Returns a de-duplicated list, preferring private (RFC1918) nameservers first.
    """
    try:
        out = subprocess.check_output(["scutil", "--dns"], text=True, stderr=subprocess.STDOUT)
    except Exception:
        return []

    servers: List[str] = []
    for ln in out.splitlines():
        s = ln.strip()
        # e.g. "nameserver[0] : 10.0.0.2"
        if not s.startswith("nameserver["):
            continue
        if ":" not in s:
            continue
        ip = s.split(":", 1)[1].strip()
        if not ip:
            continue
        try:
            ipaddress.ip_address(ip)
        except Exception:
            continue
        servers.append(ip)

    seen = set()
    servers = [x for x in servers if not (x in seen or seen.add(x))]

    def is_private_ip(x: str) -> bool:
        try:
            return ipaddress.ip_address(x).is_private
        except Exception:
            return False

    private = [x for x in servers if is_private_ip(x)]
    public = [x for x in servers if not is_private_ip(x)]
    return private + public


def _parse_features(features_str: str) -> "set[str]":
    raw = (features_str or "").strip()
    if not raw:
        return {FEATURE_RESI, FEATURE_BYPASS}
    parts = [p.strip().lower() for p in raw.split(",")]
    out = {p for p in parts if p}
    valid = {FEATURE_RESI, FEATURE_BYPASS}
    unknown = sorted(out - valid)
    if unknown:
        raise ValueError(f"Unknown feature(s): {unknown}. Valid: {sorted(valid)}")
    return out


def _format_features(features: "set[str]") -> str:
    order = [FEATURE_RESI, FEATURE_BYPASS]
    return ",".join([f for f in order if f in features]) or "(none)"


def _explain(features: "set[str]") -> str:
    lines: List[str] = []
    lines.append("This script patches a Clash.Meta/mihomo-style subscription YAML in-place (idempotent).")
    lines.append("")
    lines.append(f"Enabled features: {_format_features(features)}")
    lines.append("")
    if FEATURE_RESI in features:
        lines.append("- resi: inject/maintain a minimal 'residential chain' layout:")
        lines.append("  - proxy: 🚀 前置-SOCKS5 (dialer-proxy -> 节点择优)")
        lines.append("  - groups: 节点择优 / 代理节点 / 🚀 节点选择")
        lines.append("  - ensures top-level port: 7891 (compat fix for some clients)")
        lines.append("")
    if FEATURE_BYPASS in features:
        lines.append("- bypass: make RFC1918 + selected domains always DIRECT (System Proxy + TUN):")
        lines.append(f"  - tun.route-exclude-address: {', '.join(BYPASS_IP_CIDRS)}")
        lines.append(f"  - bypass domains: {', '.join(BYPASS_DOMAINS)}")
        lines.append(f"  - dns.fake-ip-filter: {', '.join(BYPASS_FAKEIP_FILTER)}")
        lines.append(f"  - optional dns.nameserver-policy via {BYPASS_INTERNAL_DNS_ENV}")
        lines.append("  - rules: insert DIRECT rules at top of rules: as a safety net")
        lines.append("")
    lines.append("Rollback: restore from the generated .bak.* file (unless you used --no-backup).")
    return "\n".join(lines)


def _find_top_level_block(lines: List[str], key: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Find a top-level YAML block:
      key:
        ...
    Returns (start_idx, end_idx_exclusive) over `lines`.
    """
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}:\n") or ln.rstrip("\n") == f"{key}:":
            start = i
            break
    if start is None:
        return None, None
    j = start + 1
    while j < len(lines):
        s = lines[j]
        if s.strip() == "" or s.startswith("#") or s.startswith(" "):
            j += 1
            continue
        break
    return start, j


def _ensure_tun_route_exclude(text: str, cidrs: List[str]) -> Tuple[str, bool]:
    if not cidrs:
        return text, False
    lines = _split_lines_keepends(text)

    tun_start, tun_end = _find_top_level_block(lines, "tun")
    changed = False

    if tun_start is None:
        # Insert a minimal tun block before `proxies:` if possible, otherwise at EOF.
        insert_at = None
        for i, ln in enumerate(lines):
            if ln.startswith("proxies:\n") or ln.rstrip("\n") == "proxies:":
                insert_at = i
                break
        if insert_at is None:
            insert_at = len(lines)
            if insert_at > 0 and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
        tun_block = []
        tun_block.append("tun:\n")
        tun_block.append("  enable: true\n")
        tun_block.append("  stack: system\n")
        tun_block.append("  auto-route: true\n")
        tun_block.append("  auto-detect-interface: true\n")
        tun_block.append("  dns-hijack:\n")
        tun_block.append("    - any:53\n")
        tun_block.append("  route-exclude-address:\n")
        for c in cidrs:
            tun_block.append(f"    - {c}\n")
        tun_block.append("\n")
        lines[insert_at:insert_at] = tun_block
        return "".join(lines), True

    # tun exists: ensure route-exclude-address contains cidrs
    block = lines[tun_start:tun_end]
    block_text = "".join(block)
    missing = [c for c in cidrs if c not in block_text]
    if not missing:
        return text, False

    # Find route-exclude-address line inside tun block
    rex_idx = None
    for i in range(tun_start + 1, tun_end):
        if lines[i].lstrip().startswith("route-exclude-address:"):
            rex_idx = i
            break

    if rex_idx is None:
        # append new key at end of tun block (before first non-indented/top-level)
        insert_at = tun_end
        lines[insert_at:insert_at] = ["  route-exclude-address:\n"] + [f"    - {c}\n" for c in missing]
        changed = True
        return "".join(lines), changed

    ln = lines[rex_idx]
    if "[" in ln and "]" in ln:
        # inline list: route-exclude-address: [a, b]
        open_i = ln.find("[")
        close_i = ln.rfind("]")
        inner = ln[open_i + 1 : close_i]
        new_items = []
        for c in missing:
            if c in inner:
                continue
            new_items.append(c)
        if new_items:
            inner2 = inner.rstrip()
            if inner2 and not inner2.strip().endswith(","):
                inner2 = inner2 + ", "
            inner2 = inner2 + ", ".join(new_items)
            lines[rex_idx] = ln[: open_i + 1] + inner2 + ln[close_i:]
            changed = True
        return "".join(lines), changed

    # multiline list: insert missing `-` items under the key
    j = rex_idx + 1
    while j < tun_end:
        if lines[j].startswith("  ") and not lines[j].startswith("    "):
            break
        j += 1
    insert_lines = []
    for c in missing:
        insert_lines.append(f"    - {c}\n")
    lines[j:j] = insert_lines
    changed = True
    return "".join(lines), changed


def _ensure_dns_fakeip_filter(text: str, patterns: List[str]) -> Tuple[str, bool]:
    if not patterns:
        return text, False
    lines = _split_lines_keepends(text)
    dns_start, dns_end = _find_top_level_block(lines, "dns")
    if dns_start is None:
        return text, False

    block_text = "".join(lines[dns_start:dns_end])
    missing = [p for p in patterns if p not in block_text]
    if not missing:
        return text, False

    # Locate fake-ip-filter within dns block
    fidx = None
    for i in range(dns_start + 1, dns_end):
        if lines[i].lstrip().startswith("fake-ip-filter:"):
            fidx = i
            break
    if fidx is None:
        # append a new fake-ip-filter list at end of dns block
        insert_at = dns_end
        new_lines = ["  fake-ip-filter:\n"] + [f"    - {p}\n" for p in missing]
        lines[insert_at:insert_at] = new_lines
        return "".join(lines), True

    ln = lines[fidx]
    # inline list?
    if "[" in ln and "]" in ln:
        open_i = ln.find("[")
        close_i = ln.rfind("]")
        inner = ln[open_i + 1 : close_i]
        inner2 = inner.rstrip()
        if inner2 and not inner2.strip().endswith(","):
            inner2 = inner2 + ", "
        inner2 = inner2 + ", ".join([p for p in missing if p not in inner])
        lines[fidx] = ln[: open_i + 1] + inner2 + ln[close_i:]
        return "".join(lines), True

    # multiline list: insert items after existing list items
    j = fidx + 1
    while j < dns_end:
        if lines[j].startswith("  ") and not lines[j].startswith("    "):
            break
        j += 1
    lines[j:j] = [f"    - {p}\n" for p in missing]
    return "".join(lines), True


def _yaml_inline_list(values: List[str]) -> str:
    # quote values to be safe (supports https:// / tcp:// etc)
    escaped = [v.replace('"', '\\"') for v in values]
    return "[" + ", ".join([f'"{v}"' for v in escaped]) + "]"


def _ensure_dns_nameserver_policy(
    text: str,
    domain_patterns: List[str],
    servers: List[str],
) -> Tuple[str, bool]:
    if not domain_patterns or not servers:
        return text, False
    lines = _split_lines_keepends(text)
    dns_start, dns_end = _find_top_level_block(lines, "dns")
    if dns_start is None:
        return text, False

    block_text = "".join(lines[dns_start:dns_end])
    # Find or create nameserver-policy block under dns
    np_idx = None
    for i in range(dns_start + 1, dns_end):
        if lines[i].lstrip().startswith("nameserver-policy:"):
            np_idx = i
            break

    changed = False
    entry_lines = []
    for dp in domain_patterns:
        if dp in block_text:
            continue
        entry_lines.append(f'    "{dp}": {_yaml_inline_list(servers)}\n')

    if not entry_lines:
        return text, False

    if np_idx is None:
        # append at end of dns block
        insert_at = dns_end
        new_lines = ["  nameserver-policy:\n"] + entry_lines
        lines[insert_at:insert_at] = new_lines
        changed = True
        return "".join(lines), changed

    # insert under existing nameserver-policy mapping
    j = np_idx + 1
    while j < dns_end:
        if lines[j].startswith("  ") and not lines[j].startswith("    "):
            break
        j += 1
    lines[j:j] = entry_lines
    changed = True
    return "".join(lines), changed


def _ensure_rules_bypass(text: str) -> Tuple[str, bool]:
    """
    Add rule-mode bypass rules near the top of `rules:` as a safety net.
    Note: In "global" style modes, these rules may be bypassed; TUN route-exclude is the key.
    """
    lines = _split_lines_keepends(text)
    rules_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("rules:\n") or ln.rstrip("\n") == "rules:":
            rules_idx = i
            break
    if rules_idx is None:
        return text, False

    wanted: List[str] = []
    for cidr in BYPASS_IP_CIDRS:
        wanted.append(f"- IP-CIDR,{cidr},DIRECT,no-resolve\n")
    wanted.extend(
        [
            *[f"- DOMAIN-SUFFIX,{d},DIRECT\n" for d in BYPASS_DOMAINS],
        ]
    )

    # check if already present
    rules_head = "".join(lines[rules_idx : min(len(lines), rules_idx + 200)])
    missing = [r for r in wanted if r.strip() not in rules_head]
    if not missing:
        return text, False

    insert_at = rules_idx + 1
    lines[insert_at:insert_at] = missing
    return "".join(lines), True

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


def _resolve_hk_nodes(all_proxy_names_in_order: List[str]) -> List[str]:
    matched = [n for n in all_proxy_names_in_order if _match_name_patterns(n, HK_NODE_PATTERNS)]
    # exclude both residential proxy names to avoid weird matching
    matched = [n for n in matched if n not in {RESI_PROXY["name"], RESI_PROXY_VIA_HK_NAME}]
    return matched if matched else HK_NODES_FALLBACK


_FLAG_RE = re.compile(r"^(?P<flag>[\U0001F1E6-\U0001F1FF]{2})\s*(?P<rest>.+)$")


def _parse_flag_label(name: str) -> Optional[Tuple[str, str]]:
    m = _FLAG_RE.match(name)
    if not m:
        return None
    flag = m.group("flag")
    rest = m.group("rest").strip()
    parts = rest.split()
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    label = " ".join(parts).strip()
    return (flag, label) if label else None


def _resolve_region_groups(all_proxy_names_in_order: List[str], us_nodes: List[str]) -> List[Tuple[str, List[str]]]:
    if not AUTO_REGION_URLTEST_GROUPS:
        return []
    us_set = set(us_nodes)
    groups: "OrderedDict[str, List[str]]" = OrderedDict()
    for n in all_proxy_names_in_order:
        if n == RESI_PROXY["name"] or n in us_set:
            continue
        parsed = _parse_flag_label(n)
        if not parsed:
            continue
        flag, label = parsed
        group_name = f"{flag} {label}{REGION_GROUP_SUFFIX}"
        groups.setdefault(group_name, []).append(n)
    return [(g, nodes) for g, nodes in groups.items() if len(nodes) >= REGION_GROUP_MIN_SIZE]


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


def _extract_group_names_from_proxy_groups_section(proxy_groups_section_text: str) -> List[str]:
    """
    Best-effort: list proxy-group names in order from `proxy-groups:` section.
    """
    names: List[str] = []
    for ln in _split_lines_keepends(proxy_groups_section_text):
        s = ln.strip()
        if not s.startswith("name: "):
            continue
        if s.startswith("name: '") and s.endswith("'"):
            names.append(s[len("name: '") : -1])
        elif s.startswith('name: "') and s.endswith('"'):
            names.append(s[len('name: "') : -1])
    return names


def _truthy_env(name: str) -> bool:
    v = os.getenv(name, "").strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def _resolve_node_select_group_name(proxy_groups_section_text: str) -> Optional[str]:
    """
    Decide which existing group to treat as "node select" (the UI-facing group to rewrite).
    If not found, return None and we will skip rewrite to avoid breaking non-standard YAML.
    """
    names = _extract_group_names_from_proxy_groups_section(proxy_groups_section_text)
    if not names:
        return None

    # Prefer env override if it actually exists in YAML
    env_name = os.getenv(RESI_GROUP_NODE_SELECT_NAME_ENV, "").strip()
    if env_name and env_name in names:
        return env_name

    # Prefer current default if it exists
    if GROUP_NODE_SELECT in names:
        return GROUP_NODE_SELECT

    # Common fallbacks seen in configs
    for cand in ["🚀 节点选择", "节点选择", "Proxy", "PROXY", "代理", "默认"]:
        if cand in names:
            return cand

    # If there's exactly one select group, use it as a last resort
    # (very heuristic, but better than crashing)
    return None


def _render_proxy_block() -> str:
    # 住宅出口拨号走“前置出口-择优”（默认 all candidates -> url-test）
    dialer_target = GROUP_ALL_NODES_BEST
    return (
        "  -\n"
        f"    name: '{RESI_PROXY['name']}'\n"
        f"    type: {RESI_PROXY['type']}\n"
        f"    server: {RESI_PROXY['server']}\n"
        f"    port: {RESI_PROXY['port']}\n"
        f"    username: '{RESI_PROXY['username']}'\n"
        f"    password: '{RESI_PROXY['password']}'\n"
        f"    dialer-proxy: '{dialer_target}'\n"
    )


def _render_proxy_block_via_hk() -> str:
    return (
        "  -\n"
        f"    name: '{RESI_PROXY_VIA_HK_NAME}'\n"
        f"    type: {RESI_PROXY['type']}\n"
        f"    server: {RESI_PROXY['server']}\n"
        f"    port: {RESI_PROXY['port']}\n"
        f"    username: '{RESI_PROXY['username']}'\n"
        f"    password: '{RESI_PROXY['password']}'\n"
        f"    dialer-proxy: '{GROUP_HK_RELAY}'\n"
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


def _render_region_urltest_group(group_name: str, nodes: List[str]) -> str:
    proxies_lines = "".join([f"      - '{n}'\n" for n in nodes])
    return (
        "  -\n"
        f"    name: '{group_name}'\n"
        "    type: url-test\n"
        f"    url: '{HEALTHCHECK_URL}'\n"
        f"    interval: {HEALTHCHECK_INTERVAL}\n"
        f"    tolerance: {HEALTHCHECK_TOLERANCE}\n"
        "    proxies:\n"
        f"{proxies_lines}"
    )


def _render_group_all_nodes_best_with_nodes(nodes: List[str]) -> str:
    proxies_lines = "".join([f"      - '{n}'\n" for n in nodes])
    return (
        "  -\n"
        f"    name: '{GROUP_ALL_NODES_BEST}'\n"
        "    type: url-test\n"
        f"    url: '{HEALTHCHECK_URL}'\n"
        f"    interval: {HEALTHCHECK_INTERVAL}\n"
        f"    tolerance: {HEALTHCHECK_TOLERANCE}\n"
        "    proxies:\n"
        f"{proxies_lines}"
    )


def _render_group_dialer_selector(candidates: List[str]) -> str:
    seen = set()
    final: List[str] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        final.append(c)
    lines: List[str] = []
    for c in final:
        if c == "DIRECT":
            lines.append("      - DIRECT\n")
        else:
            lines.append(f"      - '{c}'\n")
    return (
        "  -\n"
        f"    name: '{GROUP_DIALER_SELECTOR}'\n"
        "    type: select\n"
        "    proxies:\n"
        + "".join(lines)
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


def _render_group_oneclick_hk() -> str:
    return (
        "  -\n"
        f"    name: '{GROUP_ONECLICK_HK}'\n"
        "    type: select\n"
        "    proxies:\n"
        f"      - '{RESI_PROXY_VIA_HK_NAME}'\n"
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


def _ensure_many_in_section_list(section_text: str, items: List[Tuple[str, str]], dash_indent: str) -> Tuple[str, bool]:
    if not items:
        return section_text, False
    lines = _split_lines_keepends(section_text)
    changed = False
    missing_blocks: List[str] = []

    for name, block in items:
        bs, be = _find_list_item_block(lines, start_idx=1, dash_indent=dash_indent, name=name)
        if bs is None:
            missing_blocks.append(block)
            continue
        if "".join(lines[bs:be]) != block:
            lines[bs:be] = _split_lines_keepends(block)
            changed = True

    if missing_blocks:
        insert_at = 1
        merged = "".join(missing_blocks)
        lines[insert_at:insert_at] = _split_lines_keepends(merged)
        changed = True
    return "".join(lines), changed


def _set_group_proxies_exact(section_text: str, group_name: str, entries: List[str]) -> Tuple[str, bool]:
    """
    Rewrite `proxies:` list of a proxy-group to exactly `entries` (unique, preserve order).
    """
    seen = set()
    final: List[str] = []
    for e in entries:
        if e in seen:
            continue
        seen.add(e)
        final.append(e)

    lines = _split_lines_keepends(section_text)
    bs, be = _find_list_item_block(lines, start_idx=1, dash_indent="  ", name=group_name)
    if bs is None:
        raise ValueError(f"Missing proxy group {group_name!r} in proxy-groups section")

    block = lines[bs:be]
    proxies_idx = None
    for i in range(len(block)):
        if block[i].startswith("    proxies:"):
            proxies_idx = i
            break
    if proxies_idx is None:
        raise ValueError(f"Group {group_name!r} missing proxies list")

    k = proxies_idx + 1
    while k < len(block) and block[k].startswith("      -"):
        del block[k]

    rendered: List[str] = []
    for e in final:
        if e == "DIRECT":
            rendered.append("      - DIRECT\n")
        else:
            rendered.append(f"      - '{e}'\n")

    block[proxies_idx + 1 : proxies_idx + 1] = rendered
    new_block = "".join(block)
    old_block = "".join(lines[bs:be])
    if new_block == old_block:
        return section_text, False

    lines[bs:be] = _split_lines_keepends(new_block)
    return "".join(lines), True


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


def _remove_group_by_name(section_text: str, group_name: str) -> Tuple[str, bool]:
    """
    Remove a proxy-group block by name from `proxy-groups:` section text.
    Safe no-op if not present.
    """
    lines = _split_lines_keepends(section_text)
    bs, be = _find_list_item_block(lines, start_idx=1, dash_indent="  ", name=group_name)
    if bs is None:
        return section_text, False
    del lines[bs:be]
    return "".join(lines), True


def _remove_node_select_entry(section_text: str, group_name: str, entry_name: str) -> Tuple[str, bool]:
    """
    Remove an entry from a select group's proxies list (best-effort).
    """
    lines = _split_lines_keepends(section_text)
    bs, be = _find_list_item_block(lines, start_idx=1, dash_indent="  ", name=group_name)
    if bs is None:
        return section_text, False
    block = lines[bs:be]
    target1 = f"      - '{entry_name}'\n"
    target2 = f'      - "{entry_name}"\n'
    target3 = f"      - {entry_name}\n"
    changed = False
    k = 0
    while k < len(block):
        if block[k] in {target1, target2, target3}:
            del block[k]
            changed = True
            continue
        k += 1
    if not changed:
        return section_text, False
    lines[bs:be] = block
    return "".join(lines), True


def patch_file(path: Path, dry_run: bool, backup: bool, features: "set[str]", compat: str) -> Tuple[bool, str]:
    original = path.read_text(encoding="utf-8")
    text = original
    changed_any = False
    change_notes: List[str] = []
    warn_notes: List[str] = []

    if FEATURE_BYPASS in features:
        # bypass (idempotent)
        if compat == "mihomo":
            text, ch = _ensure_tun_route_exclude(text, BYPASS_IP_CIDRS)
            changed_any |= ch
            if ch:
                change_notes.append("bypass: updated tun.route-exclude-address")
        else:
            warn_notes.append("bypass: compat=classic -> skipping tun.route-exclude-address injection")
        text, ch = _ensure_dns_fakeip_filter(text, BYPASS_FAKEIP_FILTER)
        changed_any |= ch
        if ch:
            change_notes.append("bypass: updated dns.fake-ip-filter")
        if compat == "mihomo":
            dns_raw = os.getenv(BYPASS_INTERNAL_DNS_ENV, "").strip()
            dns_list = _parse_csv_env_list(BYPASS_INTERNAL_DNS_ENV)
            if dns_raw.lower() in {"system", "auto"} and not dns_list:
                warn_notes.append(
                    f"bypass: {BYPASS_INTERNAL_DNS_ENV}=system but no system DNS detected (scutil --dns returned none)"
                )
            text, ch = _ensure_dns_nameserver_policy(text, BYPASS_FAKEIP_FILTER, dns_list)
            changed_any |= ch
            if ch:
                change_notes.append("bypass: updated dns.nameserver-policy")
        else:
            if os.getenv(BYPASS_INTERNAL_DNS_ENV, "").strip():
                warn_notes.append("bypass: compat=classic -> ignoring BYPASS_INTERNAL_DNS (nameserver-policy may be unsupported)")
        text, ch = _ensure_rules_bypass(text)
        changed_any |= ch
        if ch:
            change_notes.append("bypass: inserted DIRECT rules into rules:")

    if FEATURE_RESI in features:
        if compat != "mihomo":
            raise ValueError("resi feature requires mihomo/Clash.Meta core (dialer-proxy); set --compat mihomo or disable resi.")
        if os.getenv(RESI_DIALER_MODE_ENV, "all").strip().lower() == "regex" and not os.getenv(RESI_DIALER_REGEX_ENV, "").strip():
            warn_notes.append(f"resi: {RESI_DIALER_MODE_ENV}=regex but {RESI_DIALER_REGEX_ENV} is empty; falling back to all")
        text, ch = _ensure_toplevel_port(text, TOPLEVEL_PORT)
        changed_any |= ch
        if ch:
            change_notes.append("resi: ensured top-level port")

        proxies_start, proxy_groups_start = _section_bounds(text, "proxies:\n", "proxy-groups:\n")
        proxies_section = text[proxies_start:proxy_groups_start]
        proxies_section, ch = _ensure_in_section_list(
            section_text=proxies_section,
            item_name=RESI_PROXY["name"],
            rendered_block=_render_proxy_block(),
            dash_indent="  ",
        )
        changed_any |= ch
        if ch:
            change_notes.append("resi: ensured residential proxy in proxies:")

        all_proxy_names = _extract_proxy_names_from_proxies_section(proxies_section)
        all_nodes_best, warn = _select_dialer_candidates(all_proxy_names)
        if warn:
            warn_notes.append(warn)

        proxy_groups_end = text.find("\nrules:\n", proxy_groups_start)
        if proxy_groups_end < 0:
            proxy_groups_end = text.find("rules:\n", proxy_groups_start)
        if proxy_groups_end < 0:
            raise ValueError("Missing rules: section; can't safely patch proxy-groups.")
        proxy_groups_section = text[proxy_groups_start:proxy_groups_end + 1]

        # resi group topology (minimal, per user request):
        # - only ONE extra group: GROUP_ALL_NODES_BEST (url-test) used as residential dialer-proxy
        items: List[Tuple[str, str]] = []
        if ENABLE_ALL_NODES_BEST:
            items.append((GROUP_ALL_NODES_BEST, _render_group_all_nodes_best_with_nodes(all_nodes_best)))
        proxy_groups_section, ch_groups = _ensure_many_in_section_list(proxy_groups_section, items, dash_indent="  ")
        changed_any |= ch_groups
        if ch_groups:
            change_notes.append("resi: ensured required proxy-groups")

        # Ensure node-select contains the residential SOCKS5 node (append-only, do not wipe existing nodes)
        if _truthy_env(RESI_SKIP_NODE_SELECT_REWRITE_ENV):
            warn_notes.append("resi: RESI_SKIP_NODE_SELECT_REWRITE enabled -> skipping node-select update")
        else:
            ns_name = _resolve_node_select_group_name(proxy_groups_section)
            if not ns_name:
                warn_notes.append("resi: could not detect node-select group name in YAML -> skipping node-select update to avoid breaking config")
            else:
                try:
                    proxy_groups_section, ch_ns = _ensure_node_select_contains(proxy_groups_section, ns_name, RESI_PROXY["name"])
                    changed_any |= ch_ns
                    if ch_ns:
                        change_notes.append(f"resi: appended residential node into {ns_name}")
                    # Cleanup legacy groups from older iterations (to reduce UI clutter)
                    proxy_groups_section, ch_rm1 = _remove_group_by_name(proxy_groups_section, GROUP_DIALER_SELECTOR)
                    proxy_groups_section, ch_rm2 = _remove_group_by_name(proxy_groups_section, GROUP_ONECLICK)
                    changed_any |= (ch_rm1 or ch_rm2)
                    if ch_rm1 or ch_rm2:
                        change_notes.append("resi: removed legacy resi groups (dialer selector / residential outlet)")
                    proxy_groups_section, _ = _remove_node_select_entry(proxy_groups_section, ns_name, GROUP_DIALER_SELECTOR)
                    proxy_groups_section, _ = _remove_node_select_entry(proxy_groups_section, ns_name, GROUP_ONECLICK)
                except Exception as e:
                    warn_notes.append(f"resi: failed to update node-select group safely: {e}")

        text = text[:proxies_start] + proxies_section + text[proxy_groups_start:]
        pg_start = text.find("proxy-groups:\n", proxies_start)
        pg_end = text.find("\nrules:\n", pg_start)
        if pg_end < 0:
            pg_end = text.find("rules:\n", pg_start)
        if pg_start < 0 or pg_end < 0:
            raise ValueError("Failed to re-locate proxy-groups/rules sections after patch.")
        text = text[:pg_start] + proxy_groups_section + text[pg_end + 1 :]

    if not changed_any:
        base = f"No changes needed. (features={_format_features(features)})"
        if warn_notes:
            base += "\nWarnings:\n- " + "\n- ".join(warn_notes)
        return False, base
    if dry_run:
        msg = f"Dry-run: changes would be applied. (features={_format_features(features)})"
        if change_notes:
            msg += "\nPlanned changes:\n- " + "\n- ".join(change_notes)
        if warn_notes:
            msg += "\nWarnings:\n- " + "\n- ".join(warn_notes)
        return True, msg

    if backup:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = path.with_suffix(path.suffix + f".bak.{ts}")
        bak.write_text(original, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    msg = f"Patched successfully. (features={_format_features(features)})"
    if change_notes:
        msg += "\nApplied changes:\n- " + "\n- ".join(change_notes)
    if warn_notes:
        msg += "\nWarnings:\n- " + "\n- ".join(warn_notes)
    return True, msg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("yaml_path", type=str, help="Path to the subscription YAML to patch")
    ap.add_argument(
        "--features",
        type=str,
        default=f"{FEATURE_RESI},{FEATURE_BYPASS}",
        help="Comma-separated feature list: resi,bypass (default: both)",
    )
    ap.add_argument("--no-resi-chain", action="store_true", help="Disable residential chain patching (resi)")
    ap.add_argument("--no-bypass", action="store_true", help="Disable TUN/DNS bypass patching (bypass)")
    ap.add_argument("--print-system-dns", action="store_true", help="Print detected macOS system DNS (scutil --dns) and exit")
    ap.add_argument("--explain", action="store_true", help="Explain what the script would do with selected features and exit")
    ap.add_argument("--changelog", action="store_true", help="Print a detailed change summary (no file write)")
    ap.add_argument("--diff", action="store_true", help="Print unified diff (no file write)")
    ap.add_argument(
        "--compat",
        type=str,
        default="auto",
        help="Compatibility mode: auto|mihomo|classic (auto tries to detect from YAML)",
    )
    ap.add_argument("--env-file", type=str, default=".env", help="Load env vars from file (default: .env).")
    ap.add_argument("--no-env-file", action="store_true", help="Do not load .env file automatically.")
    ap.add_argument("--dry-run", action="store_true", help="Do not write file; only report")
    ap.add_argument("--no-backup", action="store_true", help="Do not write .bak backup")
    args = ap.parse_args()

    if not args.no_env_file and args.env_file:
        _load_env_file(args.env_file)
    _apply_env_overrides()

    if args.print_system_dns:
        for ns in _get_macos_system_dns():
            print(ns)
        return 0

    p = Path(os.path.expanduser(args.yaml_path))
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    original = p.read_text(encoding="utf-8")
    compat = _resolve_compat(args.compat, original)
    features = _parse_features(args.features)
    if args.no_resi_chain:
        features.discard(FEATURE_RESI)
    if args.no_bypass:
        features.discard(FEATURE_BYPASS)
    if args.explain:
        print(_explain(features))
        print(f"\nCompatibility: {compat}")
        return 0

    if args.diff or args.changelog:
        changed, msg = patch_file(p, dry_run=True, backup=False, features=features, compat=compat)
        # patch_file(dry_run=True) doesn't write; but we also need the patched text for diff.
        # Re-run patch logic by calling patch_file on a temp path is too heavy; instead, directly compute by applying again:
        # We intentionally reuse the same patch functions to build the new text.
        new_text = original
        # bypass
        if FEATURE_BYPASS in features:
            if compat == "mihomo":
                new_text, _ = _ensure_tun_route_exclude(new_text, BYPASS_IP_CIDRS)
            new_text, _ = _ensure_dns_fakeip_filter(new_text, BYPASS_FAKEIP_FILTER)
            if compat == "mihomo":
                dns_list = _parse_csv_env_list(BYPASS_INTERNAL_DNS_ENV)
                new_text, _ = _ensure_dns_nameserver_policy(new_text, BYPASS_FAKEIP_FILTER, dns_list)
            new_text, _ = _ensure_rules_bypass(new_text)
        # resi
        if FEATURE_RESI in features:
            if compat != "mihomo":
                raise ValueError("resi feature requires mihomo/Clash.Meta core (dialer-proxy); set --compat mihomo or disable resi.")
            new_text, _ = _ensure_toplevel_port(new_text, TOPLEVEL_PORT)
            proxies_start, proxy_groups_start = _section_bounds(new_text, "proxies:\n", "proxy-groups:\n")
            proxies_section = new_text[proxies_start:proxy_groups_start]
            proxies_section, _ = _ensure_in_section_list(
                section_text=proxies_section,
                item_name=RESI_PROXY["name"],
                rendered_block=_render_proxy_block(),
                dash_indent="  ",
            )
            all_proxy_names = _extract_proxy_names_from_proxies_section(proxies_section)
            all_nodes_best, _ = _select_dialer_candidates(all_proxy_names)
            proxy_groups_end = new_text.find("\nrules:\n", proxy_groups_start)
            if proxy_groups_end < 0:
                proxy_groups_end = new_text.find("rules:\n", proxy_groups_start)
            if proxy_groups_end < 0:
                raise ValueError("Missing rules: section; can't safely patch proxy-groups.")
            proxy_groups_section = new_text[proxy_groups_start:proxy_groups_end + 1]
            items: List[Tuple[str, str]] = []
            if ENABLE_ALL_NODES_BEST:
                items.append((GROUP_ALL_NODES_BEST, _render_group_all_nodes_best_with_nodes(all_nodes_best)))
            proxy_groups_section, _ = _ensure_many_in_section_list(proxy_groups_section, items, dash_indent="  ")
            # node-select update: ensure residential node exists; best-effort cleanup legacy groups
            if not _truthy_env(RESI_SKIP_NODE_SELECT_REWRITE_ENV):
                ns_name = _resolve_node_select_group_name(proxy_groups_section)
                if ns_name:
                    proxy_groups_section, _ = _ensure_node_select_contains(proxy_groups_section, ns_name, RESI_PROXY["name"])
                    proxy_groups_section, _ = _remove_group_by_name(proxy_groups_section, GROUP_DIALER_SELECTOR)
                    proxy_groups_section, _ = _remove_group_by_name(proxy_groups_section, GROUP_ONECLICK)
                    proxy_groups_section, _ = _remove_node_select_entry(proxy_groups_section, ns_name, GROUP_DIALER_SELECTOR)
                    proxy_groups_section, _ = _remove_node_select_entry(proxy_groups_section, ns_name, GROUP_ONECLICK)
            new_text = new_text[:proxies_start] + proxies_section + new_text[proxy_groups_start:]
            pg_start = new_text.find("proxy-groups:\n", proxies_start)
            pg_end = new_text.find("\nrules:\n", pg_start)
            if pg_end < 0:
                pg_end = new_text.find("rules:\n", pg_start)
            if pg_start < 0 or pg_end < 0:
                raise ValueError("Failed to re-locate proxy-groups/rules sections after patch.")
            new_text = new_text[:pg_start] + proxy_groups_section + new_text[pg_end + 1 :]

        if args.changelog:
            print(msg)
            return 0

        if args.diff:
            import difflib

            a = original.splitlines(keepends=True)
            b = new_text.splitlines(keepends=True)
            diff = difflib.unified_diff(a, b, fromfile=str(p), tofile=str(p) + " (patched)")
            out = "".join(diff)
            print(out if out.strip() else "No diff (already patched).")
            return 0

    changed, msg = patch_file(p, dry_run=args.dry_run, backup=not args.no_backup, features=features, compat=compat)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


