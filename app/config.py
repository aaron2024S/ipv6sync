# -*- coding: utf-8 -*-
"""配置加载：全部来自环境变量（便于 Docker 部署），可选额外的 JSON 规则文件。"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field


def _env(name: str, default=None):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _env_secret(name: str) -> str:
    """取凭据，两种给法都支持：

      NAME=xxx           直接给值（.env / -e）
      NAME_FILE=/path     从文件读（Docker secret / K8s secret 挂进来的那种）

    读文件时去掉首尾换行 —— `docker secret`、`echo`、编辑器保存出来的文件
    几乎都带一个结尾换行，不去掉就会变成密码的一部分。
    """
    direct = os.environ.get(name)
    if direct:
        return direct
    path = os.environ.get(name + "_FILE")
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip("\r\n")
    except OSError as e:
        raise SystemExit(f"[配置错误] 读 {name}_FILE={path} 失败: {e}")
    except UnicodeDecodeError:
        raise SystemExit(
            f"[配置错误] {name}_FILE={path} 不是 UTF-8 文本。\n"
            f"  注意：别用 PowerShell 的 `... > file`（默认 UTF-16LE），"
            f"用 `-Encoding utf8` 或直接 printf 写。")


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return int(str(v).strip())
    except ValueError:
        raise SystemExit(f"[配置错误] {name} 必须是整数，当前为 {v!r}")


@dataclass
class Rule:
    """一条白名单规则 = 白名单里的一个条目。

    port 支持三种形态：None / -1 = 全部端口；单个端口 int；
    多端口时是规格字符串（如 "16667,5005"），由 trustlist.parse_ports 展开成多条条目。
    """
    name: str
    port: "int | str | None" = None    # None / -1 = 全部端口；带逗号 = 多端口规格
    remote_ip: str = "::/0"
    mac: str | None = None             # 目标设备 MAC，用来核对「这条规则确实指向本机」

    def __post_init__(self):
        if not self.name:
            raise SystemExit("[配置错误] 规则必须要有 name")
        if self.port is not None:
            s = str(self.port).strip()
            if s in ("", "-1"):
                self.port = None
            elif "," in s:
                # 多端口规格：保持字符串，交给 trustlist.parse_ports 校验与展开
                self.port = s.replace("，", ",")
            else:
                self.port = int(s)


@dataclass
class Config:
    # --- 路由器 ---
    host: str = "192.168.3.1"
    scheme: str = "http"
    username: str = "admin"
    password: str = ""
    password_source: str = ""          # 密码从哪来的（env / file / session），仅用于日志
    timeout: float = 10.0
    session_file: str = "/data/session.json"

    # --- 规则 ---
    rules: list[Rule] = field(default_factory=list)

    # --- 地址发现（固定流程，没有可选分支） ---
    # 只用一条路：**读本机网络接口的全局 IPv6 → 和白名单比对 → 有变化才更新**。
    #
    # 为什么不走路由器 HostInfo：那张设备表本质是邻居表缓存，更新有延迟，
    # 还会留着设备已经不用了的旧地址；而本机网卡上的地址才是报文真正能落地的
    # 地方，也是唯一的地面真值。代价是**必须 host 网络部署** —— 否则读到的是
    # 容器自己的网卡。取址方式、优先级、写几条、接口名、后缀这类开关已全部移除。
    mac: str | None = None             # 目标设备 MAC，用来核对规则确实指向本机

    # --- 行为 ---
    poll_interval: int = 60
    ensure_firewall_on: bool = False
    verify_after_write: bool = True
    dry_run: bool = False
    once: bool = False

    # --- 退避（密码错锁账号，必须慢） ---
    login_backoff_base: int = 300      # 秒，首次失败后等待
    login_backoff_max: int = 21600     # 秒，上限 6 小时
    net_retry_delay: int = 15          # 网络类错误的重试间隔

    # --- 通知（防火墙白名单变更后推送；三渠道独立，可同时启用） ---
    notify_title: str = "IPv6 白名单已更新"  # 三渠道共用的消息标题
    notify_ntfy_enabled: bool = False   # ntfy 渠道开关
    notify_ntfy_url: str = ""           # ntfy 服务器地址（建议带主题）
    notify_ntfy_token: str = ""         # ntfy 访问令牌（不开鉴权可留空）
    notify_gotify_enabled: bool = False # gotify 渠道开关
    notify_gotify_url: str = ""         # gotify 服务器地址（根路径）
    notify_gotify_token: str = ""       # gotify 应用 Token
    notify_wecom_enabled: bool = False  # 企业微信渠道开关
    notify_wecom_id: str = ""           # 企业微信机器人 ID（webhook key= 后那串）

    # --- 观测 ---
    health_port: int = 8099
    log_level: str = "INFO"
    log_json: bool = False

    # --- Web 控制台 ---
    web_host: str = "0.0.0.0"
    web_port: int = 6600               # 0 = 不启动控制台（与 PORT 的默认值保持一致）
    web_user: str = "admin"            # ADMIN_USERNAME
    web_password: str = ""             # ADMIN_PASSWORD；空 = 不启动控制台（安全默认）
    web_session_ttl: int = 12 * 3600

    # --- 运行时设置覆盖层（网页保存的文件） ---
    settings_file: str = "/data/settings.json"
    # 跨容器重启的累计计数（新增/更新/删除条目数）。放在挂载卷里，
    # 否则每次重启「累计写入」都归零，用户没法判断到底同步过没有。
    state_file: str = "/data/state.json"
    # 同步记录保留条数（/data/events.jsonl 超出丢弃最旧）。
    # 不在设置表单里露出 —— 只在「同步记录」页的工具行里调。
    events_max: int = 100

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}"


def load_rules_from_env() -> list[Rule]:
    """优先用 RULES（JSON 数组，可配多条不同设备的规则）；
    否则用扁平的单规则变量 ENTRY_NAME / PORT / REMOTE_IP（最常见场景）。"""
    raw = _env("RULES") or _env("RULES_JSON")
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"[配置错误] RULES 不是合法 JSON: {e}")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise SystemExit("[配置错误] RULES 必须是数组或对象")
        rules = []
        for item in data:
            if not isinstance(item, dict):
                raise SystemExit("[配置错误] RULES 数组元素必须是对象")
            rules.append(Rule(
                name=item.get("name") or item.get("Name"),
                port=item.get("port", item.get("Port")),
                remote_ip=item.get("remote_ip") or item.get("remoteIp") or "::/0",
                mac=item.get("mac"),
            ))
        if not rules:
            raise SystemExit("[配置错误] RULES 为空")
        return rules

    if _env("RULES_FILE"):
        path = _env("RULES_FILE")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"[配置错误] 读 RULES_FILE={path} 失败: {e}")
        if isinstance(data, dict):
            data = data.get("rules", [data])
        return [Rule(name=i.get("name"), port=i.get("port"),
                     remote_ip=i.get("remote_ip", "::/0"),
                     mac=i.get("mac")) for i in data]

    # 单规则：从扁平变量拼
    return [Rule(
        name=_env("ENTRY_NAME", "NAS"),
        port=_env("ENTRY_PORT"),
        remote_ip=_env("REMOTE_IP", "::/0"),
        mac=_env("TARGET_MAC"),
    )]


def _default_state_file(settings_file: str) -> str:
    """累计计数文件默认与设置文件同目录 —— 两者都在挂载卷里，一起持久化。"""
    d = os.path.dirname(os.path.abspath(settings_file or "/data/settings.json"))
    return os.path.join(d, "state.json")


def load_config(argv: list[str] | None = None) -> Config:
    argv = argv if argv is not None else sys.argv[1:]
    password = _env_secret("ROUTER_PASSWORD")
    if _env("ROUTER_PASSWORD"):
        pw_src = "ROUTER_PASSWORD"
    elif os.environ.get("ROUTER_PASSWORD_FILE"):
        pw_src = f"ROUTER_PASSWORD_FILE={os.environ['ROUTER_PASSWORD_FILE']}"
    else:
        pw_src = ""
    # 设置文件与累计计数文件都在挂载卷里，默认同目录
    settings_file = _env("SETTINGS_FILE", "/data/settings.json")
    cfg = Config(
        host=_env("ROUTER_HOST", "192.168.3.1"),
        scheme=_env("ROUTER_SCHEME", "http"),
        username=_env("ROUTER_USER", "admin"),
        password=password,
        password_source=pw_src,
        timeout=float(_env("ROUTER_TIMEOUT", "10")),
        session_file=_env("SESSION_FILE", "/data/session.json"),
        mac=_env("TARGET_MAC"),
        poll_interval=_env_int("POLL_INTERVAL", 60),
        ensure_firewall_on=_env_bool("ENSURE_FIREWALL_ON", False),
        verify_after_write=_env_bool("VERIFY_AFTER_WRITE", True),
        dry_run=_env_bool("DRY_RUN", False),
        once=_env_bool("ONCE", False),
        login_backoff_base=_env_int("LOGIN_BACKOFF_BASE", 300),
        login_backoff_max=_env_int("LOGIN_BACKOFF_MAX", 21600),
        net_retry_delay=_env_int("NET_RETRY_DELAY", 15),
        notify_title=_env("NOTIFY_TITLE", "IPv6 白名单已更新"),
        notify_ntfy_enabled=_env_bool("NOTIFY_NTFY_ENABLED", False),
        notify_ntfy_url=_env("NOTIFY_NTFY_URL", ""),
        notify_ntfy_token=_env_secret("NOTIFY_NTFY_TOKEN"),
        notify_gotify_enabled=_env_bool("NOTIFY_GOTIFY_ENABLED", False),
        notify_gotify_url=_env("NOTIFY_GOTIFY_URL", ""),
        notify_gotify_token=_env_secret("NOTIFY_GOTIFY_TOKEN"),
        notify_wecom_enabled=_env_bool("NOTIFY_WECOM_ENABLED", False),
        notify_wecom_id=_env("NOTIFY_WECOM_ID", ""),
        health_port=_env_int("HEALTH_PORT", 8099),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_json=_env_bool("LOG_JSON", False),
        web_host=_env("WEB_HOST", "0.0.0.0"),
        web_port=_env_int("PORT", 6600),
        web_user=_env("ADMIN_USERNAME", "admin") or "admin",
        web_password=_env_secret("ADMIN_PASSWORD"),
        web_session_ttl=_env_int("WEB_SESSION_TTL", 12 * 3600),
        settings_file=settings_file,
        state_file=_env("STATE_FILE", "") or _default_state_file(settings_file),
        events_max=max(10, min(5000, _env_int("LOG_MAX", 100))),
    )

    # 通知渠道的字段合法性由网页设置层校验（URL 格式 / 机器人 ID），这里不再拦截

    # 命令行覆盖（方便 `docker run --rm ... --once` 调试）
    it = iter(argv)
    for a in it:
        if a == "--once":
            cfg.once = True
        elif a == "--dry-run":
            cfg.dry_run = True
        elif a == "--debug":
            cfg.log_level = "DEBUG"
        elif a == "--host":
            cfg.host = next(it, cfg.host)
        elif a in ("-h", "--help"):
            raise SystemExit(HELP)

    rules = load_rules_from_env()
    # 全局的 TARGET_MAC 作为规则的默认值
    for r in rules:
        r.mac = r.mac or cfg.mac
    cfg.rules = rules

    # 最后盖一层「网页改过的设置」—— 优先级高于环境变量。
    # 放在规则装配之后，这样 ENTRY_NAME / PORT 这类字段能落到 rules[0] 上。
    from . import settings as _settings        # 延迟导入：settings 只依赖标准库
    _settings.apply_overlay(cfg, _settings.load_overlay(cfg.settings_file))

    # 常驻模式：没有密码也照常启动 —— 用户要能先进网页控制台把密码填上
    #（纯网页首次配置的关键）。缺密码时同步循环会一直停在「未配置」状态，
    # 不会去碰路由器，更不会拿空密码去撞登录（连续错 3 次会锁账号）。
    # --once 单次模式不同：跑一轮就退出，没有凭据必然一事无成，直接干净报错。
    if not cfg.password and not cfg.dry_run and cfg.once:
        if not (cfg.session_file and os.path.exists(cfg.session_file)):
            raise SystemExit(
                "[配置错误] 既没有密码、也没有可用会话。--once 模式三种给法任选一种：\n"
                "  1) env 里写             ROUTER_PASSWORD=你的密码\n"
                "  2) Docker secret 文件    ROUTER_PASSWORD_FILE=/run/secrets/router_password\n"
                "  3) 复用既有会话          把有效的 session.json 放到 "
                f"{cfg.session_file}\n"
                "     （只在会话还有效时管用；它会过期，过期后仍然需要密码）")

    return cfg


HELP = """\
华为路由器 IPv6 防火墙白名单动态同步

