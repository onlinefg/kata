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

async def click_login_turnstile_checkbox(browser, tab, timeout: float = 20) -> bool:
    """
    登录表单内嵌的 Cloudflare Turnstile 勾选框（managed 模式，需要点击）。
    思路同 Zytrano 项目：
      1. 先静默等待几秒，看 token 是否自动写入（无需点击）
      2. 用 tab.find_shadow_roots() 穿透 shadow DOM 定位含
         challenges.cloudflare.com 的 iframe，进入其内部 shadow root
         拿到真正的 checkbox 元素并 .click()
      3. 上面失败时，降级为坐标点击：取 iframe 的 bounding box，
         用 tab.mouse.click(x, y) 在 checkbox 大致位置（左侧 ~25px）点一下
      4. 点击后再等待 token 写入确认是否成功
    """
    print("   >> 检测登录页 Turnstile 勾选框...", flush=True)

    # 阶段1：静默等待，看是否已自动通过
    for _ in range(6):
        if await _turnstile_token_ready(tab):
            print("   >> ✅ Turnstile 已自动通过，无需点击", flush=True)
            return True
        await asyncio.sleep(0.5)

    # 阶段2：用 deep=True 穿透跨进程 OOPIF，直接在所有 shadow root（包括
    # Turnstile iframe 内部、属于独立渲染进程的那一层）里找真正的 checkbox。
    # 注：pydoll 内置的 _find_cloudflare_shadow_root + iframe.find('body').get_shadow_root()
    # 只能处理同进程 shadow DOM；Turnstile 的 iframe 大多数情况下是跨域 OOPIF，
    # 那条路径必然超时（这也是日志里 "Error in cloudflare bypass" 的原因），
    # 所以这里改用 deep 穿透直接拿 checkbox，不再手动下钻 iframe -> body -> shadow。
    checkbox_clicked = False
    deadline = asyncio.get_event_loop().time() + timeout
    last_err = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            shadow_roots = await tab.find_shadow_roots(deep=True)
            for sr in shadow_roots:
                checkbox = await sr.query("span.cb-i", raise_exc=False)
                if checkbox:
                    await checkbox.click()
                    checkbox_clicked = True
                    print("   >> ✅ Turnstile checkbox 已点击（deep shadow root 定位，含 OOPIF）", flush=True)
                    break
        except Exception as e:
            last_err = e
        if checkbox_clicked:
            break
        await asyncio.sleep(0.5)

    if not checkbox_clicked:
        print(f"   >> deep shadow root 未找到 checkbox（{last_err}），降级为坐标点击...", flush=True)
        try:
            # 坐标点击兜底：iframe 本身在（非跨进程那层）shadow DOM 里，
            # 亮 DOM 的 tab.find() 看不到它，必须先拿到外层 shadow root 再 query。
            shadow_root = await tab._find_cloudflare_shadow_root(timeout=5)
            iframe_el = await shadow_root.query(
                'iframe[src*="challenges.cloudflare.com"]', timeout=5, raise_exc=False
            )
            if not iframe_el:
                print("   >> ❌ 未找到 Turnstile iframe，放弃点击", flush=True)
                await take_screenshot(browser, tab, str(SHOT_DIR / "turnstile_iframe_not_found.png"))
                return False

            bounds = await iframe_el.get_bounds_using_js()
            x = bounds["x"] + 25
            y = bounds["y"] + bounds["height"] / 2
            print(f"   >> 坐标点击 Turnstile checkbox ({x:.0f}, {y:.0f})", flush=True)
            await tab.mouse.click(x, y, humanize=True)
        except Exception as e2:
            print(f"   >> ❌ 坐标点击也失败: {e2}", flush=True)
            await take_screenshot(browser, tab, str(SHOT_DIR / "turnstile_click_failed.png"))
            return False

    # 阶段3：点击后等待 token 写入确认
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
        try:
            async with tab.expect_and_bypass_cloudflare_captcha():
                await tab.go_to("https://dashboard.katabump.com/auth/login")
        except Exception:
            await tab.go_to("https://dashboard.katabump.com/auth/login")
        await asyncio.sleep(1.5)

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
        await asyncio.sleep(0.3)

        # ★ 登录页 Cloudflare Turnstile 勾选框（"请验证您是真人"）
        has_turnstile = await _js(tab, "!!document.querySelector('input[name=\"cf-turnstile-response\"]')")
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
