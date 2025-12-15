## `patch_clash_subscription.py` 使用说明

这个脚本用于给“订阅 YAML”做**幂等补丁**：订阅更新覆盖你的改动后，你再跑一次脚本，把需要的结构/豁免规则补回去（已存在且一致就不改）。

### 运行逻辑（文本图示）

```
订阅.yaml
  |
  | 读取 .env（默认同目录；也支持 --env-file 指定；无需 source）
  v
解析配置（features / dialer / bypass / compat）
  |
  | dry-run / diff / changelog 只输出，不写文件
  | 正常模式：写回 + 生成 .bak 备份（可用 --no-backup 关闭）
  v
patched 订阅.yaml
```

### 你需要准备什么（推荐统一用 `.env`）

1) 复制一份模板：

```bash
cp .env.example .env
```

2) 修改 `.env`（本地私有配置文件，通常不提交到 Git；仓库已在 `.gitignore` 里忽略它）：
- `RESI_SERVER / RESI_PORT / RESI_USERNAME / RESI_PASSWORD`
- `BYPASS_DOMAINS / BYPASS_IP_CIDRS / BYPASS_INTERNAL_DNS`
- 其它可选项见 `.env.example` 注释

> 也支持直接用环境变量（export），但为了“入口统一、减少出错”，建议团队/自用都走 `.env`。

---

### 功能概览（features）

- **`resi`**：住宅链路（dialer-proxy + 极简入口）
- **`bypass`**：白名单豁免（route + DNS + rules 兜底）

### 三种常用模式（A/B/C）

- **A：全选（resi + bypass）**（默认）

```bash
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml"
```

- **B：只启用 resi**

```bash
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --features resi
```

- **C：只启用 bypass**

```bash
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --features bypass
```

你可以只启用其中一个：

```bash
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --features resi
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --features bypass
```

默认是 `resi,bypass` 都启用。

---

### `resi` 会做什么（按脚本逻辑）

- **确保顶层端口**：`port: 7891`（规避一些客户端运行时端口冲突）
- **确保住宅节点存在**（`proxies:`）：
  - 名称默认 `🚀 前置-SOCKS5`（可用 `RESI_PROXY_NAME` 改）
  - `dialer-proxy` 指向 **“🧪 前置出口-择优”**（默认把所有前置候选放一起择优）
- **确保分组存在**（`proxy-groups:`）：
  - `🧪 前置出口-择优`（url-test：候选=全部前置节点；默认 all）
- **节点选择入口（只追加，不覆盖）**
  - 默认会把 **`🚀 前置-SOCKS5`** 这个住宅节点 **追加**到你现有的“节点选择”分组里（不删除你原来的节点）
  - 如需完全跳过可用：`RESI_SKIP_NODE_SELECT_REWRITE=true`

#### “先选前置节点，再选住宅出口”是什么意思？
- 前置节点（dialer）决定：住宅节点“拨号时”走哪一批普通节点
- 住宅出口决定：最终公网出口 IP（住宅 IP）

#### 前置拨号候选怎么控制（默认 all，也支持 regex）
`.env`：
- `RESI_DIALER_MODE="all"`：把订阅里的所有普通节点都作为候选（默认、最稳）
- `RESI_DIALER_MODE="regex"` + `RESI_DIALER_REGEX="(US|美国|🇺🇲|🇺🇸)"`：只筛出匹配的节点作为候选

---

### `bypass` 会做什么（按脚本逻辑）

目标：在“系统代理/看起来全局”以及 TUN 场景下，尽量保证这些目标**永远直连**。

- **IP 网段（推荐 RFC1918）**
  - `BYPASS_IP_CIDRS="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"`
- **域名白名单（示例用 baidu.com，实际请写你自己的）**
  - `BYPASS_DOMAINS="baidu.com"`
- **DNS（可选）**
  - `BYPASS_INTERNAL_DNS="system"`：用 macOS 当前系统 DNS（自动跟随变化）
  - 或指定：`BYPASS_INTERNAL_DNS="10.0.0.2,10.0.0.3"`

脚本会写入/补全：
- `dns.fake-ip-filter`（避免白名单域名被 fake-ip 影响）
- （mihomo 模式下）`tun.route-exclude-address`（让 RFC1918 网段尽量“不进 TUN/不进代理链路”）
- `rules:` 顶部插入 `DIRECT` 规则作为兜底

查看当前检测到的系统 DNS：

```bash
python3 "patch_clash_subscription.py" dummy --print-system-dns
```

---

### 兼容性（强烈建议看一眼）

这个脚本**离线 patch YAML**，无法 100% 确认你运行时内核；因此提供 `--compat`：

- `--compat auto`（默认）：根据 YAML 内容做启发式判断
- `--compat mihomo`：允许写 `dialer-proxy` / `tun` / `dns.nameserver-policy`
- `--compat classic`：保守模式（跳过可能不支持的字段，尽量不写崩）

---

### 常用命令

只看会改什么（不写入）：

```bash
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --dry-run
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --diff
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml" --changelog
```

执行写入（默认会生成 `.bak.*` 备份）：

```bash
python3 "patch_clash_subscription.py" "/绝对路径/你的订阅.yaml"
```

回滚：
- 用最近的 `*.bak.YYYYmmdd-HHMMSS` 覆盖原文件即可
