#!/usr/bin/env python3
"""离线拉取 python:3.12-alpine 基础镜像层（registry v2 API，匿名 Bearer 鉴权）。

本机没有 Docker 引擎，无法 docker pull / docker build。改为直接走 registry HTTP API：

  manifest index -> 选 linux/<arch> 子 manifest -> config blob + 各层 blob

逐层做**双重校验**：
  sha256(gzip blob)        == manifest.layers[i].digest
  sha256(解压后的明文 tar) == config.rootfs.diff_ids[i]

产物缓存到 offline/cache/<arch>/，之后重建镜像全程离线（不需要网络）。

用法：
  python fetch_base.py --arch all               # amd64 + arm64
  python fetch_base.py --arch amd64
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_ROOT = os.path.join(HERE, "cache")

# 上游 Dockerfile: FROM python:3.12-alpine
REPO = "library/python"
DEFAULT_TAG = "3.12-alpine"

# registry v2 镜像站，按可用性排序，第一个成功的就用。
# dockerproxy.net 放最后 —— 它不要求鉴权，但按 digest 取子 manifest 会 500。
REGISTRY_MIRRORS = [
    "https://docker.m.daocloud.io",
    "https://hub.rat.dev",
    "https://docker.1ms.run",
    "https://dockerproxy.net",
]

# Docker 平台名 -> OCI 架构名
ARCH_MAP = {"amd64": "amd64", "arm64": "arm64"}

ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])
UA = {"User-Agent": "docker/25.0.5 offline-builder"}


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------- 传输
def raw_get(url, headers, timeout=60, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def registry_get(mirror, path, accept, timeout=60):
    """registry v2 匿名拉取，自动处理 Bearer 鉴权。

    registry 的标准流程：首次请求返回 401，并在 WWW-Authenticate 头里给出
    realm/service/scope；客户端拿这些换一个匿名 token，再带上重放原请求。
    """
    url = mirror + path
    h = {"Accept": accept, **UA}
    try:
        return raw_get(url, h, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        www = e.headers.get("WWW-Authenticate", "") or ""
        m = re.search(r'realm="([^"]+)"', www)
        if not m:
            raise RuntimeError(f"401 但未提供 realm: {www[:150]}")
        q = []
        for key in ("service", "scope"):
            mm = re.search(rf'{key}="([^"]+)"', www)
            if mm:
                q.append(f"{key}={mm.group(1)}")
        tok_raw = raw_get(m.group(1) + ("?" + "&".join(q) if q else ""), UA)
        tok = json.loads(tok_raw)
        token = tok.get("token") or tok.get("access_token")
        if not token:
            raise RuntimeError("token 响应缺少 token 字段")
        return raw_get(url, {**h, "Authorization": f"Bearer {token}"}, timeout)


def try_registry(path, timeout=60):
    """依次尝试各镜像站，返回 (bytes, 命中镜像)。"""
    errs = []
    for m in REGISTRY_MIRRORS:
        try:
            return registry_get(m, path, ACCEPT, timeout), m
        except Exception as e:  # noqa: BLE001
            errs.append(f"{m}: {type(e).__name__}: {e}")
    raise RuntimeError("所有镜像站均失败:\n  " + "\n  ".join(errs))


def decompress_layer(blob, hint=""):
    """层 blob -> 明文 tar 字节。支持 gzip / zstd / 未压缩。"""
    if blob[:2] == b"\x1f\x8b":
        return gzip.decompress(blob)
    if blob[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard  # noqa: PLC0415
        except ImportError:
            print(f"FATAL: 层 {hint} 是 zstd 压缩，需先安装 zstandard")
            sys.exit(1)
        return zstandard.ZstdDecompressor().decompress(blob, max_output_size=1 << 31)
    if len(blob) > 262 and blob[257:262] == b"ustar":
        return blob
    print(f"FATAL: 层 {hint} 压缩格式无法识别: {blob[:8].hex()}")
    sys.exit(1)


# ----------------------------------------------------------- 基础镜像
def fetch_base(arch, tag, cache):
    os.makedirs(cache, exist_ok=True)
    oci_arch = ARCH_MAP[arch]

    print(f"[1/5] 拉取 {REPO}:{tag} 的 manifest index ...")
    index_raw, used = try_registry(f"/v2/{REPO}/manifests/{tag}")
    index = json.loads(index_raw)
    print(f"      来自 {used}")

    sub_digest = None
    if "manifests" in index:
        for e in index["manifests"]:
            p = e.get("platform") or {}
            if p.get("os") == "linux" and p.get("architecture") == oci_arch:
                sub_digest = e["digest"]
                break
        if not sub_digest:
            print(f"FATAL: index 中没有 linux/{oci_arch} 条目")
            sys.exit(1)
        print(f"      linux/{oci_arch} -> {sub_digest[:24]}...")
    else:
        print("      已是单架构 manifest")

    print(f"[2/5] 取 linux/{oci_arch} manifest ...")
    if sub_digest:
        raw, _ = try_registry(f"/v2/{REPO}/manifests/{sub_digest}")
        got = "sha256:" + sha256_bytes(raw)
        if got != sub_digest:
            print(f"FATAL: manifest digest 不匹配\n  got    {got}\n  expect {sub_digest}")
            sys.exit(1)
        manifest = json.loads(raw)
    else:
        manifest = index

    print("[3/5] 取 config blob ...")
    cfg_digest = manifest["config"]["digest"]
    cfg_raw, _ = try_registry(f"/v2/{REPO}/blobs/{cfg_digest}", timeout=180)
    if sha256_bytes(cfg_raw) != cfg_digest.split(":", 1)[1]:
        print("FATAL: config blob sha256 不匹配")
        sys.exit(1)
    cfg = json.loads(cfg_raw)
    diff_ids = cfg["rootfs"]["diff_ids"]
    layers = manifest["layers"]
    if len(layers) != len(diff_ids):
        print(f"FATAL: 层数 {len(layers)} 与 diff_ids {len(diff_ids)} 不一致")
        sys.exit(1)
    print(f"      config OK，{len(layers)} 层")
    with open(os.path.join(cache, "config.json"), "wb") as f:
        f.write(cfg_raw)

    print(f"[4/5] 下载并双重校验 {len(layers)} 个层 ...")
    for i, lay in enumerate(layers):
        dg = lay["digest"]
        blob_path = os.path.join(cache, f"blob-{i}.gz")
        if os.path.exists(blob_path) and sha256_file(blob_path) == dg.split(":", 1)[1]:
            raw = open(blob_path, "rb").read()
            print(f"      层 {i}: 命中缓存")
        else:
            print(f"      层 {i}: 下载中 ({lay.get('size', 0)/1e6:.1f} MB) ...")
            raw, _ = try_registry(f"/v2/{REPO}/blobs/{dg}", timeout=1800)
            if sha256_bytes(raw) != dg.split(":", 1)[1]:
                print(f"FATAL: 层 {i} blob sha256 与 manifest digest 不匹配")
                sys.exit(1)
            with open(blob_path, "wb") as f:
                f.write(raw)

        plain = decompress_layer(raw, hint=str(i))
        expect = diff_ids[i].split(":", 1)[1]
        if sha256_bytes(plain) != expect:
            print(f"FATAL: 层 {i} 明文 sha256 与 diff_id 不匹配")
            sys.exit(1)
        with open(os.path.join(cache, f"layer-{i}.tar"), "wb") as f:
            f.write(plain)
        print(f"      层 {i}: {len(plain)/1e6:.1f} MB，双重校验通过")

    print("[5/5] 读取基础镜像元信息 ...")
    info = read_base_info(cache, len(layers))
    if not info["alpine_release"]:
        print("FATAL: 基础层里找不到 /etc/alpine-release")
        sys.exit(1)
    if not info["has_python"]:
        print("FATAL: 基础层里找不到 python 解释器（镜像可能不是 python:alpine）")
        sys.exit(1)
    print(f"      Alpine {info['alpine_release']}")
    print(f"      python: {'/usr/local/bin/python3' if info['has_python'] else '缺失'}")

    meta = {
        "arch": arch,
        "oci_arch": oci_arch,
        "repo": REPO,
        "tag": tag,
        "image_digest": sub_digest or cfg_digest,
        "config_digest": cfg_digest,
        "layers": len(layers),
        "diff_ids": diff_ids,
        "layer_digests": [l["digest"] for l in layers],
        "registry_mirror": used,
        "alpine_release": info["alpine_release"],
        "python_version": info["python_version"],
        "has_python": info["has_python"],
        "base_etc": info["base_etc"],
        "layer_plain_bytes": info["layer_plain_bytes"],
    }
    with open(os.path.join(cache, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    return meta


def read_base_info(cache, n_layers):
    """按层序扫描基础层，取末次出现的 /etc/passwd|group|shadow 与 python 版本。"""
    wanted = {"etc/passwd", "etc/group", "etc/shadow", "etc/alpine-release"}
    found = {}
    has_python = False
    pyver = None
    sizes = []
    for i in range(n_layers):
        p = os.path.join(cache, f"layer-{i}.tar")
        sizes.append(os.path.getsize(p))
        with tarfile.open(p, "r:") as t:
            for m in t.getmembers():
                nm = m.name.lstrip("./")
                if nm in wanted and m.isfile():
                    found[nm] = t.extractfile(m).read()
                elif nm.endswith("/bin/python3") or nm.endswith("/bin/python"):
                    # 基础镜像里 python 与 python3 是符号链接，指向 python3.12
                    has_python = True
                    if m.issym():
                        pyver = m.linkname.rsplit("python", 1)[-1] or pyver
    etc = {}
    for k in ("etc/passwd", "etc/group", "etc/shadow"):
        if k in found:
            etc[os.path.basename(k)] = found[k].decode("utf-8", "replace")
    return {
        "alpine_release": found.get("etc/alpine-release", b"").decode().strip(),
        "python_version": pyver,
        "has_python": has_python,
        "base_etc": etc,
        "layer_plain_bytes": sizes,
    }


# --------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="离线拉取 python:3.12-alpine 基础镜像层")
    ap.add_argument("--arch", default="amd64",
                    help="amd64 / arm64 / all（逗号分隔也可以）；默认只拉 amd64")
    ap.add_argument("--tag", default=DEFAULT_TAG, help=f"基础镜像 tag（默认 {DEFAULT_TAG}）")
    args = ap.parse_args()

    arches = sorted(ARCH_MAP) if args.arch == "all" else \
        [a.strip() for a in args.arch.split(",") if a.strip()]
    for a in arches:
        if a not in ARCH_MAP:
            print(f"FATAL: 不支持的架构 {a!r}")
            sys.exit(1)

    done = []
    for a in arches:
        print(f"\n{'='*66}\n==== 拉取 {REPO}:{args.tag}  linux/{a}\n{'='*66}")
        cache = os.path.join(CACHE_ROOT, a)
        meta = fetch_base(a, args.tag, cache)
        done.append(meta)
        print(f"      -> {cache}")

    print(f"\n{'='*66}\n==== 完成 ====")
    for m in done:
        tot = sum(m["layer_plain_bytes"]) / 1e6
        print(f"{m['arch']:<6}: Alpine {m['alpine_release']}，{m['layers']} 层，"
              f"明文合计 {tot:.1f} MB，python={m['has_python']}")
    print("\n下一步: python build_offline.py --arch " + arches[0]
          if len(arches) == 1 else "\n下一步: python build_offline.py --arch all")


if __name__ == "__main__":
    main()