工作方式（固定，没有可选分支）:
  每条白名单规则按其绑定方式取址，与路由器 IPv6 防火墙白名单里本程序维护
  的条目比对，**有变化才更新**（地址没变就不碰设备，避免无谓的 flash 写入）：
  · 绑定设备 MAC 的规则（网页控制台白名单页添加）：取路由器设备表（HostInfo）
    里该设备的**第一条地址** —— 华为的排序是「最新出现优先」，第一条就是该
    设备现役的地址，也是路由器自己选设备时会填的那条。设备离线或表里没有
    它时本轮跳过、保持白名单不动。
  · 未绑定 MAC 的规则（环境变量来的老配置）：读本机网络接口上的全局 IPv6
    （容器必须 host 网络），deprecated / tentative 已被 flags 过滤。
  环境变量里旧的 ENTRY_NAME / PORT / REMOTE_IP / TARGET_MAC 仍可用，
  首次读取时自动迁移成规则列表（也持久化进 settings.json 的 RULES）。

环境变量（核心）:
  ROUTER_HOST            路由器地址，默认 192.168.3.1
                         （容器内通常是 LAN 网关；network_mode: host 时可直接用）
  ROUTER_USER            管理员用户名，默认 admin（华为设备固定是 admin）
  ROUTER_PASSWORD        管理员密码
  ROUTER_PASSWORD_FILE   改从文件读密码（Docker secret 场景），与上一项二选一
  ENTRY_NAME             白名单条目名，默认 NAS（同名则改、不存在则加）
  ENTRY_PORT             放行端口；留空或 -1 = 全部端口，多个用英文逗号分隔
                         （如 16667,5005，每端口生成一条条目：NAS、NAS-5005…）
                         注意不是 PORT —— PORT 是控制台端口（见文件末尾）
                         【以上条目类变量推荐改用网页控制台的白名单页按设备添加】
  REMOTE_IP              允许来源，默认 ::/0（不限制）
  TARGET_MAC             目标设备 MAC（可选）；填了就把这条规则改为「按设备」
                         跟随路由器设备表维护，不填则读本机网卡
  POLL_INTERVAL          轮询秒数，默认 60
  ENSURE_FIREWALL_ON     是否自动打开 IPv6 防火墙开关，默认 false
  DRY_RUN                只打印不写入
  HEALTH_PORT            健康检查端口，默认 8099

