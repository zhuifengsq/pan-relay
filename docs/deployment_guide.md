# pan-relay 生产环境部署与运维指南

本文档介绍 `pan-relay` 在生产服务器上的标准化容器部署、反向代理配置、数据备份与日常运维。

---

## 🚀 方式一：Docker Compose 生产部署（推荐）

### 1. 目录准备与克隆代码

```bash
git clone https://github.com/ucmao/pan-relay.git /opt/pan-relay
cd /opt/pan-relay
```

### 2. 环境变量配置 (`.env`)

复制环境变量模板：
```bash
cp .env.example .env
```

按需编辑 `.env` 文件：
```ini
# 服务端口
PORT=5004

# 系统时区
TZ=Asia/Shanghai

# 管理后台管理员登录凭据 (建议修改默认密码)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=YourStrongPasswordHere

# JWT 安全密钥
SECRET_KEY=your_random_secret_key_here
```

### 3. 启动容器

```bash
# 构建镜像并后台启动容器
docker compose up -d --build

# 检查运行状态与健康检查
docker compose ps

# 查看实时运行日志
docker compose logs -f
```

---

## 🔒 生产环境反向代理配置 (Nginx / HTTPS)

建议在生产环境使用 Nginx 反向代理并配置 SSL 证书。

### Nginx 虚拟主机配置示例

```nginx
server {
    listen 80;
    server_name search.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name search.yourdomain.com;

    # SSL 证书路径
    ssl_certificate /etc/letsencrypt/live/search.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/search.yourdomain.com/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # 客户端上传与请求体限制
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:5004;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 支持 SSE 流式搜索推流 (GET /api/v1/search/stream)
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 120s;
    }
}
```

> ⚠️ **重要提示**：因为系统支持流式搜索接口（SSE：`/api/v1/search/stream`），请务必在 Nginx 中配置 `proxy_buffering off;`，避免服务端推流被 Nginx 缓冲区拦截导致前端卡顿。

---

## 💾 数据持久化与备份策略

所有系统数据（账号池、转存资源、搜索源、系统配置、审计日志）均持久化保存在 SQLite 数据库：
* **数据目录**：`./data/pan_relay.db`
* **日志目录**：`./logs/`

### 自动热备份脚本示例 (crontab)

可配置 Linux 定时任务每天定时备份 SQLite 数据库文件：

```bash
#!/bin/bash
# /opt/scripts/backup_panrelay.sh
BACKUP_DIR="/opt/backups/pan-relay"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

# 使用 sqlite3 命令安全热备份（避免写入冲突）
sqlite3 /opt/pan-relay/data/pan_relay.db ".backup '$BACKUP_DIR/pan_relay_$DATE.db'"

# 清理 30 天前的旧备份
find "$BACKUP_DIR" -type f -name "pan_relay_*.db" -mtime +30 -delete
```

添加至系统定时任务：
```bash
# 每天凌晨 3 点自动备份
0 3 * * * /bin/bash /opt/scripts/backup_panrelay.sh
```

---

## ⏰ 内置后台定时任务说明

服务启动时，内置的 `APScheduler` 调度器会自动在后台并发执行以下定时工作：

| 任务名称 | 执行周期 | 功能说明 |
| :--- | :--- | :--- |
| **`cleanup_expired_temp_shares`** | 每 30 分钟 | 扫描并物理删除已过期的动态转存临时分享链接 |
| **`cleanup_all_storage`** | 每 12 小时 | 根据后台设置的超期保留策略，自动批量清理旧转存落盘资源 |
| **`scheduled_account_keepalive_job`** | 每 24 小时 | 对多账号池中所有有效账号执行静默保活与 Token 自动续期 |
| **`scheduled_resource_health_audit_job`** | 每 24 小时 | 对库内存量资源进行免登录批量健康巡检，标记死链与失效状态 |
| **`cleanup_old_logs`** | 每日 03:30 | 按保留期清理系统日志（`LOG_RETENTION_DAYS`，默认保留 30 天），控制库体积与查询开销 |

