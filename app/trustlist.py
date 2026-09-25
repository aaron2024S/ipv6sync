# -*- coding: utf-8 -*-
"""
IPv6 防火墙白名单（信任列表）读写
  接口：/api/ntwk/ip6firewall_trustlist   —— 最多 32 条
        /api/ntwk/ip6firewall_enable     —— IPv6 防火墙总开关

报文格式来自设备前端 chunk（`postdata()`）：
    create : {"Name", "RemoteIp", "LocalIp", "Port", "ID": ""}
    update : 同上，ID 为既有条目
    delete : {"Name", "Remoteip", "Localip", "Port", "ID"}   ← IP 键名小写（固件原样）

字段语义：
    Name      规则名（本程序的稳定主键：按名字 upsert，不会越加越多）
    LocalIp   内网设备 IPv6（要放通的那台机器）= 本程序动态维护的目标
    RemoteIp  允许来源，默认 "::/0" 即不限制
    Port      1-65535；-1 表示该地址全部端口
"""
from __future__ import annotations

import logging

from . import discover
from .router import RouterError

log = logging.getLogger("ipv6sync.trustlist")

MAX_ENTRIES = 32
ALL_PORTS = -1


def list_entries(r) -> list:
    return r.get("ip6firewall_trustlist") or []


def find_by_name(r, name: str) -> dict | None:
    for e in list_entries(r):
        if e.get("Name") == name:
            return e
    return None


def find_by_id(r, entry_id: str) -> dict | None:
    for e in list_entries(r):
        if e.get("ID") == entry_id:
            return e
    return None


def is_enabled(r) -> bool:
    return bool(r.get("ip6firewall_enable").get("Enable"))


def set_enabled(r, on: bool) -> dict:
    return r.post("ip6firewall_enable", {"Enable": bool(on)})


def _norm_port(port) -> int:
    if port in (None, "", "all", "ALL", "any"):
        return ALL_PORTS
    return int(port)


def parse_ports(spec) -> list[int]:
    """把「放行端口」规格解析成端口列表。

    接受：None / "" / "-1" → [-1]（全部端口）；
          单个端口 16667 → [16667]；
          逗号分隔 "16667,5005,22"（中英文逗号都可以）→ [16667, 5005, 22]。
    非法值抛 ValueError，消息直接面向使用者。
    """
    if spec is None:
        return [ALL_PORTS]
    if isinstance(spec, int):
        if not (spec == ALL_PORTS or 1 <= spec <= 65535):
            raise ValueError(f"端口 {spec} 超出范围（1-65535，或 -1 表示全部）")
        return [spec]
    tokens = [t.strip() for t in str(spec).replace("，", ",").split(",") if t.strip()]
    if not tokens:
        return [ALL_PORTS]
    out: list[int] = []
    for t in tokens:
        if t in ("-1", "-", "all", "ALL", "any"):
            out.append(ALL_PORTS)
            continue
        if not t.isdigit():
            raise ValueError(f"放行端口 {t!r} 不是数字（多个端口用英文逗号分隔，"
                             "每个 1-65535，或 -1 表示全部）")
        p = int(t)
        if not 1 <= p <= 65535:
            raise ValueError(f"放行端口 {p} 超出范围（1-65535，或 -1 表示全部）")
        out.append(p)
    # 去重保序
    seen: set[int] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def entry_names(base: str, ports: list[int],
                addr_index: int = 0) -> list[tuple[str, int]]:
    """一组端口在「第 addr_index 个地址」上的白名单条目名。

    命名规则（两个维度：端口 × 地址）：
      addr_index=0：ports[0] 用基础名，其余为 基础名-端口
                    → NAS、NAS-5005        （与单地址时代完全兼容）
      addr_index>0：整体加 @N（N 从 2 开始）
                    → NAS@2、NAS@2-5005

    为什么第 0 个地址必须沿用基础名：旧版本只维护 `NAS` 一条，升级后要能
    原地 update 那条，而不是新增一条 `NAS@1` 把名额白占掉。
    """
    prefix = base if addr_index == 0 else f"{base}@{addr_index + 1}"
    if len(ports) == 1:
        return [(prefix, ports[0])]
    return [(prefix, ports[0])] + [(f"{prefix}-{p}", p) for p in ports[1:]]


def managed_pattern(base: str):
    """本程序生成的条目名：基础名、基础名@N，各自可再带 -端口。"""
    import re
    return re.compile(re.escape(base) + r"(@\d+)?(-\d+)?$")


