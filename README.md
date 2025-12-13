### `patch_resi_chain_sanitized.py` 使用说明（社区版）

这个脚本用于给 **Clash.Meta / mihomo** 风格的订阅 YAML 做“幂等补丁”，自动加入/修正一套：
- **住宅 SOCKS5 出口**（最终对外显示住宅 IP）
- **美国中继自动择优**（`url-test`）
- **一键入口**（只在你选择它时才走住宅；选择其它节点默认不走住宅）

> 说明：脚本是**脱敏版**，敏感信息通过环境变量注入；适合社区分享。

---

### 适用范围（先确认能不能用）

- **适用**：使用 **mihomo / Clash.Meta** 内核的客户端，并且内核支持 `dialer-proxy`
- **不一定适用**：经典 Clash 老内核（可能忽略 `dialer-proxy` 或报未知字段）

脚本默认针对“订阅合并后的 YAML”结构，要求文件中存在：
- `proxies:`
- `proxy-groups:`
- `rules:`

并且节点名一般使用 `name: 'xxx'` 或 `name: "xxx"` 这种格式。

---

### 脚本会做什么（幂等 / 最小改动）

每次运行都会检查并确保（若已存在且一致则不改）：
- 顶部存在 `port: 7891`（避免某些客户端运行时 `port:7890` 与 `mixed-port:7890` 冲突）
- `proxies:` 里存在住宅节点 `🚀 前置-SOCKS5`（socks5 + 用户名密码）
  - 且包含 `dialer-proxy: 🇺🇲 美国-中继择优`（让住宅出口通过美国中继拨号）
- `proxy-groups:` 里存在：
  - `🇺🇲 美国-中继择优`：`url-test`，候选为“匹配到的美国节点”
  - `🏠 住宅+美国择优`：`select`，候选为 `🚀 前置-SOCKS5` / `DIRECT`
- `🚀 节点选择` 里包含 `🏠 住宅+美国择优`（方便一键选择）

额外特性：
- **默认会生成备份**：`*.bak.YYYYmmdd-HHMMSS`
- **幂等**：如果无需变更，输出 `No changes needed (already patched).`

---

### 用法（推荐环境变量注入）

#### 1) 设置住宅 SOCKS5 参数（必需）

```bash
export RESI_SERVER="YOUR_RESI_IP"
export RESI_PORT="443"
export RESI_USERNAME="YOUR_USERNAME"
export RESI_PASSWORD="YOUR_PASSWORD"
```

#### 2) 执行补丁（写入 + 备份）

```bash
python3 "patch_resi_chain_sanitized.py" "/绝对路径/你的订阅.yaml"
```

#### 3) 干跑（只看结果不写入）

```bash
python3 "patch_resi_chain_sanitized.py" "/绝对路径/你的订阅.yaml" --dry-run
```

#### 4) 不写备份

```bash
python3 "patch_resi_chain_sanitized.py" "/绝对路径/你的订阅.yaml" --no-backup
```

---

### 美国节点通配符匹配（读者最容易踩坑的点）

脚本不会要求你写死 `["🇺🇲 美国 01", "🇺🇲 美国 02", "🇺🇲 美国 03"]`，而是会：
1) 从 `proxies:` 扫描所有节点的 `name`
2) 用 `US_NODE_PATTERNS` 进行匹配
3) 匹配到的节点作为 `🇺🇲 美国-中继择优` 的候选列表（保持原始顺序）

配置项：
- **`US_NODE_PATTERNS`**：模式列表
  - glob：`* ? [abc]`
  - 正则：以 `re:` 开头，例如 `re:^🇺🇲\\s*美国\\s*\\d+$`
- **`US_NODES_FALLBACK`**：一个都匹配不到时的兜底列表（避免生成空组）

如果你订阅里的美国节点命名不是 “🇺🇲 美国 01/02/03” 这种形式，请务必调整 `US_NODE_PATTERNS`，例如：
- `"*美国*"`（宽松匹配）
- `"US-*"`（匹配 `US-NewYork-01`）
- `re:.*\\bUS\\b.*`（正则示例）

---

### 回滚方式（出问题如何恢复）

脚本默认会生成备份文件，例如：
- `subscription_xxx.yaml.bak.20251214-010203`

如果补丁后不满意，直接用备份文件覆盖原文件即可恢复。


