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
  --btn-line:#e3e6ea; --radius:10px;
}
/* 手动深色：页面右上角切换按钮设置 data-theme="dark" 时强制深色 */
:root[data-theme="dark"]{
  --bg:#16181d; --card:#1e2126; --line:#2e3238; --text:#e8eaed;
  --muted:#9aa1ab; --accent:#6ea8fe; --accent-weak:#1d2735;
  --ok:#4ec9a0; --ok-bg:#16261f; --warn:#e3b341; --warn-bg:#2a2416;
  --err:#f0837a; --err-bg:#2b1b1a;
  --btn-line:#464d57;
}
/* 自动模式：跟随系统，但用户手动选了浅色时不覆盖 */
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#16181d; --card:#1e2126; --line:#2e3238; --text:#e8eaed;
    --muted:#9aa1ab; --accent:#6ea8fe; --accent-weak:#1d2735;
    --ok:#4ec9a0; --ok-bg:#16261f; --warn:#e3b341; --warn-bg:#2a2416;
    --err:#f0837a; --err-bg:#2b1b1a;
    --btn-line:#464d57;
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
  border:1px solid var(--btn-line);background:var(--card);color:var(--text);cursor:pointer}
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
.card h2 .hbtns{margin-left:auto;display:flex;gap:8px}
.card h2 .hbtns .hbtn{margin-left:0}
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
.fwtoggle{margin-left:auto;display:flex;align-items:center;gap:8px;padding:5px 10px;
  border:1px solid var(--line);border-radius:8px;background:var(--card)}
.fwtoggle>span{font-size:12.5px;color:var(--muted)}
.fwtoggle>b{font-size:12.5px}
.swbtn{position:relative;width:42px;height:23px;border-radius:12px;background:#c0c4ca;
  border:none;cursor:pointer;padding:0;transition:background .15s}
.swbtn::after{content:"";position:absolute;top:2px;left:2px;width:19px;height:19px;
  border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:left .15s}
.swbtn.on{background:var(--ok)}
.swbtn.on::after{left:21px}
.swbtn:disabled{opacity:.6;cursor:wait}
pre.out{background:var(--bg);border:1px solid var(--line);border-radius:8px;
  padding:12px;font-size:12px;line-height:1.55;max-height:300px;overflow:auto;
  white-space:pre-wrap;word-break:break-all;margin:0;font-family:ui-monospace,
  SFMono-Regular,Menlo,Consolas,monospace}
/* 表格套一层横向滚动容器：手机上窄屏放不下时整体横滑，不换行、不撑出卡片 */
.tblwrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
/* 同步记录页 */
.evq{flex:1;min-width:150px;padding:7px 10px;font-size:12.5px}
#evAct,#evMax{width:auto;padding:7px 8px;font-size:12.5px}
.evbadge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px}
.ev-new{background:var(--ok-bg);color:var(--ok)}
.ev-upd{background:var(--accent-weak);color:var(--accent)}
.ev-del{background:var(--err-bg);color:var(--err)}
.ev-fw{background:var(--warn-bg);color:var(--warn)}
.evold{color:var(--muted)}
table{border-collapse:collapse;font-size:12.5px;min-width:100%}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);
  vertical-align:top;white-space:nowrap}
th{color:var(--muted);font-weight:500}
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
/* 设备列表页内的二级页签（在线/离线/黑名单/儿童上网） */
.subtabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:14px;flex-wrap:wrap}
.subtab{background:transparent;border:none;border-bottom:2px solid transparent;border-radius:0;
  padding:7px 12px;font-size:12.5px;color:var(--muted);cursor:pointer}
.subtab:hover{color:var(--accent)}
.subtab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.subtab .cnt{display:inline-block;min-width:16px;text-align:center;font-size:11px;border-radius:9px;
  padding:0 5px;margin-left:3px;background:var(--bg);color:var(--muted);border:1px solid var(--line)}
.subtab.active .cnt{color:var(--accent);border-color:var(--accent)}
tr.offrow td{opacity:.62}
.devnote{color:var(--muted);font-size:12px;margin:10px 0 0}
/* 白名单徽标 + 设备选址弹窗 */
.badge{display:inline-block;font-size:11px;padding:0 7px;border-radius:9px;margin-left:5px;
  background:var(--ok-bg);color:var(--ok);vertical-align:1px}
.wlman{color:var(--muted);font-size:11px;margin-left:5px}
#dlgMask{position:fixed;inset:0;background:rgba(15,18,24,.45);display:flex;
  align-items:center;justify-content:center;z-index:50;padding:16px}
.dlg{background:var(--card);border:1px solid var(--line);border-radius:12px;
  width:420px;max-width:100%;padding:18px 22px 16px;max-height:92vh;overflow:auto}