def managed_names_of(entries, base: str) -> list[str]:
    """从一份已有条目列表里挑出「名字看起来是本程序生成的」那些。"""
    pat = managed_pattern(base)
    return [e.get("Name") or "" for e in (entries or [])
            if e.get("Name") and pat.match(e["Name"])]


def managed_names(r, base: str) -> list[str]:
    """当前白名单里「名字看起来是本程序生成的」条目名。"""
    return managed_names_of(list_entries(r), base)


def remove_stale(r, base: str, keep: set) -> list[str]:
    """删掉本程序管理、但已不在 keep 里的条目（端口缩容 / 地址减少时收尾）。

    只认 managed_pattern 匹配的名字，用户自己起的名字（如「别的设备-22」）
    绝不碰。返回被删除的名字列表。
    """
    deleted: list[str] = []
    for e in list_entries(r):
        n = e.get("Name") or ""
        if not n or n in keep:
            continue
        if managed_pattern(base).match(n):
            if ok(delete(r, e)):
                deleted.append(n)
            else:
                log.warning("删除过期条目 %s 失败", n)
    return deleted


def cleanup_stale(r, base: str, ports: list[int]) -> list[str]:
    """兼容旧签名：按「端口维度」清理（单地址场景）。"""
    keep = {nm for nm, _p in entry_names(base, ports)}
    return remove_stale(r, base, keep)


def add(r, name: str, local_ip: str, port=None, remote_ip: str = "::/0") -> dict:
    data = {"Name": name,
            "RemoteIp": remote_ip or "::/0",
            "LocalIp": local_ip,
            "Port": _norm_port(port),
            "ID": ""}
    return r.post("ip6firewall_trustlist", data, action="create")


def update(r, entry_id: str, name: str, local_ip: str,
           port=None, remote_ip: str = "::/0") -> dict:
    data = {"Name": name,
            "RemoteIp": remote_ip or "::/0",
            "LocalIp": local_ip,
            "Port": _norm_port(port),
            "ID": entry_id}
    return r.post("ip6firewall_trustlist", data, action="update")


def delete(r, entry: dict) -> dict:
    """按列表里的元素删除。固件 delete 分支用的键名是小写 ip，先按原样发。"""
    res = r.post("ip6firewall_trustlist", {
        "Name": entry.get("Name"),
        "Remoteip": entry.get("RemoteIp"),
        "Localip": entry.get("LocalIp"),
        "Port": entry.get("Port"),
        "ID": entry.get("ID"),
    }, action="delete")
    if isinstance(res, dict) and res.get("errcode"):
        log.warning("delete 用固件原样键名失败（errcode=%s），回退成驼峰再试",
                    res.get("errcode"))
        res = r.post("ip6firewall_trustlist", {
            "Name": entry.get("Name"),
            "RemoteIp": entry.get("RemoteIp"),
            "LocalIp": entry.get("LocalIp"),
            "Port": entry.get("Port"),
            "ID": entry.get("ID"),
        }, action="delete")
    return res


def same_port(a, b) -> bool:
    return _norm_port(a) == _norm_port(b)


# 兼容旧名（模块内部历史上叫 _same_port）
_same_port = same_port


def upsert(r, name: str, local_ip: str, port=None,
           remote_ip: str = "::/0") -> tuple[str, dict | None]:
    """按名字「存在则改、不存在则加」，返回 (动作, 结果对象)。

    这是动态维护白名单最安全的语义：地址一变就原地更新同一条规则，
    不会把 32 个位置越占越满，也不会在列表里留下过期规则。
    """
    remote_ip = remote_ip or "::/0"
    old = find_by_name(r, name)

    if old:
        # 地址按规范化结果比（discover.same_addr）：固件回读的写法可能与
        # 我们传入的不同，但地址其实没变 —— 这种情况绝不能写，白写就是
        # 无谓的 flash 损耗。
        if (discover.same_addr(old.get("LocalIp"), local_ip)
                and (old.get("RemoteIp") or "::/0") == remote_ip
                and _same_port(old.get("Port"), port)):
            return "unchanged", old
        res = update(r, old["ID"], name, local_ip, port, remote_ip)
        return "updated", res

    entries = list_entries(r)
    if len(entries) >= MAX_ENTRIES:
        raise RouterError(f"白名单已满（{MAX_ENTRIES} 条），无法新增")
    res = add(r, name, local_ip, port, remote_ip)
    return "created", res


def ok(res) -> bool:
    """判断设备的写操作是否成功。

    设备成功时返回 {} 或 {"errcode": 0}；失败带回非 0 errcode。
    """
    if res is None:
        return True
    if isinstance(res, dict):
        if res.get("errcode") in (None, 0, "0"):
            return True
        return False
    return True
