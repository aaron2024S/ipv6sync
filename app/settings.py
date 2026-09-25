# -*- coding: utf-8 -*-
"""
运行时设置覆盖层

网页上改的设置写进一个 JSON 文件（默认 /data/settings.json），优先级高于环境变量：

    网页设置  >  .env / docker -e  >  代码默认值

为什么要有这一层：容器里环境变量只在**创建容器时**注入一次，改 .env 得
`up -d --force-recreate` 重建容器。而"路由器 IP 填错了想改一下"这种事儿
不该要求重建容器 —— 所以可变的字段放在这个文件里，改完热生效。

安全：
  · 文件权限 0600，原子写（先写临时文件再 rename，不掉电留半个文件）
  · 只允许白名单里的键进来（表驱动，见 FIELDS），不接受任意键
  · 每个键都做类型与范围校验，坏值直接拒绝而不是带病运行
  · 路由器密码在这里是明文 —— 和放在 .env 里是同一安全等级，文档里已写明
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field

log = logging.getLogger("ipv6sync.settings")

# 日志级别可选值
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
SCHEMES = ("http", "https")


class SettingsError(Exception):
    """设置校验失败。消息直接面向使用者，会被原样显示在网页上。"""


@dataclass
class Field:
    key: str                       # 环境变量名，同时也是 settings.json 里的键
    label: str
    group: str
    kind: str                      # str | int | float | bool | select | secret
    default: str = ""
    attr: str | None = None        # 对应的 Config 属性名
    rule_attr: str | None = None   # 对应 rules[0] 的属性名
    choices: tuple = ()
    help: str = ""
    min: object = None
    max: object = None
    placeholder: str = ""
    secret: bool = False           # 不回显值，只回显"是否已设置"

    def to_dict(self, value, source: str):
        d = {
            "key": self.key, "label": self.label, "group": self.group,
            "kind": self.kind, "help": self.help, "source": source,
            "placeholder": self.placeholder,
        }
        if self.choices:
            d["choices"] = list(self.choices)
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        if self.secret:
            d["value"] = ""
            d["is_set"] = bool(value)
        else:
            d["value"] = value
        return d


G_ROUTER = "路由器连接"
G_DISCOVER = "地址检测"
G_ENTRY = "白名单条目"
G_BEHAVIOR = "运行行为"
G_NOTIFY = "通知设置"

FIELDS: tuple = (
    # ---- 路由器连接 ----
    Field("ROUTER_HOST", "路由器地址", G_ROUTER, "str", "192.168.3.1",
          attr="host", placeholder="192.168.3.1",
          help="LAN 网关地址；容器用 host 网络时填宿主机所在网段的网关"),
    Field("ROUTER_SCHEME", "协议", G_ROUTER, "select", "http",
          attr="scheme", choices=SCHEMES, help="家用路由器一般是 http"),
    Field("ROUTER_USER", "路由器管理员用户名", G_ROUTER, "str", "admin",
          attr="username", help="华为家用路由固定是 admin"),
    Field("ROUTER_PASSWORD", "路由器管理员密码", G_ROUTER, "secret", "",
          attr="password", secret=True, placeholder="留空表示不修改",
          help="改这里会以明文写入 settings.json（0600）；连续 3 次错会锁账号，务必一次填对"),
    Field("ROUTER_TIMEOUT", "请求超时（秒）", G_ROUTER, "float", "10",
          attr="timeout", min=1, max=120),
    # ---- 地址检测（流程固定：读本机网卡 → 和白名单比 → 有变化才更新） ----
    Field("POLL_INTERVAL", "检测周期（秒）", G_DISCOVER, "int", "60",
          attr="poll_interval", min=10, max=86400,
          help="多久检查一次地址变化；地址没变不会写路由器，所以调小也不折腾 flash"),
    Field("TARGET_MAC", "本机网卡 MAC（可不填）", G_DISCOVER, "str", "",
          attr="mac", placeholder="aa:bb:cc:dd:ee:ff",
          help="本程序只读「这台机器」的网卡地址（所以容器必须用 host 网络）。"
               "填了 MAC 就核对一下：不在这台机器的网卡上时会记一条告警到日志，"
               "提醒你可能写错放行对象了。不填不影响同步"),
    # ---- 白名单条目 ----
    Field("ENTRY_NAME", "条目名称", G_ENTRY, "str", "NAS",
          rule_attr="name", help="稳定主键：同名则原地更新，不会越加越多（上限 32 条）"),
    Field("PORT", "放行端口", G_ENTRY, "str", "-1", rule_attr="port",
          placeholder="-1 或 16667,5005,22",
          help="多个端口用英文逗号分隔，每台端口生成一条白名单条目；-1 或留空 = 全部端口"),
    Field("REMOTE_IP", "允许的来源", G_ENTRY, "str", "::/0",
          rule_attr="remote_ip", help="::/0 = 不限制来源"),
    # ---- 运行行为 ----
    Field("ENSURE_FIREWALL_ON", "自动打开 IPv6 防火墙总开关", G_BEHAVIOR, "bool",
          "false", attr="ensure_firewall_on",
          help="总开关关着时白名单不生效；设为 true 后本程序会自动打开它"),
    Field("VERIFY_AFTER_WRITE", "写完回读核对", G_BEHAVIOR, "bool", "true",
          attr="verify_after_write"),
    Field("DRY_RUN", "演练模式（只打印不写入）", G_BEHAVIOR, "bool", "false",
          attr="dry_run"),
    Field("LOG_LEVEL", "日志级别", G_BEHAVIOR, "select", "INFO",
          attr="log_level", choices=LOG_LEVELS),
    # ---- 通知设置（三渠道互相独立，启用几个就同时推几个） ----
    Field("NOTIFY_TITLE", "通知标题", G_NOTIFY, "str", "IPv6 白名单已更新",
          attr="notify_title",
          help="三个渠道共用的消息标题；企业微信和 ntfy 会把它显示为正文第一行。"
               "发测试通知时标题末尾自动加「（测试）」"),
    Field("NTFY_ENABLED", "启用 ntfy", G_NOTIFY, "bool", "false",
          attr="notify_ntfy_enabled",
          help="防火墙白名单每次被本程序修改后推送一条变更消息"),
    Field("NTFY_URL", "ntfy 服务器地址", G_NOTIFY, "str", "",
          attr="notify_ntfy_url", placeholder="https://ntfy.sh/mytopic",
          help="建议带上主题（地址路径）；不写主题默认发到 ipv6sync"),
    Field("NTFY_TOKEN", "ntfy Token", G_NOTIFY, "secret", "",
          attr="notify_ntfy_token", secret=True, placeholder="留空表示不修改",
          help="ntfy 的访问令牌（tk_ 开头）；服务器不开鉴权可留空"),
    Field("GOTIFY_ENABLED", "启用 gotify", G_NOTIFY, "bool", "false",
          attr="notify_gotify_enabled"),
    Field("GOTIFY_URL", "gotify 服务器地址", G_NOTIFY, "str", "",
          attr="notify_gotify_url", placeholder="https://push.example.com",
          help="只填到根路径，不用带 /message"),
    Field("GOTIFY_TOKEN", "gotify 应用 Token", G_NOTIFY, "secret", "",
          attr="notify_gotify_token", secret=True, placeholder="留空表示不修改",
          help="gotify 应用页面里生成的 Token，必填"),
    Field("WECOM_ENABLED", "启用企业微信", G_NOTIFY, "bool", "false",
          attr="notify_wecom_enabled"),
    Field("WECOM_ID", "企业微信机器人 ID", G_NOTIFY, "str", "",
          attr="notify_wecom_id", placeholder="85d3153d-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
          help="群机器人 webhook 里 key= 后面那段；直接粘贴整个 webhook 地址也可以"),
)

BY_KEY = {f.key: f for f in FIELDS}
GROUPS = (G_ROUTER, G_DISCOVER, G_ENTRY, G_BEHAVIOR, G_NOTIFY)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

_TRUE = ("1", "true", "yes", "y", "on")
_FALSE = ("0", "false", "no", "n", "off")


def coerce(f: Field, raw):
    """把网页/文件里来的值转成规范形式；不合法抛 SettingsError。"""
    if raw is None:
        raw = ""
    if f.kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE or s == "":
            return False
        raise SettingsError(f"{f.label}：只接受 true/false，收到 {raw!r}")
    if isinstance(raw, (list, dict)):
        raise SettingsError(f"{f.label}：值类型不对")

    s = str(raw).strip()
    if f.kind == "int":
        if s == "":
            raise SettingsError(f"{f.label}：不能为空")
        try:
            v = int(s)
        except ValueError:
            raise SettingsError(f"{f.label}：需要整数，收到 {raw!r}")
        if f.min is not None and v < f.min:
            raise SettingsError(f"{f.label}：不能小于 {f.min}")
        if f.max is not None and v > f.max:
            raise SettingsError(f"{f.label}：不能大于 {f.max}")
        return v
    if f.kind == "float":
        if s == "":
            raise SettingsError(f"{f.label}：不能为空")
        try:
            v = float(s)
        except ValueError:
            raise SettingsError(f"{f.label}：需要数字，收到 {raw!r}")
        if f.min is not None and v < f.min:
            raise SettingsError(f"{f.label}：不能小于 {f.min}")
        if f.max is not None and v > f.max:
            raise SettingsError(f"{f.label}：不能大于 {f.max}")
        return v
    if f.kind == "select":
        if s not in f.choices:
            raise SettingsError(f"{f.label}：只能是 {' / '.join(f.choices)}")
        return s

    # str / secret
    if f.min is not None and len(s) < f.min:
        raise SettingsError(f"{f.label}：太短")
    return s


def validate_overlay(data: dict, strict: bool = True) -> dict:
    """逐个校验并规范化。

    strict=True（网页提交走这条）：未知键或不合法值**直接报错** —— 使用者
      提交上去的设置被默默丢掉，是最坏的行为。
    strict=False（读设置文件走这条）：未知键和不合法项只记日志并跳过，其余
      照常生效。设置文件可能是上一个版本写的，里面留着本版本已经删掉的项
      （例如上一版的取址开关），那绝不能导致整份文件失效 —— 否则用户
      此前填的路由器密码、条目名会一起丢掉。
    """
    if not isinstance(data, dict):
        raise SettingsError("提交的内容不是一个对象")
    out: dict = {}
    unknown = [k for k in data if k not in BY_KEY]
    if unknown and strict:
        raise SettingsError("不认识的设置项：" + ", ".join(sorted(unknown)))
    if unknown:
        log.warning("忽略设置文件里本版本已不存在的项：%s",
                    ", ".join(sorted(unknown)))
    for k, v in data.items():
        f = BY_KEY.get(k)
        if f is None:              # 非严格路径才会走到：旧版本残留项
            continue
        if f.kind == "secret" and (v is None or str(v) == ""):
            continue          # 空密码 = 不改，保持原值
        try:
            cv = coerce(f, v)
            if f.kind == "str" and f.key == "ROUTER_HOST" and not cv:
                raise SettingsError("路由器地址不能为空")
            if f.key == "PORT" and cv not in ("", "-1", None):
                # 端口规格在这里就拦住坏值，不让它流进同步引擎
                for tok in str(cv).replace("，", ",").split(","):
                    tok = tok.strip()
                    if tok in ("", "-1"):
                        continue
                    if not tok.isdigit() or not 1 <= int(tok) <= 65535:
                        raise SettingsError(
                            "放行端口格式不对：多个端口用英文逗号分隔，"
                            "每个 1-65535，或 -1 表示全部端口")
            if f.key in ("NTFY_URL", "GOTIFY_URL") and cv:
                if not str(cv).lower().startswith(("http://", "https://")):
                    name = "ntfy" if f.key == "NTFY_URL" else "gotify"
                    raise SettingsError(
                        f"{name} 服务器地址要以 http:// 或 https:// 开头，"
                        "如 https://ntfy.sh/mytopic")
            if f.key == "WECOM_ID" and cv:
                # 粘贴整个 webhook 地址也行，只留 key= 后面的 ID
                s = str(cv)
                if "key=" in s:
                    cv = s.split("key=", 1)[1].strip().strip("/")
                elif not s.lower().startswith("http"):
                    cv = s.strip().strip("/")
        except SettingsError as e:
            if strict:
                raise
            log.warning("设置文件里的 %s 不合法，已跳过该项：%s", k, e)
            continue
        out[k] = cv
    return out


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------

def settings_path() -> str:
    return os.environ.get("SETTINGS_FILE") or "/data/settings.json"


def load_overlay(path: str | None = None) -> dict:
    """读覆盖层。文件不存在/损坏都返回空 dict —— 配置坏了不该让程序起不来。"""
    path = path or settings_path()
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("设置文件 %s 无法读取（%s），本次忽略它", path, e)
        return {}
    if not isinstance(data, dict):
        log.warning("设置文件 %s 顶层不是对象，忽略", path)
        return {}
    try:
        # 非严格模式：未知键（旧版本残留）与个别坏值只跳过，不让整份文件失效
        return validate_overlay(data, strict=False)
    except SettingsError as e:
        log.warning("设置文件 %s 里有不合法项（%s），忽略整份文件", path, e)
        return {}


def save_overlay(data: dict, path: str | None = None) -> None:
    """原子写 + 0600。内容是已校验过的规范值。"""
    path = path or settings_path()
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log.info("设置已保存到 %s（%d 项）", path, len(data))


# --------------------------------------------------------------------------
# 应用到 Config
# --------------------------------------------------------------------------

_ENV_NAME = {f.attr: f.key for f in FIELDS if f.attr}
_RULE_NAME = {f.rule_attr: f.key for f in FIELDS if f.rule_attr}


def source_of(key: str, overlay: dict) -> str:
    """这个字段当前的值是哪来的：web / env / default。"""
    if key in overlay:
        return "web"
    if os.environ.get(key) not in (None, ""):
        return "env"
    return "default"


def apply_overlay(cfg, overlay: dict) -> None:
    """把覆盖层盖到已按环境变量构造好的 Config 上（覆盖层的优先级更高）。"""
    if not overlay:
        return
    for key, val in overlay.items():
        f = BY_KEY[key]
        if f.secret:
            cfg.password = str(val)
            cfg.password_source = f"网页设置（{settings_path()}）"
        elif f.attr:
            setattr(cfg, f.attr, val)
        elif f.rule_attr and cfg.rules:
            apply_rule_field(cfg, f, val)


def apply_rule_field(cfg, f: Field, val) -> None:
    """条目类字段作用在第一条规则上（多规则来自 RULES JSON 时不会被动到）。"""
    if not cfg.rules:
        return
    r = cfg.rules[0]
    if f.rule_attr == "port":
        # 单端口归一成 int，多端口保留规格字符串 —— 与 Rule.__post_init__ 一致
        s = str(val).strip().replace("，", ",")
        if s in ("", "-1"):
            r.port = None
        elif "," in s:
            r.port = s
        else:
            r.port = int(s)
    else:
        setattr(r, f.rule_attr, val)


def view(cfg, overlay: dict) -> list:
    """给网页用的：按分组列出每个字段的当前值与来源。"""
    out = []
    for g in GROUPS:
        items = []
        for f in FIELDS:
            if f.group != g:
                continue
            if f.rule_attr:
                val = ""
                if cfg.rules:
                    v = getattr(cfg.rules[0], f.rule_attr, "")
                    val = "" if v is None else v
            else:
                val = getattr(cfg, f.attr, f.default)
            if f.kind == "bool":
                val = bool(val)
            items.append(f.to_dict(val, source_of(f.key, overlay)))
        if items:
            out.append({"group": g, "fields": items})
    return out
