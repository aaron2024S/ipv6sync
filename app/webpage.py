# -*- coding: utf-8 -*-
"""
Web 控制台的页面（单文件、零外部资源 —— 容器里没有外网，不能引 CDN）

设计约束：
  · 所有动态文本一律用 textContent 写入，绝不拼 innerHTML。
    路由器返回的主机名是不可信输入，拼 HTML 会直接变成 XSS。
  · 不引任何第三方脚本/字体，全部本地内联。
  · 页面是「登录 → 设置」两态，靠 JS 切换，不做二次跳转。
"""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>IPv6 白名单同步 · 控制台</title>
<style>
:root{
  --bg:#f5f6f8; --card:#fff; --line:#e3e6ea; --text:#1f2328; --muted:#6b7280;
  --accent:#2563eb; --accent-weak:#eff4ff; --ok:#12805c; --ok-bg:#e8f6f0;
  --warn:#9a6700; --warn-bg:#fff8e5; --err:#b42318; --err-bg:#fdeceb;
  --radius:10px;
}
/* 手动深色：页面右上角切换按钮设置 data-theme="dark" 时强制深色 */
:root[data-theme="dark"]{
  --bg:#16181d; --card:#1e2126; --line:#2e3238; --text:#e8eaed;
  --muted:#9aa1ab; --accent:#6ea8fe; --accent-weak:#1d2735;
  --ok:#4ec9a0; --ok-bg:#16261f; --warn:#e3b341; --warn-bg:#2a2416;
  --err:#f0837a; --err-bg:#2b1b1a;
}
/* 自动模式：跟随系统，但用户手动选了浅色时不覆盖 */
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#16181d; --card:#1e2126; --line:#2e3238; --text:#e8eaed;
    --muted:#9aa1ab; --accent:#6ea8fe; --accent-weak:#1d2735;
    --ok:#4ec9a0; --ok-bg:#16261f; --warn:#e3b341; --warn-bg:#2a2416;
    --err:#f0837a; --err-bg:#2b1b1a;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",sans-serif;}
a{color:var(--accent)}
.hide{display:none !important}

/* ---------- 登录 ---------- */
#login{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.login-card{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);
  border-radius:14px;padding:28px 26px;box-shadow:0 10px 30px rgba(16,24,40,.06)}
.login-card h1{font-size:18px;margin:0 0 4px;display:flex;align-items:center;justify-content:center;gap:8px}
.login-card p.sub{margin:0 0 20px;color:var(--muted);font-size:12.5px;text-align:center}
label{display:block;font-size:12.5px;color:var(--muted);margin:14px 0 6px}
input[type=text],input[type=password],input[type=number],select{
  width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
  background:var(--card);color:var(--text);font-size:14px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-weak)}
