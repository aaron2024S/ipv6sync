# 离线镜像：手工组装的 `ipv6sync` docker-save tar

本机**没有 Docker 引擎**（`docker` 命令不存在），跑不了 `docker build`。
所以这里不模拟构建，而是按 docker-save 的公开格式**手工拼出归档** ——
产物能被目标 NAS 的 `docker load -i` 直接导入。

- 交付目录：`offline/dist/`（整个目录拷到 NAS 即可，不需要源码、不需要外网）
- 基础镜像：`library/python:3.12-alpine`（解析到 Alpine 3.24.2 + Python 3.12）
- 应用：`ipv6sync`，纯标准库，零 pip 依赖

---

## 一、产物清单

| 文件 | 大小 | 说明 |
|---|---|---|
| `ipv6sync-1.0.6-amd64-image.tar` | 51.4 MB | x86_64 NAS / PC —— **唯一产物** |
| `docker-compose.offline.yml` | — | 无 `build` 段的 compose，`image:` 直接引用本地镜像 |
| `.env.example` | — | 进阶用环境变量模板（可选；常规部署不需要 `.env`） |
| `SHA256SUMS.txt` | — | tar 的 sha256 |
| `build-report.json` | — | 构建报告：层 diff_id、来源镜像 digest、产出哈希 |

只打 **amd64**（本项目只需这一份）。tar 是 5 层：4 层来自 `python:3.12-alpine`，
第 5 层是本应用。

想额外要 ARM 版本（群晖 DS223/DS124 这类机型、树莓派）：
`powershell -ExecutionPolicy Bypass -File build.ps1 -Only arm64`，
产物会并排放在 `dist/` 里。

> 手工拼装时 `repositories` / 层目录名等都按 moby 的读取路径来写；
> `manifest.json` 里的 `Layers` 是**底 → 顶**，最后一层是本应用层。

---

## 二、在 NAS 上部署

### 1. 先确认 NAS 的 CPU 架构

```bash
uname -m
```

应该是 `x86_64`（产物只有 amd64 这一份）。若输出 `aarch64` / `arm64`，
说明 NAS 是 ARM 机型，需要回去用 `build.ps1 -Only arm64` 另打一份。

群晖可以在「控制面板 → 信息中心」看到机型；大部分 Plus/Value 系列是 `x86_64`，
部分入门机型（如 DS223、DS124）是 ARM。

### 2. 校验完整性（可选但建议）

```bash
sha256sum -c SHA256SUMS.txt
```

### 3. 导入镜像

```bash
docker load -i ipv6sync-1.0.6-amd64-image.tar
```

成功后会有 `Loaded image: ipv6sync:latest`。核对一下：

```bash
docker images ipv6sync
docker inspect ipv6sync:latest --format '{{.Config.User}} {{.Config.WorkingDir}} {{.Config.Entrypoint}}'
# 期望：syncapp /app [python -u -m app.main]
```

### 4. 唯一要设置的：控制台登录账号

打开 `docker-compose.offline.yml`，把 `environment:` 里这两行改成自己的：

```yaml
    environment:
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=change-me-please
```

其余一切（路由器地址与密码、NAS 的 MAC、检测周期、条目名与端口……）
**全部在网页控制台里设置**，存进 `/data/settings.json`（0600），
改完热生效，不需要 `.env`，也不需要重建容器。

> 进阶（Docker secret、批量部署）仍可用环境变量覆盖，如 `ROUTER_PASSWORD_FILE`、
> `ROUTER_PASSWORD` 等；网页设置的优先级高于环境变量。

### 5. 让容器能写挂载目录

