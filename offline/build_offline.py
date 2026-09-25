#!/usr/bin/env python3
"""离线组装 ipv6sync 的 docker-save 格式镜像 tar。

本机没有 Docker 引擎，无法 docker build，因此按 Dockerfile 的语义**手工拼装**归档。
产出物可被目标 NAS 的 `docker load -i` 直接导入（不需要外网、不需要源码）。

对应 Dockerfile 的每一句：
    FROM python:3.12-alpine        -> 复用 cache/<arch>/ 里已拉取并双重校验的基础层
    ENV ...                        -> 合并进 config.Env（同名后写覆盖）
    WORKDIR /app                   -> config.WorkingDir
    RUN addgroup/adduser 10001     -> 改写 /etc/group、/etc/passwd、/etc/shadow
    RUN mkdir -p /data && chown    -> 层内加 data/ 目录条目（uid 10001）
    COPY app/ /app/app/            -> 层内加 app/app/*.py
    RUN chown -R syncapp:syncapp   -> /app 树的 uid/gid 直接写成 10001
    USER syncapp                   -> config.User
    VOLUME ["/data"]               -> config.Volumes
    EXPOSE 8099 6600               -> config.ExposedPorts
    LABEL ...                      -> config.Labels
    HEALTHCHECK ...                -> config.Healthcheck
    ENTRYPOINT [...]               -> config.Entrypoint，并清空基础镜像的 Cmd

用法：
  python build_offline.py --arch all
  set SOURCE_DATE_EPOCH=... & python build_offline.py --arch amd64   # 可复现构建
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)          # ipv6sync/
CACHE_ROOT = os.path.join(HERE, "cache")
DIST_DIR = os.path.join(HERE, "dist")

IMAGE_NAME = "ipv6sync"
IMAGE_TAG = "latest"


def _read_version(fallback: str = "1.0.5") -> str:
    """版本号的唯一来源是 app/__init__.py。

    以前这里和两个 validate_*.py 各硬编码一份，发版时要改 4 处、极易漏改，
    导致「镜像标签是新的、校验脚本按旧版本找产物」这种错配。
    """
    try:
        with open(os.path.join(PROJECT, "app", "__init__.py"),
                  encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return fallback
    m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else fallback


IMAGE_VERSION = _read_version()

# 非 root 运行：与 Dockerfile 的 addgroup/adduser 参数一致
APP_UID = 10001
APP_GID = 10001
APP_USER = "syncapp"
APP_GROUP = "syncapp"
APP_HOME = "/home/syncapp"               # -H：只写进 passwd，不创建目录
APP_SHELL = "/sbin/nologin"

# Dockerfile 的 ENV（顺序照抄）
APP_ENV = [
    ("PYTHONUNBUFFERED", "1"),
    ("PYTHONDONTWRITEBYTECODE", "1"),
    ("PYTHONFAULTHANDLER", "1"),
    ("PYTHONIOENCODING", "utf-8"),
    ("LANG", "C.UTF-8"),
    ("TZ", "Asia/Shanghai"),
    ("SESSION_FILE", "/data/session.json"),
    ("SETTINGS_FILE", "/data/settings.json"),
    ("HEALTH_PORT", "8099"),
    ("PORT", "6600"),
]

APP_LABELS = {
    "org.opencontainers.image.title": "huawei-ipv6-trustlist-sync",
    "org.opencontainers.image.description":
        "发现 NAS 自身 IPv6 变化并同步到华为路由器 IPv6 防火墙白名单",
    "org.opencontainers.image.version": IMAGE_VERSION,
}

# HEALTHCHECK 的 shell 形式命令行（Dockerfile 里 `\` 续行，解析后拼成一行）
HEALTH_CMD = (
    'python -c "import os,sys,urllib.request; '
    "p=os.environ.get('HEALTH_PORT','8099'); "
    "sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', "
    'timeout=4).status==200 else 1)"'
)

ARCHES = ["amd64", "arm64"]


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm(name):
    """层内路径归一化，用于同层去重判定。"""
    n = name[2:] if name.startswith("./") else name
    n = n.lstrip("/")
    while n.endswith("/"):
        n = n[:-1]
    return n


class LayerBuilder:
    """写单个层 tar，并在同层内做路径去重。

    docker load 的解包器对**同一个 layer.tar 里重复出现的路径**会直接拒绝
    （duplicates of file paths not supported）。基础系统包之间天然重叠
    （usr/、usr/share/ 之类），所以写入必须统一走 emit()。
    去重只在层内做 —— 跨层路径重叠是正常的（上层覆盖下层），不能跨层去重。
    """

    def __init__(self, path, mtime):
        self.path = path
        self.mtime = mtime
        self.seen = set()
        self.names = []          # 原始条目名，供"无绝对路径"卫生检查
        self.files = 0
        self.dirs = 0
        self.tf = None

    def __enter__(self):
        self.tf = tarfile.open(self.path, "w", format=tarfile.GNU_FORMAT)
        return self

    def __exit__(self, *exc):
        self.tf.close()
        return False

    def emit(self, name, kind, mode, uid=0, gid=0, payload=None, linkname=""):
        key = norm(name)
        if not key or key in self.seen:
            return False
        self.seen.add(key)
        self.names.append(name)

        ti = tarfile.TarInfo(name)
        ti.mtime = self.mtime
        ti.mode = mode
        ti.uid, ti.gid = uid, gid
        ti.uname = ti.gname = ""          # 空 uname，靠数字 uid 表达（与 docker 一致）
        if kind == "dir":
            ti.type = tarfile.DIRTYPE
            self.tf.addfile(ti)
            self.dirs += 1
        elif kind == "sym":
            ti.type = tarfile.SYMTYPE
            ti.linkname = linkname
            self.tf.addfile(ti)
        else:
            ti.type = tarfile.REGTYPE
            ti.size = len(payload)
            self.tf.addfile(ti, io.BytesIO(payload))
            self.files += 1
        return True

    def emit_blob(self, name, data, mode=0o644, uid=0, gid=0):
        return self.emit(name, "file", mode, uid, gid, payload=data)


# ------------------------------------------------- 基础层的 /etc 现状
def base_etc_file(cache, n_layers, relpath):
    """取基础镜像里某个 /etc 文件的**最终形态**（按层序取最后一次出现）。

    返回 (bytes, mode)。找不到返回 (None, None)。
    """
    content, mode = None, None
    target = relpath.lstrip("/")
    for i in range(n_layers):
        p = os.path.join(cache, f"layer-{i}.tar")
        with tarfile.open(p, "r:") as t:
            for m in t.getmembers():
                nm = m.name.lstrip("./")
                if nm == target and m.isfile():
                    content = t.extractfile(m).read()
                    mode = m.mode
    return content, mode


def with_line(blob, line, what):
    """在文件末尾追加一行（保持结尾换行）。已存在同名条目则原样返回。"""
    text = blob.decode("utf-8")
    key = line.split(":", 1)[0]
    for ln in text.splitlines():
        if ln.split(":", 1)[0] == key:
            print(f"      {what}: {key} 已存在，跳过")
            return text.encode("utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    return (text + line + "\n").encode("utf-8")


def build_app_layer(cache, meta, out_path, fixed_mtime):
    """组装应用层，返回 diff_id（'sha256:...'）。"""
    n = meta["layers"]
    src_app = os.path.join(PROJECT, "app")
    if not os.path.isdir(src_app):
        print(f"FATAL: 缺少应用源码目录 {src_app}")
        sys.exit(1)
    py_files = sorted(f for f in os.listdir(src_app)
                      if f.endswith(".py") and os.path.isfile(os.path.join(src_app, f)))
    if not py_files:
        print(f"FATAL: {src_app} 里没有 .py 文件")
        sys.exit(1)

    # /etc 三件套：取基础镜像现状 -> 追加非 root 用户
    passwd, pw_mode = base_etc_file(cache, n, "etc/passwd")
    group, gp_mode = base_etc_file(cache, n, "etc/group")
    shadow, sh_mode = base_etc_file(cache, n, "etc/shadow")
    if passwd is None or group is None:
        print("FATAL: 基础层里找不到 /etc/passwd 或 /etc/group")
        sys.exit(1)

    # busybox addgroup -S / adduser -S 的产物格式
    passwd = with_line(
        passwd,
        f"{APP_USER}:x:{APP_UID}:{APP_GID}:Linux User,,,:{APP_HOME}:{APP_SHELL}",
        "/etc/passwd")
    group = with_line(group, f"{APP_GROUP}:x:{APP_GID}:", "/etc/group")
    if shadow is None:
        shadow = b""
        sh_mode = 0o600
        print("      注意: 基础镜像没有 /etc/shadow，将新建")
    # 口令字段写 ! 表示锁定；不设老化策略，与该文件里其余条目的写法保持一致。
    # 容器只是以这个 uid 运行（不做登录认证），所以这条记录写什么都不会被用到。
    shadow = with_line(shadow, f"{APP_USER}:!::0:::::", "/etc/shadow")

    print(f"      应用层内容: app/app 下 {len(py_files)} 个 .py，"
          f"UID/GID {APP_UID}")

    with LayerBuilder(out_path, fixed_mtime) as lb:
        # /app 与 /app/app（WORKDIR /app + COPY app/ /app/app/，chown 后归 10001）
        lb.emit("app", "dir", 0o755, APP_UID, APP_GID)
        lb.emit("app/app", "dir", 0o755, APP_UID, APP_GID)
        for fn in py_files:
            with open(os.path.join(src_app, fn), "rb") as f:
                lb.emit_blob(f"app/app/{fn}", f.read(), 0o644, APP_UID, APP_GID)

        # /data：VOLUME 挂载点，属主必须是运行用户，否则非 root 进程写不了会话文件
        lb.emit("data", "dir", 0o755, APP_UID, APP_GID)

        # /etc 三件套
        lb.emit_blob("etc/passwd", passwd, pw_mode or 0o644)
        lb.emit_blob("etc/group", group, gp_mode or 0o644)
        lb.emit_blob("etc/shadow", shadow, sh_mode or 0o600)
        counts = (lb.files, lb.dirs)

    # 卫生检查：回到**未改动**的原始条目名判定，别用归一化后的集合
    bad_abs = [p for p in lb.names if p.startswith("/")]
    bad_dot = [p for p in lb.names if p.startswith("..") or "/../" in p]
    bad_win = [p for p in lb.names if "\\" in p]
    if bad_abs or bad_dot or bad_win:
        print(f"FATAL: 层内路径不卫生\n  绝对路径 {bad_abs[:5]}\n"
              f"  .. 片段 {bad_dot[:5]}\n  Windows 路径 {bad_win[:5]}")
        sys.exit(1)

    diff_id = "sha256:" + sha256_file(out_path)
    print(f"      应用层: {counts[0]} 文件 / {counts[1]} 目录, "
          f"diff_id {diff_id[7:19]}...")
    return diff_id, lb.names


# ------------------------------------------------- 改写镜像 config
def make_config(base_cfg, arch, diff_ids, created):
    out = dict(base_cfg)
    out["architecture"] = arch
    out["os"] = "linux"
    out["created"] = created
    out.pop("container_config", None)        # 上游 build 残留，别带进新镜像

    c = dict(out.get("config") or {})
    c["WorkingDir"] = "/app"
    c["User"] = APP_USER
    c["Entrypoint"] = ["python", "-u", "-m", "app.main"]
    # 设了 ENTRYPOINT，基础镜像的 CMD 会被 docker build 清空 —— 保持一致写 null
    c["Cmd"] = None

    env = list(c.get("Env") or [])
    for k, v in APP_ENV:
        env = [e for e in env if not e.startswith(k + "=")] + [f"{k}={v}"]
    c["Env"] = env

    c["ExposedPorts"] = {"8099/tcp": {}, "6600/tcp": {}}
    c["Volumes"] = {"/data": {}}
    labels = dict(c.get("Labels") or {})
    labels.update(APP_LABELS)
    c["Labels"] = labels
    c["Healthcheck"] = {
        "Test": ["CMD-SHELL", HEALTH_CMD],
        "Interval": 60_000_000_000,
        "Timeout": 6_000_000_000,
        "StartPeriod": 25_000_000_000,
        "Retries": 3,
    }
    out["config"] = c

    out["rootfs"] = {"type": "layers", "diff_ids": list(diff_ids)}
    out["history"] = list(out.get("history") or []) + [{
        "created": created,
        "created_by": f"offline-build: COPY app (ipv6sync {IMAGE_VERSION}) "
                      f"+ adduser {APP_USER}({APP_UID}) + mkdir /data",
        "empty_layer": False,
    }]
    return out


def check_history(base_cfg, n_layers):
    """history 里非 empty_layer 的条目数必须等于 diff_ids 数量。"""
    hist = base_cfg.get("history") or []
    non_empty = [h for h in hist if not h.get("empty_layer")]
    if len(non_empty) != n_layers:
        print(f"FATAL: 基础镜像 history 与层数不一致 "
              f"(非空 {len(non_empty)} != 层 {n_layers})")
        sys.exit(1)
    print(f"      history 自洽: {len(hist)} 条，其中 {len(non_empty)} 条对应层")


# ------------------------------------------------- 拼 docker-save 归档
def assemble(repo_tags, cfg_json, layer_ids, all_layer_paths,
             fixed_mtime, out_path):
    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    with tarfile.open(tmp, "w", format=tarfile.GNU_FORMAT) as out:
        for idx, lid in enumerate(layer_ids):
            # 每层一个目录：<层id>/{VERSION,json,layer.tar}
            # parent 指向链上的下一项（与 docker save 的既有产出保持一致）
            m = {"id": lid}
            if idx + 1 < len(layer_ids):
                m["parent"] = layer_ids[idx + 1]
            mb = json.dumps(m, separators=(",", ":")).encode()

            ti = tarfile.TarInfo(f"{lid}/")
            ti.type, ti.mtime, ti.mode = tarfile.DIRTYPE, fixed_mtime, 0o755
            out.addfile(ti)
            for name, blob in ((f"{lid}/VERSION", b"1.0"), (f"{lid}/json", mb)):
                ti = tarfile.TarInfo(name)
                ti.size, ti.mtime, ti.mode = len(blob), fixed_mtime, 0o644
                out.addfile(ti, io.BytesIO(blob))
            p = all_layer_paths[idx]
            ti = tarfile.TarInfo(f"{lid}/layer.tar")
            ti.size = os.path.getsize(p)
            ti.mtime, ti.mode = fixed_mtime, 0o644
            with open(p, "rb") as f:
                out.addfile(ti, f)

        cfg_hex = hashlib.sha256(cfg_json).hexdigest()
        for name, blob in (
            (f"{cfg_hex}.json", cfg_json),
            ("manifest.json", json.dumps([{
                "Config": f"{cfg_hex}.json",
                "RepoTags": repo_tags,
                # Layers 必须与 config.rootfs.diff_ids 同序（底 -> 顶）
                "Layers": [f"{lid}/layer.tar" for lid in layer_ids],
            }], separators=(",", ":")).encode()),
            ("repositories", json.dumps(
                {IMAGE_NAME: {IMAGE_TAG: layer_ids[-1]}},
                separators=(",", ":")).encode()),
        ):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime, ti.mode = len(blob), fixed_mtime, 0o644
            out.addfile(ti, io.BytesIO(blob))
    os.replace(tmp, out_path)
    return hashlib.sha256(cfg_json).hexdigest()


# --------------------------------------------------------------- main
def build_one(arch, args):
    cache = os.path.join(CACHE_ROOT, arch)
    meta_path = os.path.join(cache, "meta.json")
    if not os.path.exists(meta_path):
        print(f"FATAL: 缺少 {meta_path}，先跑: python fetch_base.py --arch {arch}")
        sys.exit(1)
    meta = json.load(open(meta_path, encoding="utf-8"))
    base_cfg = json.load(open(os.path.join(cache, "config.json"), encoding="utf-8"))

    print(f"\n{'='*66}\n==== 组装 {IMAGE_NAME}:{IMAGE_TAG}  linux/{arch}\n{'='*66}")
    print(f"基础镜像: {meta['repo']}:{meta['tag']}  Alpine {meta['alpine_release']}"
          f"（{meta['layers']} 层，来自 {meta['registry_mirror']}）")

    # 0. 基础层校验
    diff_ids = list(base_cfg["rootfs"]["diff_ids"])
    if len(diff_ids) != meta["layers"]:
        print("FATAL: config.rootfs.diff_ids 数量与 meta.layers 不一致")
        sys.exit(1)
    base_layers = []
    for i in range(meta["layers"]):
        p = os.path.join(cache, f"layer-{i}.tar")
        if not os.path.exists(p):
            print(f"FATAL: 缺少基础层 {p}")
            sys.exit(1)
        if sha256_file(p) != diff_ids[i].split(":", 1)[1]:
            print(f"FATAL: 基础层 {i} 与 diff_id 不匹配，缓存已损坏")
            sys.exit(1)
        base_layers.append(p)
    print(f"[1/5] 基础层校验通过（{meta['layers']} 层）")
    check_history(base_cfg, meta["layers"])

    # 1. 应用层
    print("[2/5] 打包应用层 ...")
    app_layer = os.path.join(HERE, f"app-layer-{arch}.tar")
    app_diff_id, app_names = build_app_layer(cache, meta, app_layer, args.mtime)

    # 2. config
    print("[3/5] 改写镜像 config ...")
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(args.mtime))
    all_diff_ids = diff_ids + [app_diff_id]
    out_cfg = make_config(base_cfg, meta["oci_arch"], all_diff_ids, created)
    cfg_json = json.dumps(out_cfg, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
    print(f"      created={created}  Entrypoint=/app 下 python -u -m app.main")

    # 3. 拼归档
    print("[4/5] 拼装 docker-save 归档 ...")
    layer_ids = [d.split(":", 1)[1] for d in all_diff_ids]
    repo_tags = [f"{IMAGE_NAME}:{IMAGE_TAG}"]
    os.makedirs(DIST_DIR, exist_ok=True)
    tag_suffix = "" if IMAGE_TAG == "latest" else f"-{IMAGE_TAG}"
    out_path = os.path.join(
        DIST_DIR, f"{IMAGE_NAME}-{IMAGE_VERSION}{tag_suffix}-{arch}-image.tar")
    cfg_hex = assemble(repo_tags, cfg_json, layer_ids,
                       base_layers + [app_layer], args.mtime, out_path)
    print(f"      {len(layer_ids)} 层（{meta['layers']} 基础 + 1 应用），"
          f"config {cfg_hex[:16]}...")

    # 4. 产物摘要
    print("[5/5] 产出 sha256 ...")
    size = os.path.getsize(out_path)
    fname = os.path.basename(out_path)
    digest = sha256_file(out_path)
    entry = f"{digest} *{fname}"
    sums = os.path.join(DIST_DIR, "SHA256SUMS.txt")
    kept = []
    if os.path.exists(sums):
        for line in open(sums, encoding="utf-8").read().splitlines():
            line = line.strip()
            if not line or line.endswith("*" + fname):
                continue
            ref = line.split("*", 1)[-1]
            if os.path.exists(os.path.join(DIST_DIR, ref)):
                kept.append(line)
    kept.append(entry)
    with open(sums, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(kept)) + "\n")

    if not args.keep_layers:
        try:
            os.remove(app_layer)
        except OSError:
            pass

    return {
        "arch": arch,
        "path": out_path,
        "size": size,
        "sha256": digest,
        "cfg_hex": cfg_hex,
        "diff_ids": all_diff_ids,
        "layer_ids": layer_ids,
        "app_files": app_names,
        "repo_tags": repo_tags,
        "created": created,
        "base": {k: meta[k] for k in
                 ("repo", "tag", "layers", "image_digest", "registry_mirror",
                  "alpine_release", "diff_ids")},
    }


def main():
    ap = argparse.ArgumentParser(description="离线组装 ipv6sync 镜像 tar")
    ap.add_argument("--arch", default="amd64",
                    help="amd64 / arm64 / all；默认只出 amd64")
    ap.add_argument("--keep-layers", action="store_true",
                    help="保留中间产物 app-layer-<arch>.tar")
    ap.add_argument("--report", default=None, help="构建报告 json 输出路径")
    args = ap.parse_args()

    epochs = os.environ.get("SOURCE_DATE_EPOCH")
    args.mtime = int(epochs) if epochs else int(time.time())
    if epochs:
        print(f"SOURCE_DATE_EPOCH={epochs} -> 可复现构建")
    else:
        print("提示: 未设 SOURCE_DATE_EPOCH，目录 mtime 取当前时间，"
              "重复构建的 sha256 会变")

    arches = ARCHES if args.arch == "all" else [
        a.strip() for a in args.arch.split(",") if a.strip()]
    results = [build_one(a, args) for a in arches]

    print(f"\n{'='*66}\n==== 完成 ====")
    for r in results:
        print(f"{r['arch']:<6}: {os.path.basename(r['path'])}  "
              f"{r['size']/1e6:.1f} MB  sha256 {r['sha256'][:16]}...")
    print(f"\nSHA256SUMS: {os.path.join(DIST_DIR, 'SHA256SUMS.txt')}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"version": IMAGE_VERSION, "image": IMAGE_NAME,
                       "tag": IMAGE_TAG, "results": results},
                      f, indent=1, ensure_ascii=False)
        print(f"构建报告  : {args.report}")


if __name__ == "__main__":
    main()