button{font-family:inherit;font-size:13.5px;padding:9px 15px;border-radius:8px;
  border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{filter:brightness(1.06);color:#fff}
button:disabled{opacity:.5;cursor:default}
button.block{width:100%;margin-top:22px;padding:11px}
.msg{margin-top:12px;font-size:12.5px;padding:9px 11px;border-radius:8px;display:none}
.msg.err{display:block;background:var(--err-bg);color:var(--err)}
.msg.ok{display:block;background:var(--ok-bg);color:var(--ok)}
.msg.warn{display:block;background:var(--warn-bg);color:var(--warn)}

/* ---------- 主界面 ---------- */
#app{max-width:1080px;margin:0 auto;padding:22px 18px 96px}
header.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
header.top h1{font-size:17px;margin:0;display:flex;align-items:center;gap:8px}
h1 .logo{width:24px;height:24px;border-radius:6px;display:block;flex:none}
header.top .spacer{flex:1}
.who{color:var(--muted);font-size:12.5px}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.tabs{display:flex;gap:4px;margin-bottom:18px;border-bottom:1px solid var(--line)}
.tab{background:transparent;border:none;border-bottom:2px solid transparent;
  border-radius:0;padding:9px 18px;font-size:13.5px;color:var(--muted);cursor:pointer}
.tab:hover{color:var(--accent)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.pill{display:inline-flex;align-items:center;gap:6px;background:var(--card);
  border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:12.5px}
.pill b{font-weight:600}
.dot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex:none}
.dot.on{background:var(--ok)}.dot.off{background:#c0c4ca}
.dot.err{background:var(--err)}.dot.warn{background:#d9a300}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px 20px;margin-bottom:16px}
.card h2{font-size:14px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
.card h2 .n{color:var(--muted);font-weight:400;font-size:12.5px}
.card h2 .hbtn{margin-left:auto;padding:5px 12px;font-size:12.5px}
.card .desc{color:var(--muted);font-size:12.5px;margin:0 0 14px}
.card ul.desc{padding-left:18px;margin:0 0 8px}
.card ul.desc li{margin:0 0 7px}
.card ul.desc li:last-child{margin-bottom:0}
.card ul.desc b{color:var(--text)}
code{background:var(--accent-weak);padding:1px 6px;border-radius:4px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px 22px}
.fullrow{margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.chbox{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:12px}
.chbox:last-child{margin-bottom:2px}
.chbox .t{font-size:12.5px;font-weight:600;color:var(--text);margin:0 0 10px}
.chbox .f{margin-bottom:12px}
.chbox .f:last-child{margin-bottom:0}
.chbox .row2{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  gap:0 12px}
.remember{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);
  margin:16px 0 0;cursor:pointer;user-select:none}
.f{display:flex;flex-direction:column}
.f .lab{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted);
  margin-bottom:5px;min-height:18px}
.f .help{font-size:11.5px;color:var(--muted);margin-top:5px}
.switch{display:flex;align-items:center;gap:9px;height:37px}
.switch input{width:16px;height:16px;accent-color:var(--accent)}
.toolbar{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:16px}
pre.out{background:var(--bg);border:1px solid var(--line);border-radius:8px;
  padding:12px;font-size:12px;line-height:1.55;max-height:300px;overflow:auto;
  white-space:pre-wrap;word-break:break-all;margin:0;font-family:ui-monospace,
  SFMono-Regular,Menlo,Consolas,monospace}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);
  vertical-align:top;word-break:break-all}
th{color:var(--muted);font-weight:500;white-space:nowrap}
tr:last-child td{border-bottom:none}
.foot{position:fixed;left:0;right:0;bottom:0;background:var(--card);
  border-top:1px solid var(--line);padding:12px 18px;display:flex;gap:10px;
  align-items:center;justify-content:flex-end;z-index:20}
.foot .hint{flex:1;color:var(--muted);font-size:12.5px}
#toast{position:fixed;right:18px;bottom:70px;z-index:30;display:flex;
  flex-direction:column;gap:8px;align-items:flex-end}
.toast{background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:9px 13px;font-size:12.5px;box-shadow:0 6px 20px rgba(16,24,40,.10);
  max-width:420px}
.toast.ok{border-color:var(--ok);color:var(--ok)}
.toast.err{border-color:var(--err);color:var(--err)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
</style>
</head>
<body>

<!-- ======================= 登录 ======================= -->
<div id="login">
  <form class="login-card" id="loginForm" autocomplete="off">
    <h1><img class="logo" alt="" src="data:image/png;base64,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">IPv6 白名单同步</h1>
    <p class="sub">华为路由器 · IPv6 防火墙信任列表自动维护</p>
    <label for="u">用户名</label>
    <input type="text" id="u" name="u" autocomplete="username" autofocus>
    <label for="p">密码</label>
    <input type="password" id="p" name="p" autocomplete="current-password">
    <label class="remember"><input type="checkbox" id="rememberChk"> 保持登录(7天)</label>
    <button type="submit" class="primary block" id="loginBtn">登 录</button>
    <div class="msg" id="loginMsg"></div>
  </form>
</div>

<!-- ======================= 主界面 ======================= -->
<div id="app" class="hide">
  <header class="top">
    <h1><img class="logo" alt="" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAZI0lEQVR42o2beZQd1X3nP797q97e+6amJQSSkASMEJsBAQKs2MQLiwMGbBLbOTh24mSYLLZn8JyZHE7seI4dT07m2MnEcSCJSWIcFptkcIzBGIt9EQiDHMSiFe1Lq/W6+21V984fdate1XuvBe+cq9d6datu/X73t3x/yxVrraXHJwwNWisAZufq/Pz5bTz6zJu8+uZ+9h2aZaZaIwiCaHKPJwhgJft/bM+p3Td2fmz2eZL9h5zv0V8usGSij7NXn8SGdStZd84pFAp+Fy1dy3UyIP6viLD3wDHuvP95Hnh0C9v3TBOEBl8LnlYoka53tSegoXOS7SBYOvkobcK7ni2pSQLWgjGGMDAEYUgu57Hi5FGuf/8aPnHN+YyOVDJ0LcgAa20y4a/vfoKv3flz9h2uUinlKOb86J2sjUYPauInibwjG5L7pRfLHFGC7bpmpfOe9hwlEYEWS70RMN9osXTREF/4zcu55aMXddGYYUB8Yb7e5Pe+fC///O8vM9hXJJ/ThKGht6bYnn/2JF56zcsSKNhIzG2v++0C+mJ7Sp4WQWtNoxVwfLbBjVeu5S//53WUivkME8RGHyxQrzf52Bfv4qGn3mDRSIUgDCPCRXrTLidigO1+2R6GQFjgESeyDVYWmG0zOiKAEoXnKQ5Oz7HhghV87+u/QamYR5w6KABjLEqEW//XD/jx02+waKxCMwww8QOlx3ICVtxFBESwyXDv6L7jv5OnpG6znXNkgeu0B2Iz97VHtH77XsFgaYYh4yMVfvrcW9z6p/ejAOMkWoUmspB33Ps0//Sjl5gcrdAKwtRLSBcxyUKxwrshqhcRknkG3ZeS0X1fwtvkO9KQ9n9EJHVduh4av2szCJkYrfD9R17hO/c9jVaKMDSIMdYePFJl3Se/yXyjiacl0Xfb0873NE09/rRdqq5EUDEzrcUYGxlO93tEjGBtpNnGxurSKfKSVTQBiSxIakqHl5LILoQWSnmPx+/4z0yM9qNE4M4fPsfeY1VyBQ8jYJW4QWq0f0M5SVDvMAREgfYFqyxzzSaHZ2scPj7P8VqTlgW0wopQCwzH5hscqs5zbL5OPQxRWqE9BfGabqAsyhPwBLS4azb73mmJVRFDQoFcTrN3epbv/PCZSEiqs3V7xe98m50HjpL3VNvF2Q5/22uXe1kwG2mqiKCUUGu0mK816SsXWHXyGOeuWszalSdx6tQIo0NlinmfMDQcn2uw/0iV13cdYvPWvWx+Yw879h3FGENfKY+vFUFokuVqjRBRESbxPeUk6QQ21BlHpYRWYJgaHeDR//s7eBtf3Mb2A9OU8x7GGGJh6rb8sQLalDKmPLq7JgJaK2qNgNpsk1VLx7l+w1lcfdmZnLl8EqWEd/OpzjZ4+pUd3PPTX/DQM69x9Pgcg5UCnlIcn6/zlc99kHNXL+G3vnoPh47NkouZcAIOiNupQk6z68A0P3vhDbxHX3qTwIagdIebSe97SgJEFvRhSkWLHKnWWDk1wn+56TJueP/ZVEr5NsQ2JsXDDkvipE+J0FfJc+W6VVy5bhVv7j7MX93zJHc//CIHj89yyZql/N5N6wHYcP4K7nzweQr5IoExCwuBpARVgcGwcfNbeJu37UX7ghGLUdk7bBd+TeHTWEjcJU8pGq2AZjPg1hsu4bZP/QqDfaUEi8cqoZV6V87eWjA2EvkVS0b58z+6lt/44Hn87p/dw/Ub1rrrlmWLRzFiIzth0xgquzuScuWhgM5pfrFtH97bh6vkPK8nTu56McnqfwzafK2o1pqM9Bf55h9+nF+9aHVCuFKqKxCxCaQmJWPiPJckgqYlus9YizWWc09fzMa/vpVWaDDWoJVi+dRwxFRncHuj0LYPjv/M+Yq9R6p41UYL5UUuxGYcTGzQOuRc2m7QWIvvaWbm6px20jD/fPsnWLFkjCA0aCVdhBsTraKVcj67+4VjYpWShBkq4gbGWHI5j5x7FsApk0MUiz4haWAWo9QYILTjB+vWFE8z2wrwrDWRG0k2WDrQedbYtVlh8ZSiWmuwcskIP/zqLUyODhCEBq8H4SI4Ayi0gpDdB2fYf3SW43N18r7H6ECJxWMDDPUXI9fm7IVSKnkfpaQj4IKpsQGGBkrMzNXwdNobZIMPm0iWtLVZgYeyLvJqcydLbicYiZRJi1APAsYGy/zLn3yKydEBwh7Eh8aineV/8tWd3PfEFp7asou3D80wX29FRAoUfI+RgRJnLBnngxes5NpLzmB4oLSAO2u/53B/iYmRfg5V5/B9nQVxkoZtkvDFJh5B8GwMJRcKYy2JtZMucYW//eKNLF003LXzMZrTSnjutbf56t0b+fkvttNsBRTzmrynKZc8tyNRiH2kOsfDm9/gJy9u5S/uf5yPvXctn7nqQkb6SxEBHa9mnKosXTTEi9v2UFECho650uW5hDbM9kRJBtx2MkHi4DyWABuBj8Mzc3zppstZv3ZZD+IjW6EQvvb9n/P1e56gFYYMlPKoohfpOdZZeUniJM9TDObyNIOQ3UeOsfGVbVx36X9ipL+M7bkBFoWwavEIxlpES4bItDZkFDv50eJF+i9JeNhexqa8X/txSoT5ZpOzlk3w+Rsvz4h4e+chDEI+960HuOunmxkbKFEWjzC0mBixi21HyEnUKdSCgFPGB/nfn/kwl6w5NVEjJW3Gdtr55VOjiFIRZco6OjrcasZzxeogeAmqS0+IiZdu+ydKaAQBt338vRTyPqExiHNXFhL39NlvPcB3H32JyZEKQRASGBJxF8naGOvW1VqYnm+y4ZwVCfFAhsGxhBmHA4LQcPrJ45SKOUIsoqQbqfdQ8XhNDy3RZkhK5J14JFsa42iBuUaT96xazIcvPB1jbQbYGBMR/417H+euRzdz0nAfrTBoh7ipV8uYWrd2YC0DlSL3PrWFQj7HaVNjjA9UGBsoMVwp0l/OUynkIkAloB16rbdaGDGI0i7bs2AwkEWFAp6k4+9U0iFJ9khbDJRSNIKQW658D1pFwYlKXFbEjOe3vs1X732MsaESLRM6zJvK4znjKBKpU4wHJHbCItSCFt944HEsgqc1ec+jmM/RV/QZqBQZqZQY6y+xaKiPUt7jvidfQXs6cudWMrgtk8DpNIKAZ7UkEDIdAdh0Xs7l0OphyNKJQT50weou0RSJrPIff+9RArGIJ5jQJNJjXXYhiheEZhBQb4QExnTlFzylKBZ9Cr6HEqFlLPWwwVy1zp6Z44QmwghRPsFQzvvk8prQdmIVSRnBVBY59aNnnReQXgAoZQi1UhxvNLlszakMlosZ4xc60f/3Ta/z+NYdDPcVo5qBbkcgnlIY4HitgbWWk4b6WT01xuqpMU4a6aecy9EMQw7MzLJt/zSv7TnE9kPTzDdbVAo+hZyPNu3UtsTgH0sYmsgLZGRfeuy+ZLyCRG6QhHixkpKElBUUQZRCtLBh7XK3bNp1Rt93PvYi2hdaYYhyuQVxklKdb+BpxYfOWcnH16/l4tVLGRsoLxgW1ZsBr+48wAMvvMb9z/+SHYemGSjm8ZQisBYhbJt3Bcp2Gr8FfD/ZdJsnSrpzGmm34P4XYBnqK3H+8sUu2yptXyzCtv1HeWrbbgoFn7GBCvtnqi7BAtNzNd575jK+9GuXs27V0g5rbrNxvIBCKOQ8zj9tivNPm+IPrrqY7zz6At/6ybNUG036C3mCMMz6t44yhWRC1g7Ck6BIUChBlERVhWSQ+VaeomkMS8cHWDI60A5QUtnVJ17fyb6ZGT694Tye/JPP8F+vvpTD8zVaJuDLH3s//3rbJ1m3ainGWpcTiERWK4WnU0OpBPMbE80d6Sty27Xreei2T3L2KZMcrdXxfQ1KIvDj3lt0e6BJ0mWi4tRZmrbYBuisY+rpQZTQIuSUiUG0Usmup+e/sGMvVsGFKxfTV8zzhavXs2nHXq57zxnccNGaZKe1kq5sk03l4LLJ5khOrY3szJlLxvl/X/h1bvnOD/nRK28wUioShGahKgRkEjoZWJjQrEQ7/VZRwgKlQKuIQzoaohWhCItHBzO7HkVoEQ5449A0yvOYqTWwFgJj+N6tN3HDRWsckpMOxGgjD2DbxMYeNzQms4YIeFoRGkO5kOMfP3c97ztzOdONBp6vsSq98+lvhdIqoSMZMZ3KqUAkLmCd6FhFamJ0k9XCxGAfvQpb880WB+fm0L6iZUwG6UVgSboiRBHBUwoRqNabHDo+R7XWcEYzKr6GJpPoTqTP9zR3fPpalo0PMRu2UF46Y9wW8ThLTM8R0eyhpatU22UW3eT+coEu2RWhWmsw22yifEXTGackhu8By7QSDszM8k/PvcqjW3fy9vQMjVZA3veZGuznstOW8NFzT2f5+FBXQTNijGG4UuT/3PwBrvnLu0F53aVDpz5isymsNEutSNYL9K7xOQOjBH+BGntoDFZAaUXrBKlZay3NIOSuZ3/BNx5+hn0zs3hKyGln+Op19szM8MRbO/mrjS/y6YvXctsHLibn6QwTtFIExnDZqlO4/rzT+f6mX0b2wJi2u3NAzippJ4di35CKCZQ44iI7oBAdDdxo2wid0cv0J+975PM+ohSN2D11fIyTltcPHOFvntjM4bkavq/pKxbIeR5aazxPU8nnGK2UEAx/9sgz3HTHD5ipNaJUXMYuRH7/D37lIkr5HEYieyQulRbbNCWqTZujM5mnBGWdfnOi4Sow1WarZ4DRV8jTXy5ilKUed410fOKGijWLJ3j2v93CQ7fezG+uW0sTV9FxNbwQaFmDVcKiwTKPvLGDz/3Lj10+MVv+ttayZvEEFy1fTLXVQrSk7JerZmmiEduGDtumbGzkUj+mrX8s/qKFA7Pz3dV5CzlPs2igTCCROiwkAaHz6yJw3tJJvnHd+1g83E/duqAqY8CgYQyL+sv825Y3+e7zrySGMSNVwAfOWE4zeYa7X6toY1M0pTc7kmyJVCBKJKQAgnMR8W9WCdpT7J453mXYQpe7P3NyjABDyzoXliI4vke7ukC9FfDynoN85SdPsrc6S96PapIo5XYtfnFoYegr5vj2M5uptVqRregwsBedOhXlA5S0XXiacKVAqYRoUt7BQwmprHmSMVFW0lkw8r7HtukZAmPwUjmAmBUXn7oY5SlC92JK68yMbUeOsWnPATZuf5tNb+9nx5EZ5hpN+nJ+NoWdSVxYrAiFvM+26Rle2L2f9cuWJIFYvPbSoQFGKiVm6g18rVw43x0J9soTuGiwnfu0PbCgFSGX89h9fJbdx6qcOjyQoEHlskHrlk6xcnyEx7bvYvuRY5RyPpv3HeTpXft4eudeXj90lOl6HWstBa0paM1wueBgsWSbSJK/FYhFaaGFYcvBI6xftqQdiDmK+ot5BstFjtYb5JTqSOBKJgHS4d/w0KmkT5IuSqXAXb7OV4qjtQYv7NnHKSkGiNP7gWKBj5y5kr94ehMf+od7gMhmhMaS14qCpxku5pKSlrWWEFfS6kpeZMNWF31xtFbvCXd9rSnkfawC0S4pgnS5PunqSiJGgvFoW9A2WpKkNi+e8Mi2XZloMO2SPvuetYxXilRbTarNBv1Fn+FynmLeAyW0sASuBhkZJJXA2Mg4KazT4dgOJPZAYDhVZE1DliA0NK1F3PMig0cG8SXP6vB6Km31rRasB3ipye7GQEG5kGPj23s5MDfvOjlsYoyMtZw6MsRnzl/L0VaDfN4jEAjEEip6ulrRAl4bc1jX9GA9UpshGC14vmLNorFM4BbH0bPNJjOtJjqn2pbeUym6VCZWIGa2J20gFIWTbcAQM0RSO5DzNfvn57nnl68ntcG2KEVM+PylF3Lh4imONpv4nnbWVrX9bxKcRN+i2uuIUlGGWel2sKKFmgk5Y2KEsycnovyHylavds9UOdJo4Guv7badV4m9glWuepqREIWynmA8wXixC1KY2Ic6CTA6mhMoKOd9/u6V/6DaaCb9POmka9H3uOPaX2V8oMIxE6BzGqvBaDBeDEwk+S0GKqEWNwd3LVpXfMWcCXjfilMp53wXQdp2IRV48cAhZsMA5SmM2zijFcZLryHu2WlVIEKCsThat3gsftahwFgKjLLk8x5vVWf49suvRsDEmgzaM9aybGiQ+6+/msmBCkeDJn5Oo7TrInND0okKLUiybjSUeycjkZV/cNsOHtv5Nn4cKdp2Uu7hXbvRnmTRnk6vodoqpbMRoe6/+trb41hcRKXS1SrVboYTK4UVKPia5w8c5KplpzBaKmYSkuJebqJc5poVy3j18BG2TE/jKU3O0w6np/NykjRPiBNTLZIUObRTv2PNBve9/iYvHTzE8sFBJitltAjbjx3nj598loKnE8sfxwJIKjZw1eD4WtyRpvuuveb2iFOqK2aWFKdEnFV2iG4+DHn58BFuPG1F5BFSJatYEgYLeT5++irGiiVeOzbNnvk5mjZqblLaEe3sQCBRTKC1omENA/kcVsG8CRku5F2zpGXf3Dx/v3Ure+fm0aL42nOb2DozTdH3ok4RHSPbmAltrxbTEH+jQFc+cu3tcRtZ7wRCNocmAkagmPN4fWaGw/UGH1x6MqGTAkm5xhgrnLdonJtXr2LV0BCihLkwoGZC5m1IkxArUUDleZrjQYtT+/t5/KYbOGd8nP84Ns3GGz/KUKFAzYY8duNHscBXNm3iwe072FE9TjkXEW8lRZwryKDirHZMfFoNBU901B0htrO58ETN8JYWMFIq8HdbtzJeLvGlc88htNatJxmsHlpLfz7Hzaev4ubTVzHbbLF/bp7D9RqBNVQ8n0XlMo0w4MafPMRcq8Whep0jzTotLHvn56kGLUKBvfNzHA9aDBVy9OdyhMZEAVLHuwsWk6QBpKP7zSalOpn8x7+34go40tGUn0k2WpvBqZH6WrTSTDebfP6ss/kf556bEKylu5fUuDqBkoVb5W5/4Vm+vvklFhVKNMIWSoSRYpE9s3P0ez6lfI49c/P0eR5hqmna2s66Y7t2IdhUYbYdE2glqL5SDqNJWeE4QRLnBkkQoSQqQRJvGwzDhRx/vuVlPvvkRmaaTdeSarOJTRfDK4cajbWEbhhrablE6OqhYfK+h9ZCC/ibyzfw3HU3csboCHe9/0qe+sj1TPVXaIpFx++t6EiJOy8Qex7n8iSNPVTkXbypcpmjjQa+6IRjUY1fso1y0m6aSqeaQ5dSHC7muW/XDrbMzPDlc85jw+RJGV+dPmEiC3SkKRHOHBoin/MINOAp9jXq7JydZc5adtfmKVV96hjEi0ptqWJfj55mSaqS0i5yISIENmSyXEZ+99nH7fe2v0m/9pPOq3dx4iVFSBxqCp4oamGIRbjm5KX89srVnDM80qUGvaxMrK+1IOCKH/8re+bmyCtNwxg8UYQ2yv8bJ8a+tDvb0o2dSUMLvRtdo2y0MBu0uHnZCrz145Pcvest12N3glNLHQdcbKo7Oz6m0sSQ8zRK4L5d2/jR3t1cPjHJR5Ys5ZKxCRYVi122oQ2lo0/F9/E8D+PUMO9q/l6qCaOdDZKUkUsFryrNBNtusY+nOhd56fgkcqhWs1f87EGO1Gv4GaFPN0j1POGSiQY72ec5Xa8GARbFRKnMGf2DnD04zGl9/UyVSgz4Pp4rux9rNdk7P88j+/fwoz27yas42LLvIJGSrfa45obOIz6xdMTodTRf5OENH8YbLRS46qST+eabv2TCzyVpbelsrOmuFmR+lo4m29BpYH/OR0RRbTXYeHAvPz2wBxGFpyIxVq4zJLCGVmjQAhVPJ00atlfTdqplSrq6G9MNXVk2IeApzbFmi09MLWG0UECMtXbXXJX1jz2INSaK6jq7w6SzidJmGNNOu9vOrpeMZ04bQmNjvbfter1zZ2Gy89LjsITtecoslddKJMe2T4ZlzjJqER674iqWVCooYy1Ly318ceVZHDUtPE+33Z2Lo+Nwsu0aVabaqlLhrNJR3r0TVlsVIchALIEQNWbHrlTASCQ1obLZSq7OBlDZmqVk3i9BsvH1jnfwPc3RoMEfrVzDyZVKhEuMtTYGKJ/a9Dg/2L+TRX4x6u/Jto519BxI94GwFF6yPQ/a0NW+lp4pC+1xZ7O6pJWtuzYsdDRiW/CVcLBZ45qJk/mHC65ou2abOjk522ry6y9u5GdH9zPuF6LiZKdP6WqkTPcnJGfx3vGIbKd4ZsvWnYywPcrfcuI1rHOurilrOmhw2eAEd1+wgUou13bjXQcng4Df3/Icd+/fQb/yKCjtqrT2xOd7Y1thhXcmn+xR0xMw+F2cKMyerXSKrlz0qEUYyeVZNzjGn55+HmXPz3SdLnh09ru7t/H17VvYVZ+lrDRFpRBnIHtLRXf8YDv7Am32d3mXgKvnXOkMUWxUB3TPbxjDsaDBpYPj/PcVZ3H56GSydiZs6jo87SYphCONBnft3c79B3fy1nyVpg3xRPCdRc+U1btcpc2IcboDxWB7noS1C56wtR1tbp19w5HnCNx3TimW5yv82sTJfHJqBcP5fLQm3W22suDx+VREVw9Dnp0+zOPHDvLi7FF21Wc5FjRdo5IsKL62sxvUpl7A0iUnaQNnO7Qk82gX6sdzPYQBP88phQrn9A2zfmicCwZGXZaITEtP5+f/A5HxVY5jIckcAAAAAElFTkSuQmCC">IPv6 白名单同步</h1>
    <span class="spacer"></span>
    <span class="who" id="who"></span>
    <button id="themeBtn" title="切换界面主题：跟随系统 → 深色 → 浅色">🌗 跟随系统</button>
    <button id="logoutBtn">退出</button>
  </header>

  <div class="pills" id="pills"></div>

  <nav class="tabs" role="tablist">
    <button class="tab active" id="tabbtn-main" data-tab="main">同步与设置</button>
    <button class="tab" id="tabbtn-devices" data-tab="devices">在线设备</button>
    <button class="tab" id="tabbtn-notify" data-tab="notify">通知设置</button>
    <button class="tab" id="tabbtn-status" data-tab="status">运行状态</button>
    <button class="tab" id="tabbtn-about" data-tab="about">关于</button>
  </nav>

  <div id="tab-main">
  <div class="card">
    <h2>快捷操作</h2>
    <p class="desc">这些按钮会真的去连路由器；「立即同步一次」等于把轮询周期提前触发一轮。
      执行结果在「运行状态」页的「操作结果」栏查看。</p>
    <div class="toolbar">
      <button id="btnSync" class="primary">立即同步一次</button>
      <button id="btnRefresh">刷新页面数据</button>
    </div>
  </div>

  <div id="settings"></div>

  <div class="card">
    <h2>路由器ipv6白名单<button id="btnWl" class="primary hbtn">读取白名单</button></h2>
    <p class="desc" id="wlDesc">点右上「读取白名单」获取；只读，不会改动。</p>
    <div id="wlBox"></div>
  </div>
  </div><!-- /tab-main -->

  <div id="tab-devices" class="hide">
    <div class="card">
      <h2>在线设备<button id="btnHosts" class="primary hbtn">刷新设备列表</button></h2>
      <p class="desc" id="hostsDesc">进入页面已自动加载；点右上「刷新设备列表」重新获取，把 NAS 的 MAC 填进
        「目标设备 MAC」即可精确定位。</p>
      <div id="hostsBox"></div>
    </div>
  </div><!-- /tab-devices -->

  <div id="tab-notify" class="hide">
    <div id="notifySettings"></div>
  </div><!-- /tab-notify -->

  <div id="tab-status" class="hide">
    <div class="card">
      <h2>运行状态</h2>
      <p class="desc">同步引擎的内部快照（每 5 秒自动刷新），排障时把这段 JSON 原样发出来即可定位问题。
        上面那排状态胶囊是它的摘要版。</p>
      <pre class="out" id="state"></pre>
    </div>
    <div class="card">
      <h2>操作结果</h2>
      <p class="desc">各页操作按钮（立即同步 / 测试连接 / 读取白名单 / 刷新设备列表等）的返回结果都汇总在这里，需要时再来查看。</p>
      <pre class="out" id="out">（结果会显示在这里）</pre>
    </div>
  </div><!-- /tab-status -->

  <div id="tab-about" class="hide">
    <div class="card">
      <h2>IPv6 白名单同步 <span class="n" id="aboutVersion"></span></h2>
      <p class="desc">让 NAS 的公网 IPv6 地址在变化后依然可访问：自动发现本机最新的
        IPv6 地址，同步写入路由器的 IPv6 防火墙白名单，并按需放行指定端口，
        外网直连不再受地址变化影响。</p>
      <ul class="desc">
        <li><b>自动发现</b> —— 从本机接口或路由器记录自动获取当前 IPv6，地址变了自动更新</li>
        <li><b>白名单同步</b> —— 按设备 MAC 定位，自动写条目、清理过期条目</li>
        <li><b>多端口放行</b> —— 一次放行多个端口（逗号分隔），不用逐个去路由器设置</li>
        <li><b>网页控制台</b> —— 所有配置网页可改、持久化保存，改完即时生效</li>
        <li><b>变更通知</b> —— 支持 ntfy / gotify / 企业微信，防火墙每次被修改自动推送</li>
        <li><b>演练模式</b> —— 只看会改什么、不真写，先确认再应用</li>
      </ul>
    </div>
    <div class="card">
      <h2>作者</h2>
      <p class="desc">Aaron_Z</p>
    </div>
  </div><!-- /tab-about -->
</div>

<div class="foot" id="foot">
  <span class="hint" id="hint"></span>
  <button id="btnReset">放弃修改</button>
  <button id="btnSave" class="primary">保存并应用</button>
</div>

<div id="toast"></div>

<script>
(function () {
  "use strict";
  var VERSION = "__APP_VERSION__";
  var state = { user: "", settings: [], overview: {}, initial: {} };
  var els = {
    login: document.getElementById("login"),
    app: document.getElementById("app"),
    form: document.getElementById("loginForm"),
    u: document.getElementById("u"),
    p: document.getElementById("p"),
    loginMsg: document.getElementById("loginMsg"),
    loginBtn: document.getElementById("loginBtn"),
    who: document.getElementById("who"),
    pills: document.getElementById("pills"),
    out: document.getElementById("out"),
    stateBox: document.getElementById("state"),
    settings: document.getElementById("settings"),
    hint: document.getElementById("hint"),
    toast: document.getElementById("toast"),
    wlDesc: document.getElementById("wlDesc"),
    wlBox: document.getElementById("wlBox")
  };

  // ---------- 主题切换（跟随系统 / 深色 / 浅色，记忆在浏览器本地） ----------
  var THEME_KEY = "ipv6sync-theme";
  var THEMES = [["auto", "🌗", "跟随系统"], ["dark", "🌙", "深色"], ["light", "☀️", "浅色"]];
  function currentTheme() {
    try { return localStorage.getItem(THEME_KEY) || "auto"; } catch (e) { return "auto"; }
  }
  function applyTheme() {
    var t = currentTheme();
    var root = document.documentElement;
    if (t === "dark") { root.setAttribute("data-theme", "dark"); }
    else if (t === "light") { root.setAttribute("data-theme", "light"); }
    else { root.removeAttribute("data-theme"); }
    var i = 0;
    for (var k = 0; k < THEMES.length; k++) { if (THEMES[k][0] === t) { i = k; } }
    var btn = document.getElementById("themeBtn");
    if (btn) { btn.textContent = THEMES[i][1] + " " + THEMES[i][2]; }
  }
  document.getElementById("themeBtn").addEventListener("click", function () {
    var next = { auto: "dark", dark: "light", light: "auto" }[currentTheme()] || "auto";
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    applyTheme();
  });
  applyTheme();

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "Accept": "application/json" }, opts.headers || {});
    if (opts.body) { opts.headers["Content-Type"] = "application/json"; }
    opts.credentials = "same-origin";
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return { error: "HTTP " + r.status }; })
        .then(function (j) {
          if (!r.ok) { j.http = r.status; }
          return j;
        });
    });
  }

  function toast(text, kind) {
    var d = document.createElement("div");
    d.className = "toast" + (kind ? " " + kind : "");
    d.textContent = text;
    els.toast.appendChild(d);
    setTimeout(function () { if (d.parentNode) { d.parentNode.removeChild(d); } },
      kind === "err" ? 9000 : 4000);
  }

  function showOut(title, obj) {
    var text = typeof obj === "string" ? obj : JSON.stringify(obj, null, 1);
    els.out.textContent = "[" + new Date().toLocaleTimeString() + "] " + title + "\n" + text;
  }

  // ---------- 登录 ----------
  // 勾选「保持登录(7天)」由后端签发 7 天有效期的会话 Cookie（HttpOnly），
  // 浏览器本地不再保存任何密码明文；顺手清掉旧版本遗留的明文记录。
  try { localStorage.removeItem("ipv6sync-remember"); } catch (e) {}
  els.form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    els.loginMsg.className = "msg";
    els.loginBtn.disabled = true;
    api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        user: els.u.value,
        password: els.p.value,
        remember: document.getElementById("rememberChk").checked
      })
    }).then(function (j) {
      els.loginBtn.disabled = false;
      if (j.ok) {
        els.p.value = "";
        start(j.user); return;
      }
      els.loginMsg.textContent = j.error || "登录失败";
      els.loginMsg.className = "msg err";
    }).catch(function (e) {
      els.loginBtn.disabled = false;
      els.loginMsg.textContent = "请求失败：" + e;
      els.loginMsg.className = "msg err";
    });
  });

  function logout() {
    api("/api/logout", { method: "POST", body: "{}" }).then(function () {
      els.app.classList.add("hide");
      els.login.classList.remove("hide");
      els.form.classList.remove("hide");
      document.getElementById("foot").classList.add("hide");
      els.loginMsg.className = "msg";
    });
  }
  document.getElementById("logoutBtn").addEventListener("click", logout);

  // ---------- 渲染 ----------
  // 字段来源徽标（默认值/网页设置）已移除：满屏标签干扰阅读，需要看来源去「关于」页。

  function fieldInput(f) {
    var wrap = document.createElement("div");
    wrap.className = "f";
    wrap.dataset.key = f.key;

    var lab = document.createElement("div");
    lab.className = "lab";
    var span = document.createElement("span");
    span.textContent = f.label;
    lab.appendChild(span);
    wrap.appendChild(lab);

    var input;
    if (f.kind === "select") {
      input = document.createElement("select");
      (f.choices || []).forEach(function (c) {
        var o = document.createElement("option");
        o.value = c; o.textContent = c;
        input.appendChild(o);
      });
      input.value = f.value;
      wrap.appendChild(input);
    } else if (f.kind === "bool") {
      var sw = document.createElement("div");
      sw.className = "switch";
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!f.value;
      var on = document.createElement("span");
      on.textContent = f.value ? "已启用" : "已关闭";
      input.addEventListener("change", function () {
        on.textContent = input.checked ? "已启用" : "已关闭";
        markDirty();
      });
      sw.appendChild(input); sw.appendChild(on);
      wrap.appendChild(sw);
    } else {
      input = document.createElement("input");
      input.type = (f.kind === "secret") ? "password"
        : ((f.kind === "int" || f.kind === "float") ? "text" : "text");
      if (f.kind === "int" || f.kind === "float") { input.inputMode = "numeric"; }
      input.value = f.value === null || f.value === undefined ? "" : f.value;
      if (f.placeholder) { input.placeholder = f.placeholder; }
      if (f.min !== undefined) { input.dataset.min = f.min; }
      if (f.max !== undefined) { input.dataset.max = f.max; }
      wrap.appendChild(input);
    }
    input.dataset.kind = f.kind;
    input.addEventListener("input", markDirty);
    input.addEventListener("change", markDirty);

    if (f.help) {
      var h = document.createElement("div");
      h.className = "help";
      h.textContent = f.kind === "secret" && f.is_set ? "已配置密码。" + f.help : f.help;
      wrap.appendChild(h);
    }
    wrap._input = input;
    return wrap;
  }

  function testBtn() {
    // 「测试连接」归属「路由器连接」组标题行（右对齐），动态创建一次、随渲染搬移
    var b = document.getElementById("btnTest");
    if (!b) {
      b = document.createElement("button");
      b.id = "btnTest";
      b.className = "primary hbtn";
      b.textContent = "测试连接";
      b.addEventListener("click", function () {
        action("btnTest", "测试连接", "POST", "/api/router/test");
      });
    }
    return b;
  }

  function notifyTestBtn() {
    // 「发送测试通知」归属「通知设置」组标题行（右对齐），动态创建一次、随渲染搬移
    var b = document.getElementById("btnNotifyTest");
    if (!b) {
      b = document.createElement("button");
      b.id = "btnNotifyTest";
      b.className = "primary hbtn";
      b.textContent = "发送测试通知";
      b.addEventListener("click", function () {
        action("btnNotifyTest", "测试通知", "POST", "/api/notify/test");
      });
    }
    return b;
  }

  var NOTIFY_CHANNELS = [
    { label: "ntfy", rows: [["NTFY_ENABLED"], ["NTFY_URL", "NTFY_TOKEN"]] },
    { label: "gotify", rows: [["GOTIFY_ENABLED"], ["GOTIFY_URL", "GOTIFY_TOKEN"]] },
    { label: "企业微信群机器人", rows: [["WECOM_ENABLED"], ["WECOM_ID"]] }
  ];

  function renderNotify(fields, card) {
    // 通知设置专属布局：标题独占一行；三个渠道各一个带边框栏位，
    // 栏内按 rows 分行，一行两个字段（如 服务器地址 + Token）时并排。
    var byKey = {};
    fields.forEach(function (f) { byKey[f.key] = f; });
    var t = byKey["NOTIFY_TITLE"];
    if (t) {
      var trow = document.createElement("div");
      trow.className = "fullrow";
      trow.appendChild(fieldInput(t));
      card.appendChild(trow);
    }
    NOTIFY_CHANNELS.forEach(function (ch) {
      var box = document.createElement("div");
      box.className = "chbox";
      var cap = document.createElement("div");
      cap.className = "t";
      cap.textContent = ch.label;
      box.appendChild(cap);
      ch.rows.forEach(function (row) {
        if (row.length === 1) {
          if (byKey[row[0]]) { box.appendChild(fieldInput(byKey[row[0]])); }
          return;
        }
        var grid = document.createElement("div");
        grid.className = "row2";
        row.forEach(function (k) {
          if (byKey[k]) { grid.appendChild(fieldInput(byKey[k])); }
        });
        box.appendChild(grid);
      });
      card.appendChild(box);
    });
  }

  function renderSettings(groups) {
    els.settings.textContent = "";
    var nbox = document.getElementById("notifySettings");
    nbox.textContent = "";
    groups.forEach(function (g) {
      if (g.group === "通知设置") {
        var ncard = document.createElement("div");
        ncard.className = "card";
        var nh = document.createElement("h2");
        nh.textContent = g.group;
        nh.appendChild(notifyTestBtn());
        ncard.appendChild(nh);
        renderNotify(g.fields, ncard);
        nbox.appendChild(ncard);
        return;
      }
      var card = document.createElement("div");
      card.className = "card";
      var h = document.createElement("h2");
      h.textContent = g.group;
      if (g.group === "路由器连接") { h.appendChild(testBtn()); }
      card.appendChild(h);
      var grid = document.createElement("div");
      grid.className = "grid";
      g.fields.forEach(function (f) { grid.appendChild(fieldInput(f)); });
      card.appendChild(grid);
      els.settings.appendChild(card);
    });
  }

  function collect() {
    var patch = {};
    // 设置表单分布在「同步与设置」和「通知设置」两个 tab 容器里
    var nodes = document.querySelectorAll("#settings .f, #notifySettings .f");
    for (var i = 0; i < nodes.length; i++) {
      var f = nodes[i], input = f._input, key = f.dataset.key;
      var kind = input.dataset.kind;
      var val;
      if (kind === "bool") { val = input.checked; }
      else { val = input.value; }
      var before = state.initial[key];
      var same = (kind === "bool") ? (!!val === !!before) : (String(val) === String(before));
      if (!same) { patch[key] = val; }
    }
    return patch;
  }

  function markDirty() {
    var n = Object.keys(collect()).length;
    els.hint.textContent = n ? ("有 " + n + " 项改动未保存") : "没有未保存的改动";
    document.getElementById("btnSave").disabled = !n;
  }

  function renderPills(overview, st) {
    els.pills.textContent = "";
    var c = st.counters || {};
    var ok = !!st.ok;
    var unconf = !!st.unconfigured;
    var fw = st.firewall_ipv6_enabled;
    var nAddr = 0;
    var rules = st.rules || {};
    Object.keys(rules).forEach(function (k) {
      nAddr += ((rules[k] || {}).addrs || []).length;
    });
    // 「同步」这一格最容易误读：ok=false 既可能是「第一轮还没跑完」，
    // 也可能是「最近一轮失败了」，也可能只是「密码还没填」。分开说。
    var syncText, syncDot;
    if (st.consecutive_failures > 0) {
      syncText = "失败 " + st.consecutive_failures + " 次"; syncDot = "err";
    } else if (unconf) {
      syncText = "待配置"; syncDot = "warn";
    } else if (ok) {
      syncText = "正常"; syncDot = "on";
    } else {
      syncText = st.last_tick_at ? "上一轮异常" : "等待首轮"; syncDot = "warn";
    }
    // 累计写入是「跨容器重启」的（存 /data/state.json）；本次启动单独算。
    // 两个都显示，用户才能判断"到底是没写，还是容器刚重启过"。
    var total = c.total_writes || 0;
    var items = [
      ["同步", syncText, syncDot,
        st.last_error ? ("最近错误：" + st.last_error)
                      : ("最近一轮成功于 " + (st.last_success_at || "（尚未成功过）"))],
      ["路由器登录", st.logged_in ? "已登录" : "未登录", st.logged_in ? "on" : "off"],
      ["IPv6 防火墙", (fw === null || fw === undefined) ? "未知" : (fw ? "已开启" : "已关闭"),
        (fw === null || fw === undefined) ? "off" : (fw ? "on" : "warn")],
      ["采用地址", nAddr + " 个", nAddr ? "on" : "off"],
      ["最近一轮", st.last_tick_at || "尚未运行", "off",
        "最近一次尝试的时间（不论成功失败）"],
      ["累计写入", total + " 次", total ? "on" : "off",
        "跨容器重启累计（存 " + (c.state_file || "/data/state.json") + "）：新增 "
        + (c.created || 0) + " / 更新 " + (c.updated || 0) + " / 删除 "
        + (c.removed || 0)
        + "\n本次启动以来：" + (c.session_writes || 0) + " 次"
        + (c.first_write_at ? "\n最早写入：" + c.first_write_at : "")
        + (c.last_write_at ? "\n最近写入：" + c.last_write_at : "")],
      ["本次启动", st.started_at || "—", "off",
        "容器每次重启都会重新计时；「累计写入」不受重启影响"]
    ];
    items.forEach(function (it) {
      var p = document.createElement("span");
      p.className = "pill";
      if (it[3]) { p.title = it[3]; }
      var d = document.createElement("span");
      d.className = "dot " + it[2];
      var t = document.createElement("span");
      t.appendChild(document.createTextNode(it[0] + " "));
      var b = document.createElement("b");
      b.textContent = it[1];
      t.appendChild(b);
      p.appendChild(d); p.appendChild(t);
      els.pills.appendChild(p);
    });
    if (c.state_file_error) {
      var pe = document.createElement("span");
      pe.className = "pill";
      pe.title = c.state_file_error;
      var de = document.createElement("span");
      de.className = "dot err";
      var te = document.createElement("span");
      te.appendChild(document.createTextNode("计数文件异常 "));
      var be = document.createElement("b");
      be.textContent = "统计不可信";
      te.appendChild(be);
      pe.appendChild(de); pe.appendChild(te);
      els.pills.appendChild(pe);
    }
    if (st.dry_run) {
      var w = document.createElement("span");
      w.className = "pill";
      w.textContent = "演练模式：不会真的写入";
      els.pills.appendChild(w);
    }
  }

  // 状态里带的规则名（后端 st.rules 的键），用来在白名单里标出"这条归谁管"
  var ruleNames = [];

  function escapeRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  // 与后端 trustlist.managed_pattern 保持一致：基础名、基础名@N，各自可再带 -端口。
  function ownerOfName(name) {
    for (var i = 0; i < ruleNames.length; i++) {
      var rn = ruleNames[i];
      if (!rn) { continue; }
      var rx = new RegExp("^" + escapeRe(rn) + "(@\\d+)?(-\\d+)?$");
      if (rx.test(name)) { return rn; }
    }
    return "";
  }

  function renderState(overview, st) {
    els.stateBox.textContent = JSON.stringify(st, null, 1);
    els.who.textContent = state.user;
    var uv = document.getElementById("aboutVersion");
    if (uv) { uv.textContent = "v" + VERSION; }
    // 白名单表靠它把条目名（NAS / NAS@2 / NAS-5005…）认回是哪条规则在管
    ruleNames = Object.keys(st.rules || {});
  }

  // ---------- 数据加载 ----------
  function load(banner) {
    return api("/api/state").then(function (j) {
      if (!j.authed) { logout(); return; }
      state.user = j.user;
      state.settings = j.settings;
      state.overview = j.overview;
      state.initial = {};
      j.settings.forEach(function (g) {
        g.fields.forEach(function (f) {
          state.initial[f.key] = (f.kind === "bool") ? !!f.value : String(f.value);
        });
      });
      renderSettings(j.settings);
      renderPills(j.overview, j.status);
      renderState(j.overview, j.status);
      markDirty();
      if (banner && j.status.last_error) { showOut("上轮错误", j.status.last_error); }
    });
  }

  function start(user) {
    state.user = user || "admin";
    els.login.classList.add("hide");
    els.app.classList.remove("hide");
    document.getElementById("foot").classList.remove("hide");
    load(false).then(function () { toast("已登录", "ok"); })
      .catch(function (e) { toast("加载失败：" + e, "err"); });
  }

  function action(btnId, title, method, path, body) {
    var btn = document.getElementById(btnId);
    btn.disabled = true;
    api(path, { method: method || "POST", body: JSON.stringify(body || {}) })
      .then(function (j) {
        showOut(title, j);
        if (j.ok === false && j.error) { toast(title + "：" + j.error, "err"); }
        else { toast(title + "：完成", "ok"); }
        if (path === "/api/settings" && j.ok) {
          state.settings = j.settings;
          state.overview = j.overview;
          state.initial = {};
          j.settings.forEach(function (g) {
            g.fields.forEach(function (f) {
              state.initial[f.key] = (f.kind === "bool") ? !!f.value : String(f.value);
            });
          });
          renderSettings(j.settings);
          markDirty();
          (j.notes || []).forEach(function (n) { toast(n, "ok"); });
        }
        return load(false);
      })
      .catch(function (e) { showOut(title, "请求失败：" + e); toast(title + " 失败", "err"); })
      .then(function () { btn.disabled = false; });
  }

  document.getElementById("btnSync").addEventListener("click", function () {
    action("btnSync", "立即同步", "POST", "/api/sync");
  });
  // ---------- 选项卡 ----------
  var hostsLoaded = false;
  function switchTab(name) {
    ["main", "devices", "notify", "status", "about"].forEach(function (t) {
      document.getElementById("tab-" + t).classList.toggle("hide", t !== name);
      document.getElementById("tabbtn-" + t).classList.toggle("active", t === name);
    });
    // 保存栏属于「同步与设置」和「通知设置」两页
    document.getElementById("foot").classList.toggle("hide",
      name !== "main" && name !== "notify");
    if (name === "devices" && !hostsLoaded) { loadHosts(); }   // 首次打开自动拉一次
  }
  document.getElementById("tabbtn-main").addEventListener("click", function () { switchTab("main"); });
  document.getElementById("tabbtn-devices").addEventListener("click", function () { switchTab("devices"); });
  document.getElementById("tabbtn-notify").addEventListener("click", function () { switchTab("notify"); });
  document.getElementById("tabbtn-status").addEventListener("click", function () { switchTab("status"); });
  document.getElementById("tabbtn-about").addEventListener("click", function () { switchTab("about"); });

  function loadHosts() {
    var btn = document.getElementById("btnHosts");
    btn.disabled = true;
    return api("/api/router/hosts").then(function (j) {
      showOut("设备列表", j);
      if (!j.ok) { toast("刷新设备列表失败：" + (j.error || ""), "err"); return; }
      hostsLoaded = true;
      var box = document.getElementById("hostsBox");
      box.textContent = "";
      var t = document.createElement("table");
      var head = document.createElement("tr");
      ["MAC（填 TARGET_MAC）", "主机名", "IPv4", "当前 IPv6"].forEach(function (h) {
        var th = document.createElement("th"); th.textContent = h; head.appendChild(th);
      });
      t.appendChild(head);
      j.hosts.forEach(function (h) {
        var tr = document.createElement("tr");
        [h.mac, h.name, h.ipv4, (h.ipv6 || []).join(", ") || "（无全局 IPv6）"]
          .forEach(function (v) {
            var td = document.createElement("td");
            td.textContent = v;
            if (v === h.mac) { td.className = "mono"; }
            tr.appendChild(td);
          });
        t.appendChild(tr);
      });
      box.appendChild(t);
      document.getElementById("hostsDesc").textContent = "共 " + j.hosts.length +
        " 台设备；当前 LAN 前缀：" + ((j.lan_prefixes || []).join(", ") || "（无）");
    }).catch(function (e) { showOut("设备列表", "请求失败：" + e); })
      .then(function () { btn.disabled = false; });
  }
  document.getElementById("btnHosts").addEventListener("click", function () { loadHosts(); });
  document.getElementById("btnWl").addEventListener("click", function () {
    var btn = document.getElementById("btnWl");
    btn.disabled = true;
    api("/api/router/whitelist").then(function (j) {
      showOut("白名单", j);
      if (!j.ok) { toast("读取白名单失败：" + (j.error || ""), "err"); return; }
      els.wlDesc.textContent = "IPv6 防火墙总开关：" + (j.enabled ? "已开启" : "已关闭") +
        "（白名单只在开启时生效）　条目 " + j.entries.length + "/" + j.max;
      var box = document.getElementById("wlBox");
      box.textContent = "";
      // 固件会在条目里附带 devName（按 LocalIp 反查设备列表得到），有就显示
      var hasDev = j.entries.some(function (e) { return e.devName; });
      var nManaged = j.entries.filter(function (e) {
        return ownerOfName(e.Name || "");
      }).length;
      els.wlDesc.textContent += "　其中 " + nManaged +
        " 条由本程序自动维护（其余不动）";
      var t = document.createElement("table");
      var head = document.createElement("tr");
      ["名称", "归属规则", "内网 IPv6", "允许来源", "端口"]
        .concat(hasDev ? ["设备"] : [])
        .forEach(function (h) {
          var th = document.createElement("th"); th.textContent = h; head.appendChild(th);
        });
      t.appendChild(head);
      j.entries.forEach(function (e) {
        var tr = document.createElement("tr");
        var owner = ownerOfName(e.Name || "");
        // 第 1 列（index 1）是「归属规则」，不是原始字段，用 null 占位
        var vals = [e.Name, null, e.LocalIp, e.RemoteIp, e.Port]
          .concat(hasDev ? [e.devName || "（未识别）"] : []);
        vals.forEach(function (v, i) {
          var td = document.createElement("td");
          if (i === 1) {
            if (owner) {
              td.textContent = owner + "（自动维护）";
            } else {
              td.textContent = "用户手工添加";
              td.style.color = "var(--muted)";
            }
          } else {
            td.textContent = (v === null || v === undefined) ? "" : v;
          }
          tr.appendChild(td);
        });
        t.appendChild(tr);
      });
      box.appendChild(t);
      var bar = document.createElement("div");
      bar.className = "toolbar";
      bar.style.marginTop = "12px";
      var b = document.createElement("button");
      b.textContent = j.enabled ? "关闭 IPv6 防火墙总开关" : "打开 IPv6 防火墙总开关";
      b.addEventListener("click", function () {
        b.disabled = true;
        api("/api/router/firewall", {
          method: "POST",
          body: JSON.stringify({ enable: !j.enabled })
        }).then(function (r2) {
          showOut("防火墙开关", r2);
          toast(r2.ok ? ("已" + (r2.enabled ? "开启" : "关闭")) : ("失败：" + r2.error),
            r2.ok ? "ok" : "err");
          return load(false);
        }).then(function () { b.disabled = false; });
      });
      bar.appendChild(b);
      box.appendChild(bar);
    }).catch(function (e) { showOut("白名单", "请求失败：" + e); })
      .then(function () { btn.disabled = false; });
  });
  document.getElementById("btnRefresh").addEventListener("click", function () {
    load(false).then(function () { toast("已刷新", "ok"); });
  });
  document.getElementById("btnSave").addEventListener("click", function () {
    var patch = collect();
    if (!Object.keys(patch).length) { toast("没有改动", "err"); return; }
    action("btnSave", "保存设置", "POST", "/api/settings", patch);
  });
  document.getElementById("btnReset").addEventListener("click", function () {
    load(false).then(function () { toast("已放弃未保存的改动", "ok"); });
  });

  // 5 秒轮询一次状态（只刷状态，不重绘表单以免打断正在输入的内容）
  setInterval(function () {
    if (els.app.classList.contains("hide")) { return; }
    api("/api/state").then(function (j) {
      if (j.authed) { renderPills(j.overview, j.status); renderState(j.overview, j.status); }
    }).catch(function () {});
  }, 5000);

  // ---------- 启动 ----------
  api("/api/session").then(function (j) {
    if (j.authed) { start(j.user); }
  }).catch(function () {});
})();
</script>
</body>
</html>
"""