容器以 **uid 10001**（非 root）运行，而 `./data` 是宿主机上你的目录。
属主不对的话登录会失败（写不进 `/data/session.json`）：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
```

不想操心底层属主，就把 compose 里的 `./data:/data` 换成命名卷
`ipv6sync-data:/data`（文件末尾有注释掉的 `volumes:` 段，一并启用）——
Docker 创建命名卷时会按镜像里的属主初始化，天然可写。

`data/` 目录里会出现这几个文件（都不含路由器密码以外的秘密，属主 10001、权限 0600）：

| 文件 | 内容 |
|---|---|
| `session.json` | 路由器会话（Cookie + CSRF 令牌），删掉即强制重登 |
| `settings.json` | 网页上改过的设置。**里面会有明文路由器密码**（你在网页里填过就有） |
| `state.json` | 累计写入次数。放在卷里所以容器重启不归零，网页「累计写入」就靠它 |

网页上「累计写入」这一格可以悬停：会显示新增/更新/删除细分、本次启动以来的次数
和最近写入时间 —— 对照着看就能分清"确实没写过"和"刚重启过、计数从头开始"。

### 6. 启动

```bash
docker compose -f docker-compose.offline.yml up -d
docker compose -f docker-compose.offline.yml logs -f
```

第一次跑如果还没填 NAS 的 MAC，浏览器控制台点「读取设备列表」就能查到；
或用命令行：

```bash
docker compose -f docker-compose.offline.yml run --rm ipv6sync --list-hosts
```

### 7. 打开浏览器控制台

```
http://<NAS 的 IP>:6600/
```

（端口默认 6600，compose 的 `environment` 里加 `- PORT=xxxx` 可改，`0` = 关闭。）

用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。第一次进来先在「路由器连接」里
填**路由器地址与管理员密码**，保存后自动开始同步；NAS 的 MAC、检测周期等
也在同一页面改。**改完即生效，不用重建容器**。还能读白名单与在线设备、
手动开关 IPv6 防火墙。

⚠ 这是局域网管理后台，**别映射到公网**，别用弱口令 —— 它能改路由器密码。

### 8. 确认在跑

```bash
curl -s http://127.0.0.1:8099/healthz | jq
```

`status: ok` 且 `consecutive_failures: 0` 就说明最近一轮同步成功了。
（`Dockerfile` 里的 `HEALTHCHECK` 用的就是这个端点，`docker ps` 会显示 `healthy`。）

---

## 三、镜像内容与 Dockerfile 的字段对应

手工拼装时逐句对齐了上游 `Dockerfile`：

| Dockerfile 指令 | 落到镜像里的什么 |
|---|---|
| `FROM python:3.12-alpine` | 复用 offline 拉取并双重校验过的 4 个基础层 |
| `LABEL org.opencontainers.image.*` | `config.Labels` |
| `ENV PYTHONUNBUFFERED=1` … 共 10 项 | `config.Env`（与基础镜像环境合并，同名覆盖，无重复键） |
| `WORKDIR /app` | `config.WorkingDir` = `/app` |
| `RUN addgroup -g 10001 -S syncapp` | 改写 `/etc/group` 追加 `syncapp:x:10001:` |
| `RUN adduser -u 10001 -S -G syncapp -H` | 改写 `/etc/passwd`、`/etc/shadow` 追加 uid/gid 10001 |
| `RUN mkdir -p /data && chown` | 层内 `data/` 目录条目，属主 10001:10001 |
| `COPY app/ /app/app/` | 层内 `app/app/*.py`（13 个文件） |
| `RUN chown -R syncapp:syncapp /app` | `/app` 与 `/app/app` 的 uid/gid 直接写成 10001 |
| `USER syncapp` | `config.User` = `syncapp` |
| `VOLUME ["/data"]` | `config.Volumes` = `{"/data": {}}` |
| `EXPOSE 8099 6600` | `config.ExposedPorts` = `{"8099/tcp": {}, "6600/tcp": {}}` |
| `HEALTHCHECK --interval=60s …` | `config.Healthcheck`（interval 60s / timeout 6s / start-period 25s / retries 3） |
| `ENTRYPOINT ["python","-u","-m","app.main"]` | `config.Entrypoint`；同时把 `config.Cmd` 置空（只设 ENTRYPOINT 时 `docker build` 会清掉基础镜像的 CMD，这里保持一致） |

> 手工拼 config 时最容易漏的就是 `ENV` 这类"不体现在层里"的东西 ——
> 层是复用的，config 是自己写的。所以 `validate_image.py` 会拿 Dockerfile 里
> 那几条路径/端口默认值**逐条核对**，并确认镜像里没有烘进任何密码。

---

## 四、与 `docker build` 的差异（已知限制）

1. **`/etc/passwd`、`/etc/group`、`/etc/shadow` 是"语义等价"而非逐字节复刻。**
   追加的行按 busybox `addgroup -S` / `adduser -S` 的格式手工写成
   （uid/gid 10001、shell `/sbin/nologin`、口令字段 `!` 锁定）。
   这三处不影响运行 —— 容器只是以这个 uid 直接运行，不做登录认证。
2. **层内时间戳是固定值**（本版取 `2026-09-21T00:00:00Z`），
   所以元数据里看不到"真实构建时刻"。这样换来的是**可复现构建**：
   同样的输入能产出逐字节相同的 tar，sha256 可以当校验基准。
3. **`docker history` 里本应用只有一条 `offline-build: …` 记录**，
   看不到中间步骤 —— 这里没有执行过 `RUN`，只改了 config 和加了一层。
4. **只打了 `latest` 一个标签。** 版本信息在 tar 文件名、镜像 LABEL
   和 `/app/app/__init__.py` 的 `__version__` 里。
5. **基础层是拉取时刻的 `python:3.12-alpine`。** 它是滚动标签，
   所以这份产物锁定的是当时解析到的 `Alpine 3.24.2`（manifest digest 见
   `build-report.json` 的 `base.image_digest`）。要升级基础镜像得重新跑一遍组装。

---

## 五、出包前做了哪些校验

静态校验全绿、容器一启动就崩，是这类手工组装的典型故障。所以这里分两层校验，
脚本都留在本目录，可随时复跑。

### 静态校验 `validate_image.py`（只读字节）

- `manifest.json` 的 `Config` 文件名 == `sha256(config 内容)`
- `len(Layers) == len(config.rootfs.diff_ids)`，且每层
  `sha256(layer.tar) == diff_ids[i]` ← **`docker load` 会校验这一条**
- 每层内部：无绝对路径条目、无 `..`、无反斜杠、**同层内无重复路径**
  （重复路径会被 docker 的解包器拒绝：`duplicates of file paths not supported`）
- 基础系统文件在位：`/bin/sh`、`lib/ld-musl-*.so.1`、`/etc/passwd`
- 入口 `python` 顺着 PATH 解析到 `usr/local/bin/python3.12`，
  确认是**目标架构的 ELF**（amd64 0x3E / arm64 0xB7）且带可执行位
- `/app`、`/app/app`、`/data` 存在且属主都是 10001:10001
- `config.User` 能在镜像自己的 `/etc/passwd` 里查到、uid 对得上
- **`app.main` 依赖的每个模块都在层里** —— 手工拼装最容易漏的就是"新加了文件没进层"，
  表现是容器一启动 `ModuleNotFoundError`
- **控制台页面里有登录表单**（`type="password"` 与 `/api/login`）——
  如果哪天改坏了登录逻辑，这里会拦住
- **`config.Env` 里的路径/端口默认值逐条核对**（`SESSION_FILE`/`SETTINGS_FILE`/
  `HEALTH_PORT`/`PORT`），并确认**镜像里没有烘进任何密码**
- `config.ExposedPorts` 同时含 `8099/tcp` 与 `6600/tcp`
- 层内 `.py` 无 BOM、纯 LF
- `main.py` 里确实实现了 `/healthz`（否则 HEALTHCHECK 永远失败）

### 行为校验 `validate_boot.py`（真跑）

在临时目录复现镜像内的目录结构与环境变量，用宿主 python 当**桩**替换镜像里的
Linux 解释器，验证：

- 层内 13 个 `.py` 全部编译通过
- `config.Env` 含全部 10 个应用变量、无重复键、基础镜像的 `PATH` 保住了
- **工作目录真的被用上**：cwd=`/app` 时 `-u -m app.main --help` 能跑；
  换个 cwd 立刻 `No module named app` —— 证明 `WorkingDir=/app` 是承重的，
  不是个摆设（cwd 不对就会无限重启，是这类镜像最隐蔽的坑）
- 被 import 的 `app` 包确实来自镜像层，而不是仓库里的源码副本
- **镜像里的那份代码跑通仓库的离线单测**
- 缺 `ROUTER_PASSWORD` 时 `--once` 模式是干净的配置错误（退出码 2、无 traceback）；
  常驻模式零凭据则**常驻待机**（T10），等用户在网页里填密码
- `HEALTHCHECK` 的命令行真的可用：对 200 的健康端点返回 0、对死端口返回非 0；
  并确认 `-c` 参数里没有双引号（否则穿过 shell 会被截断）
- `SESSION_FILE` 与 `SETTINGS_FILE` 都落在声明的卷 `/data` 内
- **T9：真起一次 Web 控制台**（用镜像里的代码，路由器指向死地址以防误碰真设备）：
  未登录时 `/api/state` 只回 `authed=false`、动作接口 401 → 用口令登录拿到
  `HttpOnly` Cookie → `/api/state` 返回 5 组 / 23 个字段 → 保存设置**热生效**
  （条目名立刻变）并落盘 → 非法值 400 → 退出登录后令牌失效
- **T10：零凭据常驻待机**：不给 `ADMIN_PASSWORD` / 路由器密码时容器照常运行，
  日志提示去网页填密码 —— 这是"设置全进网页"首次部署路径的前提


结果：**静态 34 项 + 行为 33 项，0 FAIL / 0 WARN（amd64）。**

> 本机没有 Docker 引擎，所以**没有**做"真的 `docker load` 一次"这一验证。
> 上面第 2 条（逐层 diff_id 校验）是 `docker load` 唯一会强校验的东西，
> 已经对齐。如果你在 NAS 上 load 失败，请把报错原文贴出来。

---

## 六、想重新组装

```bash
cd offline

# 1) 拉基础层（需联网，约 25 MB；已拉过会命中缓存并重新校验）
python fetch_base.py                      # 默认只拉 amd64

# 2) 组装镜像 tar -> dist/
set SOURCE_DATE_EPOCH=1789948800 && python build_offline.py   # Windows
SOURCE_DATE_EPOCH=1789948800 python build_offline.py          # Linux/macOS

# 3) 校验
python validate_image.py
python validate_boot.py
```

Windows 上也可以用一键脚本：

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1            # 全流程（默认 amd64）
powershell -ExecutionPolicy Bypass -File build.ps1 -SkipFetch # 跳过拉取
powershell -ExecutionPolicy Bypass -File build.ps1 -Only arm64  # 需要 ARM 才用
```

要改应用代码，直接改 `../app/*.py` 后重跑第 2 步 —— 应用层是从源码目录现打的，
不需要先 `docker build`。版本号只有一处来源 —— `app/__init__.py` 的 `__version__`，
组装脚本与两个校验脚本都从那里读，改了它就行。

---

## 七、排错

| 现象 | 原因 / 处理 |
|---|---|
| `docker load` 报 `invalid diffID for layer N` | 层顺序或 `layer.tar` 内容错了。跑 `python validate_image.py --arch <架构>` 定位 |
| `docker load` 报 `duplicates of file paths not supported` | 同一层里有重复路径。`validate_image.py` 会先查出来 |
| `docker load` 报 `unsupported manifest version` / 找不到文件 | 用错架构的 tar，或传输中损坏。先 `sha256sum -c SHA256SUMS.txt` |
| 启动即退出，日志 `No module named app` | `WorkingDir` 没生效。`docker inspect ipv6sync:latest --format '{{.Config.WorkingDir}}'` 应为 `/app` |
| 日志 `Permission denied: /data/session.json` | `./data` 属主不是 10001。`sudo chown -R 10001:10001 data`，或改用命名卷 |
| 容器 `unhealthy` | `/healthz` 在"最近一轮同步成功"之前一直返回 503。先看日志里同步失败的原因；`curl :8099/state` 有完整快照（首次部署没填路由器密码时是 `unconfigured` 待机态，不算故障） |
| 日志 `未设置 ADMIN_PASSWORD —— Web 控制台不启动` | 正常，这是安全默认。要控制台就在 compose 的 `environment:` 里设 `ADMIN_USERNAME`/`ADMIN_PASSWORD`，`up -d --force-recreate` 重建生效 |
| 6600 连不上 | 同上；或端口被 NAS 上别的东西占了 → compose 里加 `- PORT=xxxx`。bridge 网络时别忘了 `ports:` 映射 |
| 控制台登录提示「失败次数过多」 | 5 分钟内错了 8 次触发锁定（防爆破）。等一会儿再试；反复触发锁定会逐次变长（上限 6 小时） |
| 网页改了设置没生效 | 确认点的是「保存并应用」；看表单字段旁的来源标记。若该字段显示 `网页设置`，说明它已盖过环境变量 |
| 想撤销网页上的改动 | 删掉 `data/settings.json` 里对应的键（整个删掉就完全回到环境变量生效） |
| 同步报找不到目标设备 | `TARGET_MAC` 为空或写错。用 `run --rm ipv6sync --list-hosts` 核对，或控制台「读取设备列表」 |
| **地址加进白名单了却还是连不通** | 设备有多个全局 IPv6，写进去的那条不是报文实际落地的那个。本版（≥1.0.2）默认 `WRITE_ALL=true`，会把发现的每个地址各写一条（`NAS`、`NAS@2`…）；在控制台「路由器 ipv6 白名单」栏点「读取白名单」即可看到每个条目写的是什么地址（「归属规则」列可分辨哪些是程序维护的），「运行状态」页的 JSON 里 `status.rules.NAS.addrs` 是本轮实际写入的全部地址。若仍只有一个地址，见主 README 的对照表 |
| 写白名单返回 `errcode 9003` | 设备的 IPv6 防火墙总开关是关的，白名单不生效。在控制台点「打开 IPv6 防火墙总开关」，或把 `ENSURE_FIREWALL_ON` 设为 `true`（会改变实际网络行为，想清楚再开） |
