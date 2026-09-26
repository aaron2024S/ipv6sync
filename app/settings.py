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
import ipaddress
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
    hidden: bool = False           # 不出现在设置表单里（在别的页面编辑，如 LOG_MAX）

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
    Field("POLL_INTERVAL", "检测周期（秒）", G_ROUTER, "int", "60",
          attr="poll_interval", min=10, max=86400,
          help="多久检查一次地址变化；地址没变不会写路由器，所以调小也不折腾 flash"),
    # 白名单条目不再走这里的表单：网页「IPv6 防火墙白名单」卡里按设备添加
    # （存在 overlay 的 RULES 键里）；ENTRY_NAME / PORT / REMOTE_IP / TARGET_MAC
    # 环境变量仍兼容，读取时自动迁移成 RULES（见 _migrate_legacy_rules）。
    # ---- 运行行为 ----
    Field("ENSURE_FIREWALL_ON", "自动打开 IPv6 防火墙总开关", G_BEHAVIOR, "bool",
          "false", attr="ensure_firewall_on",
          help="总开关关着时白名单不生效，本程序待机不写入；勾选后每轮发现开关"
               "是关的会自动打开再同步（注意：网页上手动关掉的开关，下一轮也会被它重新打开）"),
    Field("VERIFY_AFTER_WRITE", "写完回读核对", G_BEHAVIOR, "bool", "true",
          attr="verify_after_write"),
    Field("DRY_RUN", "演练模式（只打印不写入）", G_BEHAVIOR, "bool", "false",
          attr="dry_run"),
    Field("LOG_LEVEL", "日志级别", G_BEHAVIOR, "select", "INFO",
          attr="log_level", choices=LOG_LEVELS),
    # 同步记录保留条数：设置表单里不露出（hidden），在「同步记录」页的工具行里调，
    # 避免同一项出现在两个页面造成两处真相。
    Field("LOG_MAX", "同步记录保留条数", G_BEHAVIOR, "int", "100",
          attr="events_max", min=10, max=5000, hidden=True),
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
GROUPS = (G_ROUTER, G_BEHAVIOR, G_NOTIFY)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

_TRUE = ("1", "true", "yes", "y", "on")
_FALSE = ("0", "false", "no", "n", "off")

# 白名单自动维护规则（网页「IPv6 防火墙白名单」卡管理）在 overlay 里的键。
# 不进 FIELDS 表单 —— 它是列表结构，由白名单页的弹窗增删改。
RULES_KEY = "RULES"

# 旧版本把单条规则摊在四个扁平键里（网页表单 + 环境变量），已全部下线；
# 读设置文件时自动迁移成 RULES，绝不能让用户此前填的配置悄悄丢失。
_LEGACY_RULE_KEYS = ("ENTRY_NAME", "PORT", "REMOTE_IP", "TARGET_MAC")


def _validate_rules(items, strict: bool = False) -> list:
    """校验 RULES 列表，返回规范化后的规则字典列表。

    strict=True（网页提交）：坏规则整体报错；
    strict=False（读设置文件）：坏项跳过并记日志，其余照常生效。
    """
    if not isinstance(items, list):
        raise SettingsError("RULES 必须是数组")
    out: list = []
    seen: set = set()
    for it in items:
        try:
            if not isinstance(it, dict):
                raise SettingsError("规则项必须是对象")
            name = str(it.get("name") or "").strip()
            if not name:
                raise SettingsError("规则缺少服务名称")
            if len(name) > 48:
                raise SettingsError(f"服务名称 {name!r} 超过 48 个字符")
            if name in seen:
                raise SettingsError(f"服务名称重复：{name}")
            seen.add(name)
            mac = str(it.get("mac") or "").strip()
            port = str(it.get("port", "-1") or "-1").strip().replace("，", ",")
            if port not in ("", "-1"):
                for tok in port.split(","):
                    tok = tok.strip()
                    if not tok.isdigit() or not 1 <= int(tok) <= 65535:
                        raise SettingsError(
                            f"规则 {name} 的放行端口 {tok!r} 不合法"
                            "（每个 1-65535，或 -1 表示全部）")
            remote = str(it.get("remote_ip") or "::/0").strip() or "::/0"
            try:
                ipaddress.ip_network(remote, strict=False)
            except ValueError:
                raise SettingsError(
                    f"规则 {name} 的允许来源 {remote!r} 不是合法网段或地址")
            out.append({"name": name, "mac": mac, "port": port or "-1",
                        "remote_ip": remote})
        except SettingsError as e:
            if strict:
                raise
            log.warning("忽略设置文件里不合法的规则项 %s：%s", it, e)
    return out


def _migrate_legacy_rules(data: dict) -> dict:
    """旧版的 ENTRY_NAME / PORT / REMOTE_IP / TARGET_MAC → RULES（一次性）。"""
    if not isinstance(data, dict) or RULES_KEY in data:
        return data
    if not any(k in data for k in _LEGACY_RULE_KEYS):
        return data
    data = dict(data)
    data[RULES_KEY] = [{
        "name": str(data.get("ENTRY_NAME") or "").strip() or "NAS",
        "mac": str(data.get("TARGET_MAC") or "").strip(),
        "port": str(data.get("PORT") or "-1"),
        "remote_ip": str(data.get("REMOTE_IP") or "::/0"),
    }]
    log.info("检测到旧版条目设置（%s），已迁移为 RULES 规则列表",
             "/".join(k for k in _LEGACY_RULE_KEYS if k in data))
    return data


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
    unknown = [k for k in data if k not in BY_KEY and k != RULES_KEY]
    if unknown and strict:
        raise SettingsError("不认识的设置项：" + ", ".join(sorted(unknown)))
    if unknown:
        log.warning("忽略设置文件里本版本已不存在的项：%s",
                    ", ".join(sorted(unknown)))
    for k, v in data.items():
        if k == RULES_KEY:
            out[RULES_KEY] = _validate_rules(v, strict)
            continue
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
        return validate_overlay(_migrate_legacy_rules(data), strict=False)
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
        if key == RULES_KEY:
            # 网页白名单页维护的规则列表：整表替换（含空表 —— 用户删光规则
            # 就该同步为空，否则环境变量里的默认规则会"复活"）
            from .config import Rule
            cfg.rules = [Rule(name=r["name"], mac=r["mac"] or None,
                              port=None if r["port"] in ("", "-1") else r["port"],
                              remote_ip=r["remote_ip"]) for r in (val or [])]
            continue
        f = BY_KEY.get(key)
        if f is None:
            continue          # RULES 之外的未知键理论上已被校验层挡掉，兜底跳过
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
            if f.group != g or f.hidden:
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
