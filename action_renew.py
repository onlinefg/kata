"""
Katabump 自动续期 — pydoll 原生 CF bypass 版
- WxPusher 通知
- 多账号整合报告，含下次续期时间
- 录屏结束后停留 3 秒
- 修复通知消息截断问题
"""
import sys
print("[DEBUG] 导入中...", flush=True)

import asyncio
import json
import os
import traceback
from pathlib import Path

import httpx
from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions
print("[DEBUG] 导入完成", flush=True)

WXPUSHER_APP_TOKEN = os.getenv("WXPUSHER_APP_TOKEN", "")
WXPUSHER_UID       = os.getenv("WXPUSHER_UID", "")
HTTP_PROXY         = os.getenv("HTTP_PROXY", "")
HEADLESS           = os.getenv("HEADLESS", "false").lower() == "true"
SHOT_DIR           = Path("./screenshots")

# ----- WxPusher 通知 -----
async def send_wxpusher(text: str):
    print(f"   [WxPusher] APP_TOKEN={'set' if WXPUSHER_APP_TOKEN else 'missing'}  UID={'set' if WXPUSHER_UID else 'missing'}", flush=True)
    if not WXPUSHER_APP_TOKEN or not WXPUSHER_UID:
        print("   >> WxPusher 未配置，跳过通知", flush=True)
        return
    url = "https://wxpusher.zjiecode.com/api/send/message"
    payload = {
        "appToken": WXPUSHER_APP_TOKEN,
        "content": text,
        "contentType": 1,
        "uids": [WXPUSHER_UID],
    }
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            resp = await c.post(url, json=payload)
            print(f"   >> WxPusher 状态码: {resp.status_code}, 响应: {resp.text}", flush=True)
        except Exception as e:
            print(f"   >> WxPusher 异常: {e}", flush=True)