.dlg h3{margin:0 0 6px;font-size:14.5px;text-align:center}
.dlg .flab{display:block;font-size:12.5px;color:var(--muted);margin:11px 0 4px}
.dlg input,.dlg select{width:100%;font-size:13.5px;padding:8px 10px}
.dlgbtns{display:flex;justify-content:center;gap:12px;margin-top:16px}
.dlgbtns button{min-width:96px}
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
    <h1><img class="logo" alt="" src="data:image/png;base64,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">IPv6 白名单同步</h1>
    <span class="spacer"></span>
    <span class="who" id="who"></span>
    <button id="themeBtn" title="切换界面主题：跟随系统 → 深色 → 浅色">🌗 跟随系统</button>
    <button id="logoutBtn">退出</button>
  </header>

  <div class="pills" id="pills"></div>

  <nav class="tabs" role="tablist">
    <button class="tab active" id="tabbtn-main" data-tab="main">同步与设置</button>
    <button class="tab" id="tabbtn-devices" data-tab="devices">设备列表</button>
    <button class="tab" id="tabbtn-events" data-tab="events">同步记录</button>
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
      <button id="btnResetCounters">重置累计写入</button>
      <span class="fwtoggle" title="IPv6 防火墙总开关：关闭后白名单整体不生效，同步暂停">
        <span>IPv6 防火墙</span>
        <button id="fwSwitch" class="swbtn" type="button" aria-label="IPv6 防火墙总开关"></button>
        <b id="fwSwitchTxt">…</b>
      </span>
    </div>
  </div>

  <div id="settings"></div>

  <div class="card">
    <h2>IPv6 防火墙白名单<span class="hbtns">
      <button id="btnWl" class="hbtn"><svg viewBox="0 0 16 16" width="13" height="13"
        style="vertical-align:-2px" aria-hidden="true"><path fill="none"
        stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
        d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 1.8v2.7h-2.7"/></svg> 刷新</button>
      <button id="btnWlAdd" class="primary hbtn">＋ 添加</button></span></h2>
    <p class="desc" id="wlDesc">点「添加」选一台设备保存：立即写入白名单，之后每轮
      自动跟随该设备的地址变化原地更新（打「自动维护」标）。手工条目只展示，不会被动。</p>
    <div id="wlBox"></div>
  </div>
  </div><!-- /tab-main -->

  <div id="tab-devices" class="hide">
    <div class="card">
      <h2>设备列表<button id="btnHosts" class="primary hbtn"><svg viewBox="0 0 16 16" width="13" height="13"
        style="vertical-align:-2px" aria-hidden="true"><path fill="none"
        stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
        d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 1.8v2.7h-2.7"/></svg> 刷新</button></h2>
      <p class="desc" id="hostsDesc">进入页面已自动加载；点右上「刷新」重新获取。
        在线/离线/儿童上网来自路由器设备档案（HostInfo），黑名单来自 WiFi MAC 过滤配置
        （wlanfilterenhance）。在白名单卡里点「添加」选设备即可放行任意一台。</p>
      <div class="subtabs" role="tablist">
        <button class="subtab active" id="devtab-online" type="button">在线设备<span class="cnt" id="cnt-online">0</span></button>
        <button class="subtab" id="devtab-offline" type="button">离线设备<span class="cnt" id="cnt-offline">0</span></button>
        <button class="subtab" id="devtab-black" type="button">黑名单<span class="cnt" id="cnt-black">0</span></button>
        <button class="subtab" id="devtab-kids" type="button">儿童上网<span class="cnt" id="cnt-kids">0</span></button>
      </div>
      <div id="devbox-online"></div>
      <div id="devbox-offline" class="hide"></div>
      <div id="devbox-black" class="hide"></div>
      <div id="devbox-kids" class="hide"></div>
    </div>
  </div><!-- /tab-devices -->

  <div id="tab-events" class="hide">
    <div class="card">
      <h2>同步记录<button id="btnEv" class="primary hbtn"><svg viewBox="0 0 16 16" width="13" height="13"
        style="vertical-align:-2px" aria-hidden="true"><path fill="none"
        stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
        d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 1.8v2.7h-2.7"/></svg> 刷新</button></h2>
      <p class="desc">每次真正改动路由器白名单的动作都记一条（地址没变的轮次不记）；
        更新会显示 旧地址 → 新地址。记录落盘在 <span class="mono">/data/events.jsonl</span>，
        重启不丢；超出保留上限自动丢弃最旧。</p>
      <div class="toolbar" style="margin-bottom:10px">
        <input id="evQ" placeholder="搜索设备名 / MAC / 地址…" class="evq">
        <select id="evAct">
          <option value="">全部动作</option>
          <option value="created">新增</option>
          <option value="updated">更新</option>
          <option value="removed">删除</option>
          <option value="fw">开关</option>
        </select>
        <button id="btnEvClear">清空记录</button>
      </div>
      <p class="desc" style="margin:0 0 10px">保留上限
        <select id="evMax">
          <option>100</option><option>200</option><option>500</option>
        </select>
        条，改完即时生效并落盘　<span id="evMeta"></span></p>
      <div id="evBox"></div>
      <div style="text-align:center;margin-top:10px">
        <button id="btnEvMore" style="display:none">加载更多</button>
      </div>
    </div>
  </div><!-- /tab-events -->

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
      <p class="desc">各页操作按钮（立即同步 / 测试连接 / 各页「刷新」等）的返回结果都汇总在这里，需要时再来查看。</p>
      <pre class="out" id="out">（结果会显示在这里）</pre>
    </div>
  </div><!-- /tab-status -->

  <div id="tab-about" class="hide">
    <div class="card">
      <h2>IPv6 白名单同步 <span class="n" id="aboutVersion"></span></h2>
      <p class="desc">让局域网设备的公网 IPv6 访问不因地址变化而中断：自动跟踪设备的
        当前 IPv6，同步写入路由器的 IPv6 防火墙白名单并按需放行端口，
        支持同时维护 NAS 在内的多台设备，外网直连不再受地址变化影响。</p>
      <ul class="desc">
        <li><b>按设备维护</b> —— 白名单里点「添加」选设备即放行，之后地址一变自动原地更新条目</li>
        <li><b>自动取址</b> —— 绑定设备的规则取路由器设备记录的最新地址；设备离线或记录未就绪时自动跳过，不误删</li>
        <li><b>设备列表</b> —— 在线 / 离线 / 黑名单 / 儿童上网一页看全，添加放行时直接按设备选</li>
        <li><b>多端口放行</b> —— 一次放行多个端口（逗号分隔），不用逐个去路由器设置</li>
        <li><b>网页控制台</b> —— 所有配置网页可改、持久化保存，改完即时生效</li>
        <li><b>同步记录</b> —— 白名单每次真实改动都有流水可查，落盘保存重启不丢</li>
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