通知（防火墙白名单变更后推送；也可只在网页控制台里配。三个渠道互相独立，
启用几个就同时推几个）:
  NOTIFY_TITLE           三渠道共用的消息标题，默认「IPv6 白名单已更新」；
                         测试通知的标题末尾会自动加「（测试）」
  NOTIFY_NTFY_ENABLED    true/false，启用 ntfy 渠道
  NOTIFY_NTFY_URL        ntfy 服务器地址（建议带主题，如 https://ntfy.sh/mytopic）
  NOTIFY_NTFY_TOKEN      ntfy 访问令牌，不开鉴权可留空（也可用 NOTIFY_NTFY_TOKEN_FILE）
  NOTIFY_GOTIFY_ENABLED  true/false，启用 gotify 渠道
  NOTIFY_GOTIFY_URL      gotify 服务器地址（只填到根路径）
  NOTIFY_GOTIFY_TOKEN    gotify 应用 Token（也可用 NOTIFY_GOTIFY_TOKEN_FILE）
  NOTIFY_WECOM_ENABLED   true/false，启用企业微信渠道
  NOTIFY_WECOM_ID        企业微信群机器人 ID，webhook 里 key= 后面那段；
                         直接粘贴整个 webhook 地址也行

Web 控制台（浏览器里改设置）:
  ADMIN_USERNAME         控制台登录用户名，默认 admin
  ADMIN_PASSWORD         控制台登录密码，写进 compose 的 environment；
                         不设就不启动控制台（安全默认）
  ADMIN_PASSWORD_FILE    改从文件读控制台密码（Docker secret）
  PORT                   控制台端口，默认 6600；0 = 关闭（compose: - PORT=3500）
  WEB_HOST               控制台监听地址，默认 0.0.0.0
  WEB_SESSION_TTL        登录有效期（秒），默认 43200
  SETTINGS_FILE          网页保存的设置文件，默认 /data/settings.json
                         优先级：网页设置 > 环境变量 > 默认值
  STATE_FILE             累计写入计数的落盘位置，默认与 SETTINGS_FILE 同目录
                         的 state.json。放在卷里，容器重启后「累计写入」不归零

首次部署推荐流程：compose 里只给 ADMIN_USERNAME / ADMIN_PASSWORD，
其余（路由器地址/密码、NAS MAC、检测周期…）打开网页控制台填，改完即生效。

命令行:
  --once        跑一轮就退出（配合 cron / 计划任务）
  --dry-run     演练，不写路由器
  --debug       详细日志
  --list-hosts  打印在线设备与其 IPv6（用来查 TARGET_MAC）
  --whitelist   打印路由器当前的 IPv6 白名单
"""
