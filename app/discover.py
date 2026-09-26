# -*- coding: utf-8 -*-
"""
本机网卡 IPv6 地址发现 —— 本程序唯一一条取址路径

读 `/proc/net/if_inet6` 取本机的全局 IPv6。容器用 host 网络时「本机」就是
NAS 自己，那上面的地址才是外部报文真正能落地的地方；路由器设备表（HostInfo）
**不参与取址** —— 它本质是邻居表缓存，更新有延迟，还会留着设备已经不用了的
旧地址。

文件末尾那两个读 HostInfo 的工具函数（host_candidates / lan_prefixes）
只服务于控制台的「设备列表」页，用来查 MAC，不参与取址。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re

log = logging.getLogger("ipv6sync.discover")

PROC_NET_IF_INET6 = "/proc/net/if_inet6"

# /proc/net/if_inet6 第 5 列是 flags（十六进制，只打低 8 位）。
# 位含义取自 Linux 的 include/uapi/linux/if_addr.h：
#   0x01 IFA_F_TEMPORARY（与 IFA_F_SECONDARY 同值）—— RFC 4941 隐私临时地址
#   0x20 IFA_F_DEPRECATED —— 已过首选期，只该被当作"旧地址"
#   0x40 IFA_F_TENTATIVE  —— DAD 还没做完，此刻不能用
#   0x80 IFA_F_PERMANENT
IFA_F_TEMPORARY = 0x01
IFA_F_DEPRECATED = 0x20
IFA_F_TENTATIVE = 0x40

# 拿不到 flags 时不会误判成"不健康"，因此这几个常量只用于"降级排序/过滤"，
# 不用来决定有没有地址可用。
_HEALTH_MASK = IFA_F_TENTATIVE | IFA_F_DEPRECATED


# --------------------------------------------------------------------------
# 规范化与过滤
# --------------------------------------------------------------------------

def canon(addr: str) -> str | None:
    """转成压缩小写形式；非法地址返回 None。"""
    if not addr:
        return None
    a = str(addr).strip().strip("[]")
    a = a.split("%")[0]                      # 去掉 zone id（fe80::1%eth0）
    a = re.sub(r"/\d+$", "", a)              # 去掉前缀长度
    try:
        return ipaddress.IPv6Address(a).compressed
    except ValueError:
        return None


def same_addr(a, b) -> bool:
    """两个地址是否「其实是同一个」—— 规范化之后再比。

    为什么要单独立一个函数：白名单回读出来的写法未必和 HostInfo 给的
    一致。固件可能把地址存成大写、补齐前导零或不做 `::` 压缩，这时直接比
    字符串会把「写法不同」误判成「地址变了」，于是每一轮轮询都白写一次
    白名单 —— 而路由器配置写入是 flash 操作，长期反复写毫无意义还有损耗。

    任一边解析不出来（空值 / 非法值）时退化成「去空白的原文比较」，
    避免把「空的 LocalIp」错判成与任意地址相同。
    """
    ca, cb = canon(a), canon(b)
    if ca is None or cb is None:
        return str(a or "").strip() == str(b or "").strip()
    return ca == cb


def is_usable(addr: str) -> bool:
    """是否是「可以用在防火墙白名单里」的地址。

    排除回环、未指定、组播、链路本地（fe80::/10）、IPv4 映射，以及
    ULA（fc00::/7）—— 家里能放通到公网的都是运营商下发的全局地址。
    """
    c = canon(addr)
    if not c:
        return False
    a = ipaddress.IPv6Address(c)
    if a.is_loopback or a.is_unspecified or a.is_multicast or a.is_link_local:
        return False
    if a.ipv4_mapped is not None:
        return False
    if a.is_private:
        return False
    return True


def norm_mac(mac: str) -> str:
    if not mac:
        return ""
    return re.sub(r"[^0-9a-f]", "", mac.lower())


# --------------------------------------------------------------------------
# 本机网卡 —— 唯一取址来源
# --------------------------------------------------------------------------

def read_local_addrs() -> list[tuple[str, int]]:
    """读本机（宿主机）的全局 IPv6 地址，带 flags。仅 Linux，需 host 网络。

    返回 [(压缩小写地址, flags)]，flags 见文件顶部常量。

    丢掉 DAD 未完成的（tentative）和已过首选期的（deprecated）—— 这两种
    地址此刻都不该写进白名单：tentative 的还没生效，deprecated 的是设备
    马上就要丢弃的旧地址，写进去要么不通、要么很快变成垃圾条目。
    """
    out: list[tuple[str, int]] = []
    if os.path.exists(PROC_NET_IF_INET6):
        try:
            with open(PROC_NET_IF_INET6, encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 6:
                        continue
                    raw_addr, _idx, _plen, scope, raw_flags, _dev = parts[:6]
                    if scope != "00":            # 00 = global
                        continue
                    try:
                        ip = ipaddress.IPv6Address(int(raw_addr, 16))
                    except ValueError:
                        continue
                    if not is_usable(str(ip)):
                        continue
                    try:
                        flags = int(raw_flags, 16)
                    except ValueError:
                        flags = 0
                    if flags & _HEALTH_MASK:
                        log.info("本机地址 %s 带 flags=0x%02x（tentative/deprecated），"
                                 "不予采用", ip.compressed, flags)
                        continue
                    out.append((ip.compressed, flags))
        except OSError as e:
            log.warning("读 %s 失败: %s", PROC_NET_IF_INET6, e)
    else:
        log.debug("%s 不存在（非 Linux 或未挂载），跳过本机地址发现",
                  PROC_NET_IF_INET6)
    seen, res = set(), []
    for a, fl in out:
        if a not in seen:
            seen.add(a)
            res.append((a, fl))
    return res


def local_candidates() -> list[str]:
    """本机全局地址列表（只要地址，不要 flags）。"""
    return [a for a, _fl in read_local_addrs()]


def temporary_set() -> set[str]:
    """本机那些「看起来是隐私临时地址」的地址集合。

    只用于**排序**（稳定地址优先写），不做硬过滤：这个标志位的可靠性
    依赖内核版本，万一判错也只是写了条多余的条目，不会少写。
    """
    return {a for a, fl in read_local_addrs()
            if fl & IFA_F_TEMPORARY}


# --------------------------------------------------------------------------
# 本机网卡信息
# --------------------------------------------------------------------------

SYS_CLASS_NET = "/sys/class/net"


def local_macs() -> set[str]:
    """本机网卡的 MAC 集合（规范化成 12 位小写十六进制）。仅 Linux。

    取不到（非 Linux / 没挂 /sys）时返回空集合 —— 调用方必须把空集合理解为
    "不知道"，而不是"本机没有网卡"，否则会退化成"永远不是本机"。
    """
    out: set[str] = set()
    try:
        names = os.listdir(SYS_CLASS_NET)
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(SYS_CLASS_NET, n, "address"),
                      encoding="utf-8") as f:
                mac = norm_mac(f.read())
        except OSError:
            continue
        if mac and mac != "000000000000":
            out.add(mac)
    return out


# --------------------------------------------------------------------------
# 路由器 HostInfo（只给「设备列表」页查 MAC 用，不参与取址）
# --------------------------------------------------------------------------

def host_candidates(host: dict) -> list[str]:
    """把 HostInfo 里一台设备的全部 IPv6 地址抽出来（含 Ipv6Addrs 数组）。

    注意这里是**列表**：一台 Linux 设备常常同时有 SLAAC 稳定地址、RFC 4941
    隐私临时地址、DHCPv6 分配地址，前缀一样、只有后 64 位不同。固件给的顺序
    不代表优先级，所以调用方不要只用第一条。

    返回值统一规范化（压缩小写），否则固件换个写法就会在下游被当成"新地址"。
    """
    cands: list[str] = []
    for item in host.get("Ipv6Addrs") or []:
        if isinstance(item, dict):
            cands.append(item.get("Ipv6Addr") or "")
        elif isinstance(item, str):
            cands.append(item)
    # 单数字段 IPv6Address 实测常是链路本地（fe80::），is_usable 会把它滤掉；
    # 万一固件在这里放了全局地址，也没有理由不用它，所以照样收进来。
    cands.append(host.get("IPv6Address") or "")
    out: list[str] = []
    for c in cands:
        if not is_usable(c):
            continue
        cc = canon(c)
        if cc and cc not in out:
            out.append(cc)
    return out


def lan_prefixes(router, plen: int = 64) -> list[str]:
    """收集路由器当前下发的 LAN 全局前缀（从各在线主机的地址推导）。"""
    prefixes = set()
    for h in router.get("HostInfo") or []:
        for a in host_candidates(h):
            try:
                net = ipaddress.IPv6Network(f"{a}/{plen}", strict=False)
                prefixes.add(str(net))
            except ValueError:
                pass
    return sorted(prefixes)


# --------------------------------------------------------------------------
# 写入顺序
# --------------------------------------------------------------------------

def rank_candidates(cands: list[str],
                    temp_suspect: set[str] | None = None) -> list[str]:
    """把候选地址排成「建议写入顺序」，**不丢弃任何一个**。

    只有一条规则：本机标记为隐私临时地址（RFC 4941）的往后排。排序只决定
    「谁占基础条目名」（如 NAS）—— 稳定地址（SLAAC / DHCPv6）更耐用，让它占
    基础名，临时地址轮换时就不会把 NAS 这条条目也带着改写一遍。

    地址一个都不会少写：临时地址同样要写，报文完全可能落在它上面。
    """
    ts = temp_suspect or set()
    if not cands:
        return []
    return sorted(cands, key=lambda a: 1 if a in ts else 0)   # 稳定排序