> 说明：`cleanup_old_logs` 的保留天数由环境变量 `LOG_RETENTION_DAYS` 控制；系统日志写入已改为**有界队列 + 后台单线程异步落库**（单次最多合并 200 条日志为一个事务批量写入），请求线程不再被日志 INSERT 阻塞，队列积压时自动回退为同步写入。

调度器启动时会尝试获取数据库目录下的 `scheduler.lock` 文件锁（Windows: `msvcrt.locking`，Linux/macOS: `fcntl.flock`）：

- 同一台机器上用 gunicorn/uwsgi 多进程启动时，**只有一个进程能拿到锁并执行定时任务**，其余进程会跳过并在日志中提示，避免重复清理 / 重复保活；
- 该锁是**单机文件锁**。多容器 / 多主机部署时必须只让一个实例执行定时任务：其余实例设置 `SCHEDULER_ENABLED=0`；
- 如需临时关闭当前实例的全部定时任务，同样设置 `SCHEDULER_ENABLED=0` 后重启。

---

## ⚡ 生产启动方式（waitress）与健康检查

仓库同时提供两种启动方式：

| 启动方式 | 命令 | 适用场景 |
| :--- | :--- | :--- |
| 开发调试（Flask 自带服务器） | `python app.py` | 本地开发、单请求调试，**不要用于生产** |
| 生产（waitress WSGI） | `python serve.py` | 容器 / 小规模生产环境，多线程、更稳健 |

`serve.py` 复用 `app.py` 初始化好的 Flask 应用（蓝图、SQLite、调度器），仅替换 WSGI 服务器，支持以下环境变量：

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `5004` | 监听端口 |
| `WAITRESS_THREADS` | `8` | waitress 工作线程数；SQLite 写多时可适当调大 |

健康检查端点：`GET /healthz`，仅探测进程与 SQLite（执行 `SELECT 1`），不触发登录跳转、不请求外部服务；正常返回 200 与 JSON `{"status":"ok","db":"ok","time":"..."}`，数据库不可用时返回 503。Dockerfile 与 docker-compose 的 `HEALTHCHECK` 已改为探测该端点。

---

## 🔧 本次性能优化新增的环境变量

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `SEARCH_UPSTREAM_MAX_WORKERS` | `32` | 全进程共享的上游搜索线程池大小（API / Telegram / 插件任务共用），避免并发搜索时线程数线性膨胀 |
| `SEARCH_UPSTREAM_DEADLINE_SECONDS` | `0`（关闭） | 单次上游搜索的全局截止时间；`>0` 时超时未完成的结果会被丢弃，防止个别慢上游拖住整体响应 |
| `LOG_RETENTION_DAYS` | `30` | 系统日志保留天数，由每日 `cleanup_old_logs` 任务执行清理 |
| `COOKIE_SECURE` | 跟随请求协议 | 管理员鉴权 Cookie 是否强制加 `Secure` 标记；HTTPS 部署建议显式设为 `1`（HTTP 内网部署保持默认，避免登录态失效） |
| `RESOURCE_SEARCH_LIMIT` | `500` | 站内资源库按标题搜索单次最多返回条数，避免资源量很大时把整表命中行一次性读入内存（接口与搜索缓存本身也有上限，正常展示不受影响） |
| `PAN_STORE_SETTLE_SECONDS` | `0.5` | 网盘转存成功后的等待秒数（用于规避上游风控）；批量入库脚本可设为 `0` 显著提速，默认值保持原有行为 |
| `SCHEDULER_ENABLED` | `1` | 设为 `0` 时当前进程不启动任何定时任务（多实例部署时只在主实例开启） |
| `SEARCH_PLUGIN_MAX_WORKERS` | `8` | 插件专用线程池大小，与 API/Telegram 上游池隔离，挂死插件不会占满整个搜索池 |
| `SEARCH_CIRCUIT_BREAKER_ENABLED` | `1` | 源级熔断开关：某源连续失败 3 次后冷却 60 秒，冷却期直接跳过 |
| `SEARCH_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `60` | 熔断基础冷却时间（秒）；half-open 探测失败后按 2 倍指数退避 |
| `SEARCH_CIRCUIT_BREAKER_MAX_COOLDOWN_SECONDS` | `300` | 熔断冷却时间上限（秒） |
| `SSE_MAX_CONCURRENT` | `64` | 单进程最大并发 SSE 连接数，超限返回 503 |
| `SLOW_REQUEST_MS` | `800` | 慢请求日志阈值（毫秒） |
| `TRUSTED_PROXIES` | 空 | 受信反向代理 IP/CIDR 白名单；未配置时不信任任何转发头 |
| `MAX_PAGE_SIZE` / `MAX_SEARCH_LIMIT` | `100` / `100` | 分页与搜索返回条数硬上限 |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` / `LOGIN_LOCKOUT_SECONDS` | `5` / `300` / `900` | 管理员登录失败锁定策略 |
| `PAN_TRANSFER_LOCK_DIR` | `<项目>/data/locks` | 跨进程转存幂等锁目录；多容器/多 worker 部署时必须指向共享卷（NFS 需实测 flock 支持） |

