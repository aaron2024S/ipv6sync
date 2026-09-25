#!/usr/bin/env python3
"""静态校验：只读字节，不执行任何镜像内容。

血泪教训 —— 这类手工组装的镜像最容易出现"静态检查全绿、容器一启动就崩"，
所以这里把能靠字节判定的东西全部查一遍：

  1. manifest.json 的 Config 文件名 == sha256(config 内容)
  2. len(manifest.Layers) == len(config.rootfs.diff_ids)
  3. 每层 sha256(layer.tar) == diff_ids[i]          <- docker load 会校验这一条
  4. 每层内部：无绝对路径 / 无 .. / 无 Windows 分隔符 / 无重复路径
  5. 基础系统文件在位：/bin/sh、ld-musl-*、/etc/passwd
  6. 入口解析：PATH 里的 python -> 符号链接链 -> 目标架构 ELF，且带可执行位
  7. /app 与 /app/app 存在、属主 10001；/app/app/main.py 在
  8. config.User 能在镜像自己的 /etc/passwd 里查到，且 uid 一致
  9. 工作目录里 CMD/ENTRYPOINT 的相对路径可解析
 10. 层内的 .py 是无 BOM 的 UTF-8、纯 LF
 11. healthcheck 依赖的 /healthz 在应用代码里真的实现了

用法：
  python validate_image.py --arch amd64
  python validate_image.py --arch all
"""
import argparse
import hashlib
import io
import json
import os
import posixpath
import re
import struct
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)          # ipv6sync/
DIST_DIR = os.path.join(HERE, "dist")
IMAGE = "ipv6sync"