<div id="dlgMask" class="hide">
  <div class="dlg">
    <h3 id="dlgTitle">添加白名单条目</h3>
    <p class="desc" id="dlgHint" style="margin:0 0 4px">选设备后自动填入它的当前地址（设备地址列表第一条）。</p>
    <label class="flab">服务名称</label>
    <input type="text" id="dlgName" placeholder="留空则自动用设备名" autocomplete="off">
    <label class="flab">允许来源</label>
    <input type="text" id="dlgRemote" value="::/0" autocomplete="off">
    <label class="flab">设备名称</label>
    <select id="dlgDev"><option value="">未知设备</option></select>
    <label class="flab">本地 IP</label>
    <input type="text" id="dlgLocal" class="mono" placeholder="选设备后自动填入，也可手填" autocomplete="off">
    <label class="flab">通信端口</label>
    <input type="text" id="dlgPort" placeholder="-1 = 全部端口，多个用逗号分隔" autocomplete="off">
    <div class="msg" id="dlgMsg"></div>
    <div class="dlgbtns">
      <button id="dlgCancel">取消</button>
      <button id="dlgSave" class="primary">保存</button>
    </div>
  </div>
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
    var nAddr = 0, nDev = 0;
    var devLines = [];
    var rules = st.rules || {};
    Object.keys(rules).forEach(function (k) {
      var r = rules[k] || {};
      var a = r.addrs || [];
      nAddr += a.length;
      if (a.length) { nDev += 1; }
      devLines.push(k + "：" + (a.join("、") ||
        "（本轮无地址：" + (r.detail || r.error || "跳过") + "）"));
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
      ["采用地址", nAddr + " 个 · " + nDev + " 台设备", nAddr ? "on" : "off",
        "自动维护覆盖 " + nDev + " 台设备、共 " + nAddr + " 个地址，逐台：\n"
        + (devLines.join("\n") || "（还没有任何维护规则）")],
      ["最近一轮", st.last_tick_at || "尚未运行", "off",
        "最近一次尝试的时间（不论成功失败）"],
      ["累计写入", total + " 次", total ? "on" : "off",
        "跨容器重启累计（存 " + (c.state_file || "/data/state.json")
        + "），按条目计、含全部自动维护设备：新增 "
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

  // 白名单页数据：规则列表（谁在自动维护）与设备（弹窗选址用），loadWL 时更新
  var wlData = { rules: [], hosts: [] };

  function escapeRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  // 与后端 trustlist.managed_pattern 保持一致：基础名、基础名@N，各自可再带 -端口。
  function ownerOfName(name) {
    for (var i = 0; i < wlData.rules.length; i++) {
      var rn = wlData.rules[i].name;
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
    syncFwSwitch(st.firewall_ipv6_enabled);
  }

  // ---------- 快捷操作：总开关滑动开关 + 重置累计写入 ----------
  var fwBusy = false;

  function syncFwSwitch(enabled) {
    var sw = document.getElementById("fwSwitch");
    var txt = document.getElementById("fwSwitchTxt");
    if (!sw || fwBusy) { return; }
    var on = enabled === true;
    sw.className = "swbtn" + (on ? " on" : "");
    txt.textContent = on ? "已开启" : "已关闭";
    txt.style.color = on ? "var(--ok)" : "var(--muted)";
  }

  document.getElementById("fwSwitch").addEventListener("click", function () {
    var sw = document.getElementById("fwSwitch");
    var want = !sw.classList.contains("on");
    if (!confirm((want ? "打开" : "关闭") + " IPv6 防火墙总开关？" +
        (want ? "" : "关闭后白名单整体不生效，同步将暂停，直到重新打开。"))) { return; }
    fwBusy = true;
    sw.disabled = true;
    api("/api/router/firewall", {
      method: "POST",
      body: JSON.stringify({ enable: want })
    }).then(function (j) {
      showOut("防火墙开关", j);
      toast(j.ok ? ("总开关已" + (want ? "打开" : "关闭")) : ("失败：" + j.error),
        j.ok ? "ok" : "err");
      fwBusy = false;
      sw.disabled = false;
      syncFwSwitch(want === true);
      return load(false).then(loadWL);
    }).catch(function (e) {
      showOut("防火墙开关", "请求失败：" + e);
      fwBusy = false;
      sw.disabled = false;
    });
  });

  document.getElementById("btnResetCounters").addEventListener("click", function () {
    if (!confirm("确定把累计写入计数清零？此操作不影响白名单和同步。")) { return; }
    api("/api/counters/reset", { method: "POST", body: "{}" })
      .then(function (j) {
        showOut("重置累计写入", j);
        toast(j.ok ? "累计写入计数已清零" : ("失败：" + j.error), j.ok ? "ok" : "err");
        return load(false);
      });
  });

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
    load(false).then(function () {
      toast("已登录", "ok");
      loadWL();               // 白名单是主页的核心卡片，进来就拉一次
    }).catch(function (e) { toast("加载失败：" + e, "err"); });
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
    ["main", "devices", "events", "notify", "status", "about"].forEach(function (t) {
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
  document.getElementById("tabbtn-events").addEventListener("click", function () {
    switchTab("events");
    if (!evLoaded) { loadEvents(); }
  });
  document.getElementById("tabbtn-notify").addEventListener("click", function () { switchTab("notify"); });
  document.getElementById("tabbtn-status").addEventListener("click", function () { switchTab("status"); });
  document.getElementById("tabbtn-about").addEventListener("click", function () { switchTab("about"); });

  // ---------- 同步记录（文件即数据：现读 /api/events，前端过滤分页） ----------
  var evLoaded = false;
  var evShown = 100;                 // 首屏只渲染最近 100 条，「加载更多」翻页
  var EV_BADGE = { created: ["新增", "ev-new"], updated: ["更新", "ev-upd"],
                   removed: ["删除", "ev-del"], fw: ["开关", "ev-fw"] };

  function evAddrCell(td, e) {
    // 全程 textContent 防注入（设备名/地址来自路由器，属不可信输入）
    if (e.action === "fw") {
      td.textContent = e.addr || "";
      return;
    }
    if (e.old_addr) {
      var old = document.createElement("span");
      old.className = "mono evold";
      old.textContent = e.old_addr;
      td.appendChild(old);
      td.appendChild(document.createTextNode(" → "));
    }
    var cur = document.createElement("span");
    cur.className = "mono";
    cur.textContent = e.addr || "";
    td.appendChild(cur);
  }

  function renderEvents(all) {
    var q = document.getElementById("evQ").value.trim().toLowerCase();
    var act = document.getElementById("evAct").value;
    var list = all.filter(function (e) {
      if (act && e.action !== act) { return false; }
      if (!q) { return true; }
      return ((e.device || "") + " " + (e.mac || "") + " " + (e.entry || "")
              + " " + (e.addr || "") + " " + (e.old_addr || ""))
        .toLowerCase().indexOf(q) >= 0;
    });
    var box = document.getElementById("evBox");
    box.textContent = "";
    if (!list.length) {
      var p = document.createElement("p");
      p.className = "desc";
      p.style.textAlign = "center";
      p.textContent = "（记录已清空，或没有匹配的记录）";
      box.appendChild(p);
      document.getElementById("evMeta").textContent =
        "文件共 " + all.length + " 条";
      return;
    }
    var t = document.createElement("table");
    t.style.minWidth = "640px";        // 手机窄屏：表格整体横滑（tblwrap）
    var head = document.createElement("tr");
    ["时间", "设备", "动作", "条目", "地址", "端口", "触发"].forEach(function (h) {
      var th = document.createElement("th"); th.textContent = h; head.appendChild(th);
    });
    t.appendChild(head);
    list.slice(0, evShown).forEach(function (e) {
      var tr = document.createElement("tr");
      var td;
      td = document.createElement("td"); td.textContent = e.ts || "";
      td.style.color = "var(--muted)"; tr.appendChild(td);
      td = document.createElement("td");
      td.textContent = e.device || "（未知）";
      if (e.mac) {
        var br = document.createElement("br");
        var m = document.createElement("span");
        m.className = "mono"; m.style.fontSize = "11px";
        m.style.color = "var(--muted)";
        m.textContent = e.mac;
        td.appendChild(br); td.appendChild(m);
      }
      tr.appendChild(td);
      td = document.createElement("td");
      var b = EV_BADGE[e.action] || [e.action || "?", ""];
      var sp = document.createElement("span");
      sp.className = "evbadge " + b[1]; sp.textContent = b[0];
      td.appendChild(sp); tr.appendChild(td);
      td = document.createElement("td"); td.className = "mono";
      td.textContent = e.entry || ""; tr.appendChild(td);
      td = document.createElement("td");
      evAddrCell(td, e);
      tr.appendChild(td);
      td = document.createElement("td"); td.textContent = e.port || "";
      tr.appendChild(td);
      td = document.createElement("td"); td.textContent = e.trigger || "";
      td.style.color = "var(--muted)"; tr.appendChild(td);
      t.appendChild(tr);
    });
    var sc = document.createElement("div");
    sc.className = "tblwrap";
    sc.appendChild(t);
    box.appendChild(sc);
    document.getElementById("evMeta").textContent =
      "匹配 " + list.length + " 条 / 文件共 " + all.length +
      " 条 · 显示最近 " + Math.min(evShown, list.length) + " 条";
    document.getElementById("btnEvMore").style.display =
      list.length > evShown ? "" : "none";
  }

  var evAll = [];
  function loadEvents() {
    var btn = document.getElementById("btnEv");
    btn.disabled = true;
    return api("/api/events").then(function (j) {
      if (!j.ok) { toast("读取同步记录失败：" + (j.error || ""), "err"); return; }
      evLoaded = true;
      evAll = (j.events || []).slice().reverse();   // 新的在上
      var sel = document.getElementById("evMax");
      sel.value = String(j.max || 100);
      if (sel.selectedIndex < 0) { sel.value = "100"; }
      renderEvents(evAll);
    }).catch(function (e) { toast("请求失败：" + e, "err"); })
      .then(function () { btn.disabled = false; });
  }

  document.getElementById("btnEv").addEventListener("click", loadEvents);
  document.getElementById("evQ").addEventListener("input", function () {
    evShown = 100; renderEvents(evAll);
  });
  document.getElementById("evAct").addEventListener("change", function () {
    evShown = 100; renderEvents(evAll);
  });
  document.getElementById("btnEvMore").addEventListener("click", function () {
    evShown += 100; renderEvents(evAll);
  });
  document.getElementById("evMax").addEventListener("change", function () {
    var v = parseInt(this.value, 10);
    this.disabled = true;
    api("/api/events/max", { method: "POST", body: JSON.stringify({ max: v }) })
      .then(function (j) {
        toast(j.ok ? ("保留上限已改为 " + v + " 条并落盘")
                   : ("失败：" + (j.error || "")), j.ok ? "ok" : "err");
      })
      .then(function () { document.getElementById("evMax").disabled = false; });
  });
  document.getElementById("btnEvClear").addEventListener("click", function () {
    if (!confirm("确定清空全部同步记录？此操作不可恢复（不影响白名单和同步）。")) { return; }
    var b = this; b.disabled = true;
    api("/api/events/clear", { method: "POST", body: "{}" })
      .then(function (j) {
        if (j.ok) { evAll = []; evShown = 100; renderEvents(evAll); }
        toast(j.ok ? "记录已清空" : ("失败：" + (j.error || "")),
              j.ok ? "ok" : "err");
      })
      .then(function () { b.disabled = false; });
  });

  // 设备列表页内的二级页签：在线 / 离线 / 黑名单 / 儿童上网
  var DEV_TABS = ["online", "offline", "black", "kids"];
  DEV_TABS.forEach(function (k) {
    document.getElementById("devtab-" + k).addEventListener("click", function () {
      DEV_TABS.forEach(function (x) {
        document.getElementById("devtab-" + x).classList.toggle("active", x === k);
        document.getElementById("devbox-" + x).classList.toggle("hide", x !== k);
      });
    });
  });

  function loadHosts() {
    var btn = document.getElementById("btnHosts");
    btn.disabled = true;
    return api("/api/router/hosts").then(function (j) {
      showOut("设备列表", j);
      if (!j.ok) { toast("刷新设备列表失败：" + (j.error || ""), "err"); return; }
      hostsLoaded = true;
      var hosts = j.hosts || [];
      var online = hosts.filter(function (h) { return h.active; });
      var offline = hosts.filter(function (h) { return !h.active; });
      var kids = hosts.filter(function (h) { return h.kids; });
      var black = j.blacklist || [];
      document.getElementById("cnt-online").textContent = online.length;
      document.getElementById("cnt-offline").textContent = offline.length;
      document.getElementById("cnt-black").textContent = black.length;
      document.getElementById("cnt-kids").textContent = kids.length;
      document.getElementById("hostsDesc").textContent = "在线 " + online.length +
        " · 离线 " + offline.length + " · 黑名单 " + black.length +
        " · 儿童上网 " + kids.length +
        "　当前 LAN 前缀：" + ((j.lan_prefixes || []).join(", ") || "（无）") +
        (j.blacklist_error ? "　（黑名单读取失败：" + j.blacklist_error + "）" : "");

      // 所有动态文本一律 textContent 写入（主机名是不可信输入，防 XSS）
      function mkTable(heads) {
        var t = document.createElement("table");
        var hr = document.createElement("tr");
        heads.forEach(function (h) {
          var th = document.createElement("th"); th.textContent = h; hr.appendChild(th);
        });
        t.appendChild(hr);
        return t;
      }
      function addRow(t, cells, monoCols, rowCls) {
        var tr = document.createElement("tr");
        if (rowCls) { tr.className = rowCls; }
        cells.forEach(function (v, i) {
          var td = document.createElement("td");
          td.textContent = (v === null || v === undefined || v === "") ? "—" : v;
          if (monoCols.indexOf(i) >= 0) { td.className = "mono"; }
          tr.appendChild(td);
        });
        t.appendChild(tr);
        return tr;
      }
      // 表格套横向滚动容器；没有数据时显示一行占位说明
      function mount(boxId, t, emptyText, note) {
        var box = document.getElementById(boxId);
        box.textContent = "";
        if (t.rows.length > 1) {
          var sc = document.createElement("div");
          sc.className = "tblwrap";
          sc.appendChild(t);
          box.appendChild(sc);
        } else {
          var p = document.createElement("p");
          p.className = "devnote";
          p.textContent = emptyText;
          box.appendChild(p);
        }
        if (note) {
          var n = document.createElement("p");
          n.className = "devnote";
          n.textContent = note;
          box.appendChild(n);
        }
      }

      var t1 = mkTable(["MAC（填 TARGET_MAC）", "主机名", "IPv4", "当前 IPv6"]);
      online.forEach(function (h) {
        addRow(t1, [h.mac, h.name, h.ipv4,
          (h.ipv6 || []).join(", ") || "（无全局 IPv6）"], [0]);
      });
      mount("devbox-online", t1, "当前没有在线设备。");

      var t2 = mkTable(["MAC", "主机名", "IPv4", "最后在线"]);
      offline.forEach(function (h) {
        addRow(t2, [h.mac, h.name, h.ipv4, h.offline_at || "（未知）"], [0], "offrow");
      });
      mount("devbox-offline", t2, "当前没有离线记录。",
        "离线设备保留痕迹，按最后在线时间排列，方便回头找 MAC。");

      var t3 = mkTable(["主机名", "MAC", "拦截频段"]);
      black.forEach(function (b) {
        var tr = addRow(t3, [b.name || "（未知名）", b.mac, b.band || ""], [1]);
        tr.style.color = "var(--err)";
      });
      mount("devbox-black", t3, "黑名单是空的（没有设备被 WiFi 拦截）。",
        "来自路由器 WiFi MAC 过滤黑名单，只读展示；增删请到路由器管理页操作。");

      var t4 = mkTable(["主机名", "MAC", "当前 IPv6", "状态"]);
      kids.forEach(function (h) {
        addRow(t4, [h.name, h.mac,
          (h.ipv6 || []).join(", ") || "",
          h.active ? "在线" : ("离线" + (h.offline_at ? " · " + h.offline_at : ""))], [1]);
      });
      mount("devbox-kids", t4, "没有加入「儿童上网保护」的设备。",
        "由路由器儿童上网管控标记圈定；这些设备在线时也会出现在「在线设备」页签里。");
    }).catch(function (e) { showOut("设备列表", "请求失败：" + e); })
      .then(function () { btn.disabled = false; });
  }
  document.getElementById("btnHosts").addEventListener("click", function () { loadHosts(); });
  // ---------- 白名单：路由器式表格 + 设备选址弹窗 ----------
  var dlgEditing = null;   // null=新增；{mode:"rule",old:规则名,entry:条目}；{mode:"entry",id,entry}

  function loadWL() {
    var btn = document.getElementById("btnWl");
    btn.disabled = true;
    return api("/api/router/whitelist").then(function (j) {
      showOut("白名单", j);
      if (!j.ok) { toast("读取白名单失败：" + (j.error || ""), "err"); return; }
      wlData.rules = j.rules || [];
      wlData.hosts = j.hosts || [];
      var nManaged = (j.entries || []).filter(function (e) {
        return ownerOfName(e.Name || "");
      }).length;
      var box = document.getElementById("wlBox");
      // 总开关关闭 → 整卡收起：只留一行提示，表格与按钮全部隐藏
      if (!j.enabled) {
        els.wlDesc.textContent = "IPv6 防火墙总开关已关闭。";
        document.getElementById("btnWl").style.display = "none";
        document.getElementById("btnWlAdd").style.display = "none";
        box.textContent = "";
        var offHint = document.createElement("div");
        offHint.className = "help";
        offHint.style.color = "var(--warn)";
        offHint.textContent = "总开关已关闭，白名单整体不生效 —— 表格已隐藏。" +
          "可在上方「快捷操作」里重新打开；同步也已暂停，打开后下一轮自动恢复。";
        box.appendChild(offHint);
        return;
      }
      document.getElementById("btnWl").style.display = "";
      document.getElementById("btnWlAdd").style.display = "";
      els.wlDesc.textContent = "条目 " + j.entries.length + "/" + j.max +
        "　其中 " + nManaged + " 条由本程序自动维护（其余不动）";
      box.textContent = "";
      var t = document.createElement("table");
      var head = document.createElement("tr");
      ["服务名称", "允许来源", "本地 IP", "通信端口", "操作"].forEach(function (h) {
        var th = document.createElement("th"); th.textContent = h; head.appendChild(th);
      });
      t.appendChild(head);
      (j.entries || []).forEach(function (e) {
        var owner = ownerOfName(e.Name || "");
        var tr = document.createElement("tr");
        // 第 1 列：服务名称 + 归属徽标
        var c0 = document.createElement("td");
        c0.appendChild(document.createTextNode(e.Name || ""));
        if (owner) {
          var bg = document.createElement("span");
          bg.className = "badge"; bg.textContent = "自动维护";
          c0.appendChild(bg);
        } else {
          var mn = document.createElement("span");
          mn.className = "wlman"; mn.textContent = "手工";
          c0.appendChild(mn);
        }
        tr.appendChild(c0);
        [e.RemoteIp || "::/0", e.LocalIp,
         (e.Port === -1 || e.Port === "-1") ? "全部" : e.Port]
          .forEach(function (v) {
            var td = document.createElement("td");
            td.textContent = (v === null || v === undefined || v === "") ? "—" : v;
            tr.appendChild(td);
          });
        // 操作：编辑 / 删除（动态文本一律 textContent，主机名不可信）
        var cOp = document.createElement("td");
        var be = document.createElement("button"); be.textContent = "编辑";
        var bd = document.createElement("button"); bd.textContent = "删除";
        be.addEventListener("click", function () {
          openDlg(owner ? { mode: "rule", old: owner, entry: e }
                        : { mode: "entry", id: e.ID || "", entry: e });
        });
        bd.addEventListener("click", function () {
          if (owner) { removeRule(owner); } else { deleteEntry(e); }
        });
        cOp.appendChild(be);
        cOp.appendChild(document.createTextNode(" "));
        cOp.appendChild(bd);
        tr.appendChild(cOp);
        t.appendChild(tr);
      });
      var sc = document.createElement("div");
      sc.className = "tblwrap";
      sc.appendChild(t);
      box.appendChild(sc);
    }).catch(function (e) { showOut("白名单", "请求失败：" + e); })
      .then(function () { btn.disabled = false; });
  }

  function fillDevSelect(selectedMac) {
    var sel = document.getElementById("dlgDev");
    sel.textContent = "";
    var blank = document.createElement("option");
    blank.value = ""; blank.textContent = "未知设备";
    sel.appendChild(blank);
    wlData.hosts.forEach(function (h) {
      var o = document.createElement("option");
      var ipv6 = (h.ipv6 || [])[0] || "";
      if (!ipv6) {
        o.disabled = true;
        o.textContent = h.name + "(" + h.mac + ")　无全局 IPv6";
      } else {
        o.value = h.mac;
        o.textContent = h.name + "(" + h.mac + ")";
      }
      sel.appendChild(o);
    });
    sel.value = selectedMac || "";
    if (sel.value !== (selectedMac || "")) { sel.value = ""; }
  }

  function openDlg(editing) {
    dlgEditing = editing || null;
    var title = document.getElementById("dlgTitle");
    var hint = document.getElementById("dlgHint");
    var name = document.getElementById("dlgName");
    var remote = document.getElementById("dlgRemote");
    var local = document.getElementById("dlgLocal");
    var port = document.getElementById("dlgPort");
    var msg = document.getElementById("dlgMsg");
    msg.className = "msg"; msg.textContent = "";
    if (!editing) {
      title.textContent = "添加白名单条目";
      hint.textContent = "选设备后自动填入它的当前地址（设备地址列表第一条）。保存后加入自动维护，地址变化时原地更新。";
      name.value = ""; remote.value = "::/0"; local.value = "";
      port.value = ""; fillDevSelect("");
    } else if (editing.mode === "rule") {
      var r = null;
      wlData.rules.forEach(function (x) { if (x.name === editing.old) { r = x; } });
      title.textContent = "编辑自动维护条目";
      hint.textContent = "可换绑设备或改端口；保存后立即按新配置同步。";
      name.value = editing.old;
      remote.value = (r && r.remote_ip) || "::/0";
      port.value = (r && r.port === "全部") ? "-1" : ((r && r.port) || "-1");
      local.value = editing.entry ? (editing.entry.LocalIp || "") : "";
      fillDevSelect(r ? r.mac : "");
    } else {
      var e2 = editing.entry || {};
      title.textContent = "编辑手工条目";
      hint.textContent = "手工条目直接改字段保存；要自动跟随设备地址，请删除后用「添加」重加。";
      name.value = e2.Name || "";
      remote.value = e2.RemoteIp || "::/0";
      var p = e2.Port;
      port.value = (p === -1 || p === "-1" || p === null || p === undefined)
        ? "-1" : String(p);
      local.value = e2.LocalIp || "";
      fillDevSelect("");
    }
    document.getElementById("dlgMask").classList.remove("hide");
  }

  function closeDlg() {
    document.getElementById("dlgMask").classList.add("hide");
    dlgEditing = null;
  }

  document.getElementById("dlgDev").addEventListener("change", function () {
    var mac = this.value;
    if (!mac) { return; }
    var hit = null;
    wlData.hosts.forEach(function (h) { if (h.mac === mac) { hit = h; } });
    if (hit) {
      document.getElementById("dlgLocal").value = (hit.ipv6 || [])[0] || "";
      var nameBox = document.getElementById("dlgName");
      if (!nameBox.value) { nameBox.value = hit.name; }
    }
  });
  document.getElementById("dlgCancel").addEventListener("click", closeDlg);
  document.getElementById("dlgMask").addEventListener("click", function (ev) {
    if (ev.target === this) { closeDlg(); }
  });
  document.getElementById("dlgSave").addEventListener("click", function () {
    var body = {
      name: document.getElementById("dlgName").value.trim(),
      remote_ip: document.getElementById("dlgRemote").value.trim(),
      mac: document.getElementById("dlgDev").value,
      local_ip: document.getElementById("dlgLocal").value.trim(),
      port: document.getElementById("dlgPort").value.trim() || "-1"
    };
    var msg = document.getElementById("dlgMsg");
    function fail(t) { msg.className = "msg err"; msg.textContent = t; }
    if (!body.name) { return fail("服务名称不能为空"); }
    var p;
    if (dlgEditing && dlgEditing.mode === "rule") {
      body.action = "update"; body.old_name = dlgEditing.old;
      p = api("/api/rules", { method: "POST", body: JSON.stringify(body) });
    } else if (dlgEditing && dlgEditing.mode === "entry") {
      body.id = dlgEditing.id;
      p = api("/api/router/whitelist/update",
              { method: "POST", body: JSON.stringify(body) });
    } else {
      if (!body.mac) { return fail("请选择一台设备（自动维护需要跟随设备地址）"); }
      if (!body.local_ip) { return fail("本地 IP 为空 —— 所选设备当前没有全局 IPv6"); }
      body.action = "add";
      p = api("/api/rules", { method: "POST", body: JSON.stringify(body) });
    }
    var btn = document.getElementById("dlgSave");
    btn.disabled = true;
    p.then(function (j) {
      if (!j.ok) { return fail(j.error || "保存失败"); }
      closeDlg();
      (j.notes || []).forEach(function (n) { toast(n, "ok"); });
      loadWL();
      load(false);
    }).catch(function (e) { fail("请求失败：" + e); })
      .then(function () { btn.disabled = false; });
  });

  function removeRule(name) {
    if (!window.confirm("删除条目「" + name + "」？\n会连同自动维护规则一起删除，之后不再自动重建。")) { return; }
    api("/api/rules", { method: "POST",
      body: JSON.stringify({ action: "remove", name: name }) })
      .then(function (j) {
        showOut("删除条目", j);
        toast(j.ok ? "已删除" : ("删除失败：" + (j.error || "")), j.ok ? "ok" : "err");
        if (j.ok) { loadWL(); load(false); }
      });
  }

  function deleteEntry(e) {
    if (!window.confirm("从路由器白名单删除条目「" + (e.Name || "") + "」？")) { return; }
    api("/api/router/whitelist/delete", { method: "POST",
      body: JSON.stringify({ id: e.ID || "", name: e.Name || "" }) })
      .then(function (j) {
        showOut("删除条目", j);
        toast(j.ok ? "已删除" : ("删除失败：" + (j.error || "")), j.ok ? "ok" : "err");
        if (j.ok) { loadWL(); }
      });
  }

  document.getElementById("btnWl").addEventListener("click", function () { loadWL(); });
  document.getElementById("btnWlAdd").addEventListener("click", function () { openDlg(null); });
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