# ----- 用户加载 -----
def load_users():
    raw = os.getenv("USERS_JSON", "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "users" in data:
            return data["users"]
    except Exception:
        return []

# ----- Chromium 路径探测 -----
def _find_chromium() -> str | None:
    candidates = [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            print(f"[DEBUG] 找到 Chromium: {p}", flush=True)
            return p
    try:
        import subprocess
        result = subprocess.run(
            ["which", "chromium-browser", "chromium", "google-chrome"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and os.path.isfile(line):
                print(f"[DEBUG] which 找到: {line}", flush=True)
                return line
    except Exception:
        pass
    return None

def build_options():
    opts = ChromiumOptions()
    opts.headless = HEADLESS
    path = _find_chromium()
    if path:
        opts.binary_location = path
    else:
        print("⚠️ 未找到 Chromium，使用 pydoll 默认路径", flush=True)

    opts.add_argument("--window-size=1280,720")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-features=VizDisplayCompositor")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--disable-save-password-bubble")
    opts.add_argument("--disable-password-generation")
    opts.add_argument("--password-store=basic")
    opts.add_argument("--use-mock-keychain")
    if HTTP_PROXY:
        opts.add_argument(f"--proxy-server={HTTP_PROXY}")
    opts.browser_preferences = {
        "credentials_enable_service": False,
        "profile": {
            "password_manager_enabled": False,
            "default_content_setting_values": {
                "notifications": 2,
                "geolocation": 2,
            },
        },
    }
    return opts

# ----- JS 执行 -----
async def _js(tab, expression: str):
    raw = await tab.execute_script(f"return ({expression});")
    if isinstance(raw, dict):
        try:
            inner = raw.get("result", {}).get("result", {})
            if "value" in inner:
                return inner["value"]
            if "value" in raw.get("result", {}):
                return raw["result"]["value"]
        except Exception:
            pass
        return str(raw)
    return raw

# ----- 工具函数 -----
async def get_url(tab) -> str:
    try:
        return str(await _js(tab, "window.location.href"))
    except Exception:
        return ""

async def page_has_text(tab, text: str) -> bool:
    try:
        result = await _js(
            tab,
            f"document.body && document.body.innerText && document.body.innerText.includes({json.dumps(text)})"
        )
        return result is True or str(result).lower() == "true"
    except Exception:
        return False

async def take_screenshot(browser, tab, path: str):
    """pydoll 2.23 的正确方法名是 take_screenshot（不是 screenshot），
    且没有 tab._connection / tab.connection 这种属性（实际是
    tab._connection_handler），所以旧版本两条分支永远静默失败，
    从未真正写出过文件。这里直接调用官方 API，并把异常打印出来，
    避免以后再次"静默无截图"。"""
    try:
        await tab.take_screenshot(path)
        print(f"   >> 📸 截图已保存: {path}", flush=True)
    except Exception as e:
        print(f"   >> ⚠️ 截图失败 path={path}: {e!r}", flush=True)

# ----- 获取下次续期时间（从页面抓取 Expiry）-----
async def _get_next_renew_time(tab) -> str:
    """从页面提取 Expiry 和 Renew period，返回可读字符串"""
    try:
        info = await _js(tab, """
            (function() {
                const lines = document.body.innerText.split('\\n');
                let expiry = null, period = null;
                for (let i = 0; i < lines.length; i++) {
                    if (lines[i].includes('Expiry')) {
                        expiry = lines[i].replace('Expiry', '').trim();
                    }
                    if (lines[i].includes('Renew period')) {
                        const nextLine = lines[i+1] || '';
                        period = nextLine.trim();
                    }
                }
                if (expiry && period) {
                    return JSON.stringify({ expiry, period });
                }
                return null;
            })()
        """)
        if info and isinstance(info, str) and info != "null":
            data = json.loads(info)
            expiry = data.get("expiry", "")
            period = data.get("period", "")
            if expiry:
                return f"到期日: {expiry} (周期: {period})" if period else f"到期日: {expiry}"
    except Exception:
        pass

    # 备选：用正则从页面文本中找日期（如 "expires on 20 May 2025" 等格式）
    try:
        fallback = await _js(tab, r"""
            (function() {
                const t = document.body.innerText;
                const m = t.match(/expir\w*\s*[:\-]?\s*([\d]{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})/i);
                return m ? m[1].trim() : null;
            })()
        """)
        if fallback and fallback != "null" and isinstance(fallback, str):
            return f"到期日: {fallback}"
    except Exception:
        pass

    return "续期成功（未能获取具体时间）"

# ----- ALTCHA JS 点击 -----
async def solve_altcha_in_modal(tab) -> bool:
    print("   >> 开始处理 modal 内 ALTCHA...", flush=True)
    for _ in range(20):
        exists = await _js(tab, "!!document.querySelector('#renew-modal altcha-widget')")
        if exists is True or str(exists).lower() == "true":
            break
        await asyncio.sleep(0.5)
    else:
        print("   >> 未在 modal 内发现 ALTCHA", flush=True)
        return False

    for attempt in range(1, 6):
        print(f"   >> ALTCHA JS 点击尝试 {attempt}/5", flush=True)
        clicked = await _js(tab, """
            (function() {
                const cb = document.querySelector('#renew-modal altcha-widget input[type="checkbox"]');
                if (!cb) return false;
                cb.focus();
                cb.click();
                cb.dispatchEvent(new Event('input', {bubbles: true}));
                cb.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            })()
        """)
        print(f"   >> JS click 返回值: {clicked}", flush=True)
        if not clicked or clicked == "false":
            await asyncio.sleep(1)
            continue

        for _ in range(12):
            await asyncio.sleep(1)
            state = await _js(tab, """
                (function() {
                    const w = document.querySelector('#renew-modal altcha-widget .altcha');
                    if (!w) return 'no-widget';
                    return w.getAttribute('data-state') || 'unknown';
                })()
            """)
            print(f"   >> ALTCHA state = {state}", flush=True)
            if state == "verified":
                print("   >> ✅ ALTCHA verified", flush=True)
                await _js(tab, """
                    (function() {
                        const modal = document.querySelector('#renew-modal');
                        if (!modal) return;
                        const btn = modal.querySelector('button[type="submit"]');
                        if (btn) {
                            btn.focus();
                            setTimeout(() => btn.click(), 300);
                        }
                    })()
                """)
                await asyncio.sleep(2)
                return True
            if state == "error":
                print("   >> ❌ ALTCHA error", flush=True)
                break
        await asyncio.sleep(0.5)
    print("   >> ❌ ALTCHA 多次点击失败", flush=True)
    return False

async def handle_captcha(tab, is_renew: bool) -> bool:
    if is_renew:
        raw = await _js(tab, "!!document.querySelector('#renew-modal altcha-widget')")
        if raw is True or str(raw).lower() == "true":
            return await solve_altcha_in_modal(tab)
    return True

# ----- 登录页 Cloudflare Turnstile 勾选框点击（坐标点击思路，参考 Zytrano）-----
async def _turnstile_token_ready(tab) -> bool:
    """检测 cf-turnstile-response 是否已写入 token（穿透 shadow DOM）"""
    val = await _js(tab, """
        (function() {
            function deepQuery(root, sel) {
                let el = root.querySelector(sel);
                if (el) return el;
                for (const host of root.querySelectorAll('*')) {
                    if (host.shadowRoot) {
                        el = deepQuery(host.shadowRoot, sel);
                        if (el) return el;
                    }
                }
                return null;
            }
            const el = deepQuery(document, 'input[name="cf-turnstile-response"]');
            return el ? (el.value || '').length > 10 : false;
        })()
    """)
    return val is True or str(val).lower() == "true"

async def _dump_frames(tab, label: str):
    """打印当前所有 frame URL，用于诊断 Turnstile 找不到的情况"""
    try:
        frames = await tab.get_frames()
        print(f"   [诊断/{label}] 共 {len(frames)} 个 frame：", flush=True)
        for i, f in enumerate(frames):
            url = (getattr(f, 'url', '') or 'about:blank')[:120]
            print(f"     [{i}] {url}", flush=True)
    except Exception as e:
        print(f"   [诊断/{label}] dump_frames 失败: {e}", flush=True)


async def _find_turnstile_box_cdp(tab) -> dict | None:
    """
    用 JS 累加 offsetTop/offsetLeft 获取 div.cf-turnstile 的绝对坐标。
    不依赖 get_frames()（pydoll Tab 不支持），不进入 shadow DOM。
    div.cf-turnstile 容器本身在主文档 DOM 里，offsetTop/Left 是准确的。
    """
    try:
        # 用 JSON.stringify 返回，避免 CDP objectId 问题（pydoll 对象返回值无法直接解析）
        raw_str = await _js(tab, """
        JSON.stringify((function() {
            var el = document.querySelector('div.cf-turnstile') || document.querySelector('[data-sitekey]');
            if (!el) return {err: 'no-element', url: window.location.href.substring(0,60)};
            var top = 0, left = 0, cur = el;
            while (cur) {
                top  += cur.offsetTop  || 0;
                left += cur.offsetLeft || 0;
                cur   = cur.offsetParent;
            }
            var viewTop  = top  - window.scrollY;
            var viewLeft = left - window.scrollX;
            var w = el.offsetWidth  || 0;
            var h = el.offsetHeight || 0;
            return {
                x:      viewLeft,
                y:      viewTop,
                rawTop: top,
                rawLeft: left,
                scrollY: window.scrollY,
                width:  w > 10 ? w : 300,
                height: h > 10 ? h : 65,
                winW:   window.innerWidth,
                winH:   window.innerHeight,
                src:    'offset-accumulated'
            };
        })())
        """)

        import json as _json
        try:
            box = _json.loads(raw_str) if isinstance(raw_str, str) else None
        except Exception:
            box = None

        if box and isinstance(box, dict):
            if box.get('err'):
                print(f"   >> offset诊断: {box}", flush=True)
                return None
            x = box.get('x', -1)
            y = box.get('y', -1)
            print(f"   >> offset坐标: x={x:.0f} y={y:.0f} raw=({box.get('rawLeft',0):.0f},{box.get('rawTop',0):.0f}) scroll={box.get('scrollY',0):.0f} 窗口={box.get('winW',0)}x{box.get('winH',0)}", flush=True)
            if x >= 0 and y >= 0:
                return box
            if box.get('rawTop', 0) > 0:
                print(f"   >> y<0，用 rawTop={box.get('rawTop',0):.0f}", flush=True)
                return {**box, 'y': box['rawTop'], 'x': box.get('rawLeft', 0)}
        else:
            print(f"   >> JS解析失败: raw_str={str(raw_str)[:100]}", flush=True)

        return None
    except Exception as e:
        print(f"   >> offset定位失败: {e}", flush=True)
        return None

async def _find_turnstile_box_js(tab) -> dict | None:
    """
    穿透 Shadow DOM 递归查找 Turnstile iframe/容器坐标。
    CF Turnstile 将 iframe 注入到 div.cf-turnstile 的 closed shadow-root 里，
    document.querySelectorAll('iframe') 无法找到它。
    策略：
      1. div.cf-turnstile 容器坐标（外层有时有真实尺寸）
      2. 递归穿透所有 shadowRoot 找 iframe[src*=challenges]
      3. 递归穿透所有 shadowRoot 找任意 iframe（尺寸匹配）
    """
    js = """
    JSON.stringify((function() {
        // 递归穿透 shadow DOM，收集所有元素
        function deepQueryAll(root, sel) {
            var results = [];
            try {
                var direct = root.querySelectorAll(sel);
                for (var i = 0; i < direct.length; i++) results.push(direct[i]);
                var all = root.querySelectorAll('*');
                for (var j = 0; j < all.length; j++) {
                    var sr = all[j].shadowRoot;
                    if (sr) {
                        var inner = deepQueryAll(sr, sel);
                        for (var k = 0; k < inner.length; k++) results.push(inner[k]);
                    }
                }
            } catch(e) {}
            return results;
        }

        function rectOf(el) {
            var r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                return {x: r.left, y: r.top, width: r.width, height: r.height};
            return null;
        }

        // Layer 1: div.cf-turnstile 容器本身（有时外层就有尺寸）
        var containers = deepQueryAll(document, 'div.cf-turnstile, [data-sitekey]');
        for (var i = 0; i < containers.length; i++) {
            var r = rectOf(containers[i]);
            if (r) { r.src = 'shadow-cf-turnstile-div'; return r; }
        }

        // Layer 2: 穿透 shadow DOM 找 CF iframe（src 含 challenges）
        var iframes = deepQueryAll(document, 'iframe');
        for (var i = 0; i < iframes.length; i++) {
            var src = iframes[i].src || iframes[i].getAttribute('src') || '';
            if (src.indexOf('challenges.cloudflare.com') !== -1 ||
                src.indexOf('turnstile') !== -1) {
                var r = rectOf(iframes[i]);
                if (r) { r.src = 'shadow-cf-iframe'; return r; }
            }
        }

        // Layer 3: 穿透 shadow DOM 找尺寸匹配的任意 iframe
        for (var i = 0; i < iframes.length; i++) {
            var r2 = iframes[i].getBoundingClientRect();
            if (r2.width >= 200 && r2.width <= 450 && r2.height >= 50) {
                return {x: r2.left, y: r2.top, width: r2.width, height: r2.height,
                        src: 'shadow-iframe-size', iframe_src: (iframes[i].src||'').substring(0,60)};
            }
        }

        // 诊断：返回找到的所有 iframe 信息
        var info = [];
        for (var i = 0; i < iframes.length; i++) {
            var r3 = iframes[i].getBoundingClientRect();
            info.push({src:(iframes[i].src||'').substring(0,60), w:Math.round(r3.width), h:Math.round(r3.height)});
        }
        return {debug: true, iframes: info, containers: containers.length};
    })())
    """
    import json as _json2
    try:
        raw_str2 = await _js(tab, js)
        box = _json2.loads(raw_str2) if isinstance(raw_str2, str) else None
        if box and isinstance(box, dict):
            if box.get("debug"):
                print(f"   >> Shadow DOM 诊断: iframes={box.get('iframes')}, cf-turnstile容器数={box.get('containers')}", flush=True)
                return None
            if box.get("width", 0) > 0:
                print(f"   >> Turnstile 定位成功（{box.get('src','?')}）: x={box.get('x'):.0f} y={box.get('y'):.0f} w={box.get('width'):.0f} h={box.get('height'):.0f}", flush=True)
                return box
        else:
            print(f"   >> JS Shadow DOM raw: {str(raw_str2)[:80]}", flush=True)
    except Exception as e:
        print(f"   >> JS 查找 Turnstile 失败: {e}", flush=True)
    return None


async def click_login_turnstile_checkbox(browser, tab, timeout: float = 20) -> bool:
    """
    登录页 Cloudflare Turnstile 勾选框点击。
    策略：
      1. 先静默等待 3s，看 token 是否自动写入（invisible 模式无需点击）
      2. JS 递归遍历所有 iframe，找 challenges.cloudflare.com 坐标（可穿透嵌套层）
      3. 坐标合理性校验 → 点击 checkbox 左侧区域
      4. 点击后轮询等待 token 写入
    """
    print("   >> 检测登录页 Turnstile 勾选框...", flush=True)

    # 阶段1：静默等待，看是否已自动通过（invisible 模式）
    for _ in range(6):
        if await _turnstile_token_ready(tab):
            print("   >> ✅ Turnstile 已自动通过，无需点击", flush=True)
            return True
        await asyncio.sleep(0.5)

    # 阶段2：CDP frame tree 优先（能穿透 closed shadow DOM），JS 作 fallback
    box = None
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        # 先用 CDP（最可靠，能看到 closed shadow-root 里的 iframe）
        box = await _find_turnstile_box_cdp(tab)
        if box:
            print(f"   >> [CDP] 找到 Turnstile: {box}", flush=True)
            break
        # CDP 失败再试 JS（open shadow DOM 降级场景）
        box = await _find_turnstile_box_js(tab)
        if box:
            print(f"   >> [JS] 找到 Turnstile: {box}", flush=True)
            break
        await asyncio.sleep(0.5)

    # 阶段2.5：所有方法失败 → 动态坐标兜底
    # CF Turnstile 在登录表单底部，checkbox 约在窗口水平中央偏左、垂直约75%处
    if not box:
        await take_screenshot(browser, tab, str(SHOT_DIR / "turnstile_iframe_not_found.png"))
        try:
            win = await _js(tab, "({w: window.innerWidth, h: window.innerHeight})")
            win_w = win.get('w', 1280) if isinstance(win, dict) else 1280
            win_h = win.get('h', 720)  if isinstance(win, dict) else 720
            # 截图分析：
            # - 截图尺寸 1279x576，窗口 1280x720，差值144px = 浏览器地址栏高度
            # - checkbox 在截图约 (152, 390)，换算窗口坐标: x=152, y=390+144=534
            # - 用截图比例动态换算
            addr_bar_h = win_h - 576  # 地址栏高度（截图高度固定576）
            cx = 152  # checkbox 水平位置固定（登录卡片左侧）
            cy = 390 + addr_bar_h   # 截图内 y=390，加地址栏偏移
            print(f"   >> 自动定位失败，动态坐标兜底 ({cx}, {cy})，窗口={win_w}x{win_h} 地址栏={addr_bar_h}", flush=True)
            await tab.mouse.click(cx, cy, humanize=True)
        except Exception as e:
            print(f"   >> 动态坐标点击失败: {e}", flush=True)
            return False
        for i in range(int(timeout * 2)):
            if await _turnstile_token_ready(tab):
                print(f"   >> Turnstile token 就绪（动态坐标，{i * 0.5:.1f}s）", flush=True)
                return True
            await asyncio.sleep(0.5)
        print("   >> 动态坐标点击后 token 超时", flush=True)
        return False

    # 坐标合理性校验：x/y 必须在视口范围内
    bx = box.get("x", -1)
    by = box.get("y", -1)
    bh = box.get("height", 0)
    if not (0 <= bx < 1200 and 0 <= by < 800):
        print(f"   >> ❌ bounding_box 坐标异常 ({bx:.0f}, {by:.0f})，跳过点击", flush=True)
        await take_screenshot(browser, tab, str(SHOT_DIR / "turnstile_bad_coords.png"))
        return False

    # checkbox 在容器左侧约 28px，垂直居中
    x = bx + 28
    y = by + bh / 2
    print(f"   >> 坐标点击 Turnstile checkbox ({x:.0f}, {y:.0f})", flush=True)
    try:
        # humanize=True 内部：贝塞尔曲线 + Fitts定律 + 生理颤抖 + 超调修正 + 随机按压时长
        await tab.mouse.click(x, y, humanize=True)
    except Exception as e:
        print(f"   >> ❌ 坐标点击失败: {e}", flush=True)
        await take_screenshot(browser, tab, str(SHOT_DIR / "turnstile_click_failed.png"))
        return False

    # 阶段3：点击后等待 token 写入
    for i in range(int(timeout * 2)):
        if await _turnstile_token_ready(tab):
            print(f"   >> ✅ Turnstile token 就绪（{i * 0.5:.1f}s）", flush=True)
            return True
        await asyncio.sleep(0.5)

    print("   >> ❌ Turnstile token 等待超时", flush=True)
    await take_screenshot(browser, tab, str(SHOT_DIR / "turnstile_token_timeout.png"))
    return False

# ----- 登录 -----

def mask_email(email: str) -> str:
    """将邮箱脱敏：user@domain.com -> u***@d***.com"""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    masked_local = local[0] + "***" if len(local) > 1 else "***"
    domain_parts = domain.split(".")
    masked_domain = domain_parts[0][0] + "***" if len(domain_parts[0]) > 1 else "***"
    return f"{masked_local}@{masked_domain}.{'.'.join(domain_parts[1:])}"

def mask_sid(sid) -> str:
    """将服务器ID脱敏"""
    s = str(sid)
    return s[:2] + "***" if len(s) > 2 else "***"

async def do_login(browser, tab, user: dict) -> bool:
    u, p = user["username"], user["password"]
    print(f"  🔑 清理旧 session: {mask_email(u)}", flush=True)
    try:
        await tab.go_to("https://dashboard.katabump.com/auth/logout")
        await asyncio.sleep(0.5)
    except Exception:
        pass

    for attempt in range(1, 4):
        print(f"\n  🔑 登录 {attempt}/3: {mask_email(u)}", flush=True)
        await tab.go_to("https://dashboard.katabump.com/auth/login")
        await asyncio.sleep(3)

        if "dashboard" in await get_url(tab) and "login" not in await get_url(tab):
            print("   >> ✅ Session 仍有效", flush=True)
            return True

        print("   >> 填表...", flush=True)
        try:
            filled = await _js(tab, f"""
                (function() {{
                    const email = document.querySelector('input[name="email"], input[type="email"]');
                    const pass = document.querySelector('input[name="password"], input[type="password"]');
                    if (email && pass) {{
                        email.value = {json.dumps(u)};
                        email.dispatchEvent(new Event('input', {{bubbles:true}}));
                        email.dispatchEvent(new Event('change', {{bubbles:true}}));
                        pass.value = {json.dumps(p)};
                        pass.dispatchEvent(new Event('input', {{bubbles:true}}));
                        pass.dispatchEvent(new Event('change', {{bubbles:true}}));
                        return true;
                    }}
                    return false;
                }})()
            """)
            if not filled:
                email_el = await tab.find(tag_name="input", name="email", timeout=5)
                await email_el.click()
                await email_el.type_text(u, humanize=True)
                pass_el = await tab.find(tag_name="input", name="password", timeout=3)
                await pass_el.click()
                await pass_el.type_text(p, humanize=True)
        except Exception as e:
            print(f"   >> 填表失败: {e}", flush=True)
            continue

        await handle_captcha(tab, is_renew=False)
        await asyncio.sleep(2)

        # Cloudflare Turnstile 坐标点击
        has_turnstile = await _js(tab, "!!document.querySelector('input[name=\"cf-turnstile-response\"]') || !!document.querySelector('div.cf-turnstile')")
        if has_turnstile is True or str(has_turnstile).lower() == "true":
            ok = await click_login_turnstile_checkbox(browser, tab, timeout=20)
            if not ok:
                print("   >> Turnstile 未完成，刷新重试...", flush=True)
                await tab.refresh()
                await asyncio.sleep(2)
                continue

        await tab.execute_script("""(function() {
            const bar = document.querySelector('[aria-label="Save password?"]');
            if (bar) bar.remove();
        })()""")
        print("   >> 点击 Login...", flush=True)
        try:
            btn = await tab.find(tag_name="button", text="Login", timeout=2)
            await btn.click()
        except Exception:
            await tab.execute_script("document.querySelector('button[type=submit]')?.click()")

        for _ in range(10):
            await asyncio.sleep(1)
            url = await get_url(tab)
            if "dashboard" in url and "login" not in url:
                print("   >> ✅ 登录成功", flush=True)
                return True
    return False

# ----- 续期（返回 (成功bool, 细节str)）-----
async def do_renew(browser, tab, user: dict):
    u   = user["username"]
    sid = user.get("serverId") or os.getenv("KATABUMP_SERVER_ID")
    if not sid:
        print("   >> ❌ 未提供 serverId，跳过续期", flush=True)
        return False, "缺少 serverId"
    sf  = u.replace("@", "_").replace(".", "_")

    print(f"   >> 导航到 servers/edit?id={mask_sid(sid)}", flush=True)
    await tab.go_to(f"https://dashboard.katabump.com/servers/edit?id={sid}")
    await asyncio.sleep(2)

    for attempt in range(1, 4):
        print(f"\n  🔄 续期 {attempt}/3: {mask_email(u)}", flush=True)
        btn = await tab.find(tag_name="button", text="Renew", timeout=3, raise_exc=False)
        if not btn:
            print("   >> 找不到 Renew 按钮", flush=True)
            return False, "找不到 Renew 按钮"

        await btn.click()
        print("   >> 已点击 Renew，等待 modal...", flush=True)

        for _ in range(10):
            await asyncio.sleep(1)
            visible = await _js(tab, """
                (function() {
                    const m = document.querySelector('#renew-modal');
                    return m && m.getBoundingClientRect().width > 0;
                })()
            """)
            if visible is True or str(visible).lower() == "true":
                break
        else:
            print("   >> modal 未出现，刷新重试", flush=True)
            await tab.refresh()
            await asyncio.sleep(2)
            continue

        print("   >> modal 已打开，处理 ALTCHA...", flush=True)
        if not await handle_captcha(tab, is_renew=True):
            print("   >> ALTCHA 失败，刷新重试", flush=True)
            await tab.refresh()
            await asyncio.sleep(1.5)
            continue

        # 等待续期结果 (最多8秒)
        for _ in range(8):
            await asyncio.sleep(1)
            gone = await _js(tab, "!document.querySelector('#renew-modal')")
            if gone is True or str(gone).lower() == "true":
                print("   >> ✅ 续期成功！", flush=True)
                await asyncio.sleep(2)  # 等待页面 Expiry 数据刷新
                next_time = await _get_next_renew_time(tab)
                sp = SHOT_DIR / f"{sf}_ok.png"
                await take_screenshot(browser, tab, str(sp))
                return True, next_time

            # ✅ 修复：完整捕获 “You can't renew ... (in X day(s)).” 整个句子
            not_yet_msg = await _js(tab, """
                (function() {
                    const bodyText = document.body.innerText;
                    // 匹配从 "You can't renew" 开始，直到遇到两个句号或字符串末尾
                    const match = bodyText.match(/You can't renew[^.]*\\.[^.]*?\\./i);
                    if (match) {
                        return match[0].replace(/\\s+/g, ' ').trim();
                    }
                    // 如果只有一个句号，退而求其次
                    const match2 = bodyText.match(/You can't renew[^.]*\\./i);
                    return match2 ? match2[0].replace(/\\s+/g, ' ').trim() : null;
                })()
            """)
            if not_yet_msg:
                print(f"   >> ⏳ {not_yet_msg}", flush=True)
                sp = SHOT_DIR / f"{sf}_skip.png"
                await take_screenshot(browser, tab, str(sp))
                return False, not_yet_msg

        # 未确定结果，刷新重试
        await tab.refresh()
        await asyncio.sleep(1.5)

    return False, "多次尝试续期未成功"

# ----- 主流程 -----
async def main():
    users = load_users()
    if not users:
        print("❌ 未设置 USERS_JSON")
        sys.exit(1)

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    opts = build_options()
    print(f"\n🚀 开始 | 用户: {len(users)} | Headless: {HEADLESS}", flush=True)

    browser = None
    for i in range(3):
        try:
            async with asyncio.timeout(60):
                browser = Chrome(options=opts)
                await browser.__aenter__()
            break
        except Exception as e:
            print(f"❌ Chrome 启动失败 ({i+1}/3): {e}", flush=True)
            if browser:
                try: await browser.__aexit__(None, None, None)
                except: pass
                browser = None
            if i == 2:
                await send_wxpusher("❌ Chrome 多次启动失败，放弃")
                sys.exit(1)
            await asyncio.sleep(3)

    tab = None
    try:
        async with asyncio.timeout(30):
            for _ in range(5):
                try:
                    tab = await browser.start()
                    break
                except Exception:
                    await asyncio.sleep(1.5)
        if not tab:
            raise RuntimeError("无法创建 tab")
    except Exception as e:
        print(f"❌ tab 创建失败: {e}", flush=True)
        await browser.__aexit__(None, None, None)
        sys.exit(1)

    results = []   # 收集报告 (username, success, detail)
    try:
        for user in users:
            u = user["username"]
            print(f"\n{'='*40} [{mask_email(u)}] {'='*40}", flush=True)
            try:
                if not await do_login(browser, tab, user):
                    results.append((u, False, "登录失败"))
                    continue
                success, detail = await do_renew(browser, tab, user)
                results.append((u, success, detail))
            except Exception as e:
                print(f"   >> ❌ {type(e).__name__}: {e}", flush=True)
                traceback.print_exc()
                results.append((u, False, f"异常: {str(e)[:50]}"))
            sf = u.replace("@", "_").replace(".", "_")
            await take_screenshot(browser, tab, str(SHOT_DIR / f"{sf}.png"))

        # 整合通知
        if results:
            summary_lines = ["📊 Katabump 自动续期报告"]
            for name, ok, detail in results:
                icon = "✅" if ok else "❌"
                summary_lines.append(f"\n{icon} {name}")
                if detail:
                    summary_lines.append(f"   {detail}")
            await send_wxpusher("\n".join(summary_lines).strip())

        # 所有账号处理完毕，等待3秒再退出，方便录屏观察
        print("\n⏳ 所有流程结束，3秒后关闭浏览器...", flush=True)
        await asyncio.sleep(3)

    finally:
        try: await tab.close()
        except: pass
        try: await browser.__aexit__(None, None, None)
        except: pass

    print("\n✅ 整体流程结束", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