def _read_version(fallback: str = "1.0.5") -> str:
    """版本号唯一来源是 app/__init__.py，避免发版时多处硬编码漏改。"""
    try:
        with open(os.path.join(PROJECT, "app", "__init__.py"),
                  encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return fallback
    m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else fallback


VERSION = _read_version()

ELF_MACHINE = {0x3E: "amd64", 0xB7: "arm64"}
ARCHES = ["amd64", "arm64"]

fails = []
warns = []


def fail(msg):
    fails.append(msg)
    print(f"  [FAIL] {msg}")


def warn(msg):
    warns.append(msg)
    print(f"  [WARN] {msg}")


def ok(msg):
    print(f"  [ok]   {msg}")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def norm(name):
    n = name[2:] if name.startswith("./") else name
    n = n.lstrip("/")
    while n.endswith("/"):
        n = n[:-1]
    return n


def read_elf_machine(blob):
    if blob[:4] != b"\x7fELF":
        return None
    if blob[5] != 1:
        return None
    return ELF_MACHINE.get(struct.unpack("<H", blob[18:20])[0])


class View:
    """把各层叠成一个"最终文件系统"视图（上层覆盖下层）。"""

    def __init__(self):
        self.members = {}      # norm path -> TarInfo
        self.layer_of = {}
        self.file_data = {}    # norm path -> bytes（只留小文件）

    def add_layer(self, idx, tf):
        for m in tf.getmembers():
            k = norm(m.name)
            if not k:
                continue
            self.members[k] = m
            self.layer_of[k] = idx
            if m.isfile() and m.size <= 4 << 20:
                f = tf.extractfile(m)
                self.file_data[k] = f.read() if f else b""

    def get(self, path):
        return self.members.get(norm(path))

    def data(self, path):
        return self.file_data.get(norm(path))

    def resolve(self, path, depth=0):
        """跟随符号链接（同目录内的相对目标），返回最终真实路径。"""
        if depth > 16:
            return None
        m = self.get(path)
        if m is None:
            return None
        if m.issym():
            tgt = m.linkname
            if not tgt.startswith("/"):
                tgt = posixpath.join(posixpath.dirname(norm(path)), tgt)
            return self.resolve(tgt, depth + 1)
        return norm(path)


def validate(arch):
    print(f"\n{'='*66}\n==== 静态校验 {IMAGE}:latest  linux/{arch}\n{'='*66}")
    tar_path = os.path.join(DIST_DIR, f"{IMAGE}-{VERSION}-{arch}-image.tar")
    if not os.path.exists(tar_path):
        fail(f"产物不存在: {tar_path}")
        return None
    print(f"产物: {os.path.basename(tar_path)}  "
          f"{os.path.getsize(tar_path)/1e6:.1f} MB")

    with tarfile.open(tar_path, "r:") as out:
        names = out.getnames()
        mf = out.extractfile("manifest.json")
        if mf is None:
            fail("归档里没有 manifest.json")
            return None
        manifest = json.load(mf)

        # --- 1. manifest 结构
        if len(manifest) != 1:
            fail(f"manifest.json 有 {len(manifest)} 个条目，期望 1")
            return None
        m0 = manifest[0]
        cfg_name = m0["Config"]
        cfg_raw = out.extractfile(cfg_name)
        if cfg_raw is None:
            fail(f"manifest 指向的 config {cfg_name} 不存在")
            return None
        cfg_raw = cfg_raw.read()
        if sha256_bytes(cfg_raw) != cfg_name.split(".")[0]:
            fail(f"config 文件名与内容 sha256 不符: {cfg_name}")
        else:
            ok(f"config 文件名 == sha256(内容)  {cfg_name[:20]}...")
        cfg = json.loads(cfg_raw)

        # --- 2. 层数
        diff_ids = cfg["rootfs"]["diff_ids"]
        layers = m0["Layers"]
        if len(diff_ids) != len(layers):
            fail(f"Layers {len(layers)} != diff_ids {len(diff_ids)}")
            return None
        ok(f"层数一致: {len(layers)} 层")

        # --- 3. 逐层 diff_id
        layer_blobs = []
        for i, lp in enumerate(layers):
            blob = out.extractfile(lp)
            if blob is None:
                fail(f"Layers[{i}] 指向的 {lp} 不存在")
                return None
            blob = blob.read()
            layer_blobs.append(blob)
            got = sha256_bytes(blob)
            want = diff_ids[i].split(":", 1)[1]
            if got != want:
                fail(f"层 {i} layer.tar sha256 与 diff_id 不符 "
                     f"({got[:16]} != {want[:16]})")
            else:
                ok(f"层 {i} sha256(layer.tar) == diff_id  {got[:16]}...")
            # 目录名与 diff_id 是否一致（可选约定，便于排查）
            d = posixpath.dirname(lp)
            if d != want:
                warn(f"层 {i} 目录名 {d[:16]}... 不等于 diff_id（不影响 load）")

        # --- 4/5. 层内卫生 + 视图
        view = View()
        for i, blob in enumerate(layer_blobs):
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as lt:
                raw_names = []
                seen = set()
                dup = []
                for mm in lt.getmembers():
                    raw_names.append(mm.name)
                    k = norm(mm.name)
                    if k and k in seen:
                        dup.append(mm.name)
                    seen.add(k)
                if dup:
                    fail(f"层 {i} 内路径重复（docker load 会拒绝）: {dup[:5]}")
                absb = [p for p in raw_names if p.startswith("/")]
                dotb = [p for p in raw_names if p.startswith("..") or "/../" in p]
                winb = [p for p in raw_names if "\\" in p]
                if absb:
                    fail(f"层 {i} 含绝对路径条目（containerd 解包器会拒绝）: "
                         f"{absb[:5]}")
                if dotb:
                    fail(f"层 {i} 含 .. 片段: {dotb[:5]}")
                if winb:
                    fail(f"层 {i} 含 Windows 路径: {winb[:5]}")
                if not (dup or absb or dotb or winb):
                    ok(f"层 {i} 路径卫生: {len(raw_names)} 个条目，"
                       f"无绝对路径/重复/反斜杠")
                lt2 = tarfile.open(fileobj=io.BytesIO(blob), mode="r:")
                view.add_layer(i, lt2)
                lt2.close()

        # --- 5. 基础系统文件
        sh = view.get("bin/sh") or view.get("usr/bin/sh")
        if not sh:
            fail("找不到 /bin/sh（HEALTHCHECK 的 CMD-SHELL 需要它）")
        else:
            ok("/bin/sh 在位")
        musl = [k for k in view.members if k.startswith("lib/ld-musl-")]
        if not musl:
            fail("找不到 lib/ld-musl-*.so.1")
        else:
            ok(f"{musl[0]} 在位")
        if not view.get("etc/passwd"):
            fail("找不到 /etc/passwd")
        else:
            ok("/etc/passwd 在位")

        # --- 6. 入口解析：PATH 里的 python -> ELF
        env = cfg["config"].get("Env") or []
        path_val = next((e.split("=", 1)[1] for e in env
                         if e.startswith("PATH=")), "")
        entry = cfg["config"].get("Entrypoint") or []
        prog = entry[0] if entry else None
        real = None
        if prog and "/" not in prog:
            for d in path_val.split(":"):
                cand = posixpath.join(d.strip("/"), prog) if d.strip("/") else prog
                if view.get(cand):
                    real = view.resolve(cand)
                    break
        else:
            real = view.resolve(prog or "")
        if not real:
            fail(f"入口 {prog!r} 在 PATH({path_val}) 里解析不到")
        else:
            m = view.get(real)
            blob = view.data(real)
            if blob is None:
                warn(f"入口 {real} 层内文件 >4MB，跳过 ELF 头读取")
            else:
                mach = read_elf_machine(blob)
                if mach is None:
                    fail(f"入口 {real} 不是 Linux ELF")
                elif mach != arch:
                    fail(f"入口 {real} 架构不符: {mach} != {arch}")
                else:
                    ok(f"入口 {prog} -> {real} 是 {mach} ELF")
            if m and not (m.mode & 0o111):
                fail(f"入口 {real} 没有可执行位 (mode={oct(m.mode)})")
            else:
                ok(f"入口 {real} 有可执行位 (mode={oct(m.mode) if m else '?'})")

        # --- 7. 应用内容与属主
        wd = cfg["config"].get("WorkingDir") or "/"
        appdir = posixpath.join(wd, "app")
        for p, want_uid in ((wd, 10001), (appdir, 10001), ("data", 10001)):
            m = view.get(p)
            if not m:
                fail(f"缺少目录 {p}")
                continue
            if not m.isdir():
                fail(f"{p} 不是目录")
            elif (m.uid, m.gid) != (want_uid, want_uid):
                fail(f"{p} 属主 {m.uid}:{m.gid}，期望 {want_uid}:{want_uid}")
            else:
                ok(f"{p}/ 存在，属主 {m.uid}:{m.gid}，mode {oct(m.mode)}")

        # 视图里的键是归一化过的（无前导 /），前缀匹配也必须用归一化形式，
        # 否则这里会永远匹配不到、把"有文件"误报成"没文件"
        app_prefix = norm(appdir) + "/"
        app_files = sorted(k for k in view.members
                           if k.startswith(app_prefix)
                           and view.get(k).isfile())
        pys = [k for k in app_files if k.endswith(".py")]
        if not pys:
            fail(f"{appdir}/ 下没有 .py 文件")
        else:
            ok(f"{appdir}/ 下 {len(pys)} 个 .py: "
               f"{', '.join(posixpath.basename(p) for p in pys)}")
        # ENTRYPOINT 是 -m app.main：包必须能 import
        for need in (f"{appdir}/__init__.py", f"{appdir}/main.py"):
            if not view.get(need):
                fail(f"ENTRYPOINT `-m app.main` 需要 {need}，但不存在")

        # main 直接 import 的模块也必须在层里，否则容器一启动就 ImportError
        # （离线手工拼 config/层的时候最容易漏这类"新加的模块没进层"）
        for need in ("node", "webui", "webpage", "settings", "router", "sync",
                     "config", "discover", "trustlist"):
            if not view.get(f"{appdir}/{need}.py"):
                fail(f"{appdir}/{need}.py 不在镜像里，但 app.main 需要它")
        ok("app.main 依赖的模块都在层里")

        # Web 控制台的页面里必须有登录表单（否则控制台等于裸奔）
        page = view.data(f"{appdir}/webpage.py") or b""
        for marker in (b'type="password"', b"/api/login"):
            if marker not in page:
                fail(f"webpage.py 里找不到 {marker!r} —— 控制台可能没有登录保护")
        ok("控制台页面含登录表单与登录接口调用")

        # --- 8. 用户名可解析
        user = cfg["config"].get("User")
        pw = (view.data("etc/passwd") or b"").decode("utf-8", "replace")
        ent = next((ln for ln in pw.splitlines()
                    if ln.split(":", 1)[0] == user), None)
        if not ent:
            fail(f"config.User={user!r} 在镜像 /etc/passwd 里查不到 —— "
                 f"docker 启动时会直接失败")
        else:
            f = ent.split(":")
            if int(f[2]) != 10001 or int(f[3]) != 10001:
                fail(f"用户 {user} uid/gid = {f[2]}:{f[3]}，期望 10001:10001")
            else:
                ok(f"用户 {user} 可解析: uid={f[2]} gid={f[3]} shell={f[6]}")
            if f[6] in ("/sbin/nologin", "/bin/false") and not view.get(f[6].lstrip("/")):
                warn(f"{f[6]} 在镜像里不存在（不影响以该 uid 直接运行）")
        grp = (view.data("etc/group") or b"").decode("utf-8", "replace")
        if not any(ln.split(":", 1)[0] == "syncapp" for ln in grp.splitlines()):
            fail("/etc/group 里没有 syncapp")
        else:
            ok("/etc/group 里有 syncapp")

        # --- 9. 卷与端口
        vols = cfg["config"].get("Volumes") or {}
        if "/data" not in vols:
            fail("config.Volumes 里缺 /data")
        else:
            ok("config.Volumes 含 /data")
        ep = cfg["config"].get("ExposedPorts") or {}
        for p in ("8099/tcp", "6600/tcp"):
            if p not in ep:
                fail(f"config.ExposedPorts 里缺 {p}")
            else:
                ok(f"config.ExposedPorts 含 {p}")
        # 手工拼 config 时最容易漏的就是这些默认环境变量（镜像层是复用的，
        # config 是自己写的），所以按 Dockerfile 逐条核对
        envs = {e.split("=", 1)[0]: e.split("=", 1)[1]
                for e in (cfg["config"].get("Env") or []) if "=" in e}
        for k, want in (("SESSION_FILE", "/data/session.json"),
                        ("SETTINGS_FILE", "/data/settings.json"),
                        ("HEALTH_PORT", "8099"), ("PORT", "6600")):
            if envs.get(k) != want:
                fail(f"config.Env[{k}] = {envs.get(k)!r}，期望 {want!r}")
        ok("config.Env 里的路径与端口默认值正确")
        if "WEB_PASSWORD" in envs or "ROUTER_PASSWORD" in envs:
            fail("镜像里烘进了密码类环境变量 —— 必须由运行时注入，不能进镜像")
        else:
            ok("镜像里没有烘进任何密码")
        if cfg["config"].get("Cmd"):
            warn(f"config.Cmd 非空 {cfg['config']['Cmd']}；"
                 f"Dockerfile 只设了 ENTRYPOINT，docker build 会清空 Cmd")
        else:
            ok("config.Cmd 为空（与只设 ENTRYPOINT 的语义一致）")

        # --- 10. 源码卫生：无 BOM、纯 LF
        for p in pys:
            b = view.data(p)
            if b is None:
                continue
            if b.startswith(b"\xef\xbb\xbf"):
                warn(f"{p} 带 UTF-8 BOM")
            if b"\r\n" in b:
                fail(f"{p} 含 CRLF（Linux 上应统一 LF）")
        ok(f"{len(pys)} 个 .py 均为无 BOM 的 LF 文本")

        # --- 11. healthcheck 依赖的 /healthz
        if b"/healthz" not in (view.data(f"{appdir}/main.py") or b""):
            fail(f"{appdir}/main.py 里没有 /healthz（HEALTHCHECK 会永远失败）")
        else:
            ok("main.py 里实现了 /healthz")

    # --- 产物哈希清单
    sums = os.path.join(DIST_DIR, "SHA256SUMS.txt")
    if not os.path.exists(sums):
        fail("缺少 SHA256SUMS.txt")
    else:
        h = hashlib.sha256()
        with open(tar_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        want = h.hexdigest()
        hit = [ln for ln in open(sums, encoding="utf-8").read().splitlines()
               if ln.strip().endswith("*" + os.path.basename(tar_path))]
        if not hit:
            fail(f"SHA256SUMS.txt 里没有 {os.path.basename(tar_path)}")
        elif hit[0].split()[0] != want:
            fail(f"SHA256SUMS.txt 记录的哈希与实际不符: "
                 f"{hit[0].split()[0][:16]} != {want[:16]}")
        else:
            ok(f"SHA256SUMS.txt 命中且一致  {want[:16]}...")

    return {
        "arch": arch,
        "path": tar_path,
        "sha256": sha256_bytes(open(tar_path, "rb").read())
        if os.path.getsize(tar_path) < (1 << 30) else None,
        "layers": len(diff_ids),
        "diff_ids": diff_ids,
        "app_files": pys,
        "config_hex": cfg_name.split(".")[0],
    }


def main():
    ap = argparse.ArgumentParser(description="静态校验离线镜像 tar")
    ap.add_argument("--arch", default="amd64", help="amd64 / arm64 / all")
    args = ap.parse_args()
    arches = ARCHES if args.arch == "all" else [
        a.strip() for a in args.arch.split(",") if a.strip()]

    results = [r for r in (validate(a) for a in arches) if r]

    print(f"\n{'='*66}")
    if fails:
        print(f"==== 校验失败：{len(fails)} 项 FAIL，{len(warns)} 项 WARN ====")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print(f"==== 全部通过（{len(warns)} 项 WARN）====")
    for w in warns:
        print(f"  ! {w}")
    for r in results:
        print(f"  {r['arch']}: {r['layers']} 层, sha256 {r['sha256'][:16]}...")


if __name__ == "__main__":
    main()