## 🔐 安全加固与可观测性（第二批）

### 反向代理与真实客户端 IP

服务默认**不信任**任何转发头（`X-Forwarded-For` / `X-Real-IP` / `CF-Connecting-IP` / `True-Client-IP`），只使用 TCP 直连地址。部署在 Nginx / CDN / 负载均衡之后时，必须配置受信代理白名单，否则限流、黑名单与访问日志拿到的都是代理 IP：

```bash
TRUSTED_PROXIES = 127.0.0.1,10.0.0.0/8
```

只有来自白名单地址的请求才会解析转发头（取第一个合法 IP），并可依据 `X-Forwarded-Proto: https` 判定为 HTTPS（影响鉴权 Cookie 的 `Secure` 标记）。Nginx 侧建议同时设置 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`。

### 输入边界

| 环境变量 | 默认值 | 作用 |
| :--- | :--- | :--- |
| `MAX_PAGE_SIZE` | `100` | 分页接口 `page_size` 硬上限；负数 / 超大值会被钳制，避免一次导出全表 |
| `MAX_SEARCH_LIMIT` | `100` | 聚合搜索接口 `limit` 硬上限 |

分页下限固定为 1，非法参数回退默认值（`page=1`、`page_size=10`、`limit=100`）。

### 登录防护与 JWT 吊销

- 管理员登录按客户端 IP 限速：`LOGIN_WINDOW_SECONDS`（默认 300 秒）内失败 `LOGIN_MAX_ATTEMPTS`（默认 5）次后锁定 `LOGIN_LOCKOUT_SECONDS`（默认 900 秒），期间返回 429 与 `Retry-After`；
- JWT 增加 `jti` 与令牌版本：登出会写入 `revoked_tokens` 吊销表；修改密码会递增 `admin_token_version`，**所有旧令牌立即失效**；每日 `cleanup_old_logs` 任务顺带清理过期吊销记录；
- 鉴权 Cookie 统一 `HttpOnly` + `SameSite=Lax`，`COOKIE_SECURE=1` 或可信 HTTPS 代理下加 `Secure`；
- 首次启动仍使用默认 `admin/admin123` 时会输出告警，请尽快在后台修改。

### SSE 与线程模型

每条 SSE 流式搜索连接会占用一个 waitress 工作线程：

- `SSE_MAX_CONCURRENT`（默认 64）限制单进程同时打开的流式连接数，超限返回 503 JSON，避免长连接占满线程池导致普通请求不可用；
- 大量用户使用流式搜索时，应同步调大 `WAITRESS_THREADS`（例如 32~64），并让普通请求走独立实例或前置队列；
- `/healthz` 返回 `sse_active` 与 `sse_capacity` 便于观察当前流式占用。

### 请求追踪与慢请求

每个请求分配 `request_id`（若客户端传入合法 `X-Request-ID` 则沿用），并写入响应头 `X-Request-ID`；耗时超过 `SLOW_REQUEST_MS`（默认 800 毫秒）的请求会记录 WARNING 日志，包含方法、路径、状态码、耗时与 request_id，便于定位慢接口。

### 写接口幂等

`/api/v1/transfer` 支持可选幂等键：请求头 `Idempotency-Key` 或 JSON 字段 `idempotency_key`（8~128 字符）。相同键在 24 小时内重复提交会直接返回首次结果，不会重复远程转存；过期记录由每日清理任务删除。不传幂等键时行为与原来一致。

### ⚠️ 多进程部署与限流说明

接口限流（`rate_limit_per_minute`、`max_concurrent_searches`）与搜索缓存当前**保存在单个进程的内存中**：

- 使用 `python serve.py`（waitress 单进程多线程）时，限流与缓存对所有请求共享，行为正确；
- 如果改用 gunicorn/uwsgi 等**多进程**方式启动，每个进程各自计数，实际限流阈值会被放大 N 倍，搜索缓存命中率也会下降。多进程部署时应把限流迁移到 Nginx / Redis 等外部组件，并保持 `worker` 数量为 1，或在进程外做全局限流。

### 🧩 搜索缓存语义

搜索缓存（进程内，TTL 300 秒、最多 500 个关键词、单个关键词最多 1000 条）只保存**完整枚举结束后、与调用方过滤条件无关的全集**：

- 同步搜索与 SSE 流式搜索共用同一份缓存；命中缓存时仍按本次请求的 `scope`、`target_clouds`、`limit` 和前台网盘配置做过滤，因此不同调用方不会互相污染结果；
- 单关键词 1000 条的上限仅用于限制内存占用，不改变调用方 `limit` 的语义（超出上限的部分本就不会在单次请求中全部返回）；
- `scope=own`（仅本地库）、达到 `limit` 提前结束、上游未完整返回时**不写缓存**，避免把不完整结果缓存 5 分钟；
- 修改系统配置、增删改 API 搜索源、启停 Telegram 频道、安装/更新插件后，缓存会立即整体失效，无需重启。

### 🔁 转存失败与超时处理

- 转存过程中遇到 **读超时 / 连接中断** 时，远程可能已经成功，继续切换账号会重复转存。此时会记录错误、停止账号切换并把本次操作判定为失败（返回空结果），由调用方决定是否重试；
- 远程转存成功但**本地入库失败**时（重试一次后仍失败），会尽力删除刚转存到网盘的远端文件并返回失败，不再把“部分成功”当成功返回，也不会写入转存缓存；
- 只有明确失败（如参数错误、明确的上游错误码）才会切换到下一个账号。

### 🗂️ 数据库索引迁移

启动时 `init_sqlite_db()` 会自动为旧库补建以下索引（幂等，可重复执行）：

- `idx_resources_share_created`：资源大厅“最近有分享链接的资源”倒序取前 N 条的部分索引（`WHERE share_link IS NOT NULL AND share_link <> ''`）；
- `idx_temp_share_status_expires`：过期临时分享清理的 `(status, expires_at)` 索引。

回滚时可手工删除：`DROP INDEX IF EXISTS idx_resources_share_created; DROP INDEX IF EXISTS idx_temp_share_status_expires;`（索引可随时重建，不改变数据）。

### 🔐 安全建议

- 生产环境务必在 `.env` 中设置随机 `SECRET_KEY`（未设置时启动日志会输出告警），否则管理员 JWT 存在被伪造风险；
- 首次登录后立即修改默认管理员密码（`admin` / `admin123`）；
- 反向代理到 HTTPS 后设置 `COOKIE_SECURE=1`，并为后台路径配置访问控制。