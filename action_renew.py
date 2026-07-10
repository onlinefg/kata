"""
Katabump 自动续期 — CloakBrowser 版
- CF Turnstile 先等自动通过，过不去再坐标点击（移植自 runfc）
- ALTCHA modal JS 点击
- 多账号 USERS_JSON，WxPusher 通知
"""
import os, re, json, time, random, logging, sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PROXY_SERVER       = "socks5://127.0.0.1:10808"
WXPUSHER_APP_TOKEN = os.getenv("WXPUSHER_APP_TOKEN", "")
WXPUSHER_UID       = os.getenv("WXPUSHER_UID", "")
SCREENSHOT_DIR     = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

BASE_URL   = "https://dashboard.katabump.com"
LOGIN_URL  = f"{BASE_URL}/auth/login"
LOGOUT_URL = f"{BASE_URL}/auth/logout"

# ---------- 工具 ----------
def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    parts = domain.split(".")
    return f"{local[0]}***@{parts[0][0]}***.{'.'.join(parts[1:])}"

def mask_sid(sid) -> str:
    s = str(sid)
    return s[:2] + "***" if len(s) > 2 else "***"

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
        pass
    return []

def wxpush(content: str):
    if not WXPUSHER_APP_TOKEN or not WXPUSHER_UID:
        log.warning("WxPusher 未配置，跳过")
        return
    import urllib.request
    payload = json.dumps({
        "appToken": WXPUSHER_APP_TOKEN,
        "content": content,
        "contentType": 1,
        "uids": [WXPUSHER_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                log.info("📨 WxPusher 推送成功")
            else:
                log.warning(f"📨 WxPusher 失败: {result}")
    except Exception as e:
        log.warning(f"📨 WxPusher 异常: {e}")

def take_screenshot(page, name: str):
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path, full_page=False)
        log.info(f"📸 {path}")
    except Exception as e:
        log.warning(f"截图失败: {e}")

def get_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""

def human_delay(min_s=0.3, max_s=0.8):
    time.sleep(random.uniform(min_s, max_s))

def js_eval(page, expr: str):
    try:
        return page.evaluate(f"() => {{ {expr} }}")
    except Exception as e:
        log.warning(f"js_eval 失败: {e}")
        return None

# ---------- CF Turnstile ----------
def is_cf_blocked(page) -> bool:
    try:
        has_widget = page.evaluate("""() => !!(
            document.querySelector('iframe[src*="challenges.cloudflare.com"]') ||
            document.querySelector('input[name="cf-turnstile-response"]') ||
            document.querySelector('.cf-turnstile') ||
            document.querySelector('#challenge-stage') ||
            document.querySelector('#cf-wrapper')
        )""")
        if has_widget:
            return True
    except Exception:
        pass
    try:
        body = get_text(page).lower()
        return "verify you are human" in body or "ray id" in body or (
            "cloudflare" in body and "security" in body
        )
    except Exception:
        return False

def wait_cf_pass(page, timeout=45) -> bool:
    log.info("等待 CF 自动通过...")
    for i in range(timeout):
        if not is_cf_blocked(page):
            log.info(f"✅ CF 自动通过（{i}s）")
            return True
        if i % 5 == 0 and i > 0:
            log.info(f"  CF 等待中... {i}s")
        time.sleep(1)
    return False

def _find_stable_cf_frame(page, stable_timeout=12):
    """连续 2 次 bounding_box 一致才返回，避免拿到正在被 CF 替换的旧 frame"""
    deadline = time.time() + stable_timeout
    while time.time() < deadline:
        cf_frame = None
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if not cf_frame:
            time.sleep(0.3)
            continue
        try:
            box1 = cf_frame.frame_element().bounding_box()
        except Exception as e:
            log.warning(f"  [稳定检测] frame_element 失败: {e}")
            time.sleep(0.3)
            continue
        if not box1:
            time.sleep(0.3)
            continue
        time.sleep(0.3)
        try:
            box2 = cf_frame.frame_element().bounding_box()
        except Exception:
            time.sleep(0.3)
            continue
        if box2 and abs(box2["y"] - box1["y"]) < 5:
            log.info(f"  ✅ CF frame 稳定 box={box2}")
            return cf_frame, box2
        log.warning(f"  [稳定检测] 位置漂移 {box1} -> {box2}")
        time.sleep(0.3)
    return None, None

def click_cf_checkbox(page, timeout=45) -> bool:
    """找稳定 CF frame → 坐标点击 → 等待登录页就绪"""
    def login_ready() -> bool:
        try:
            return page.locator(
                'input[name="email"], input[type="email"]'
            ).first.is_visible(timeout=500)
        except Exception:
            return False

    log.info("【CF】查找稳定 CF iframe...")
    frames = page.frames
    log.info(f"  当前 {len(frames)} 个 frame：" +
             " | ".join((f.url or "about:blank")[:80] for f in frames[:5]))

    cf_frame, box = _find_stable_cf_frame(page, stable_timeout=12)

    if not cf_frame or not box:
        log.warning("【CF】未找到稳定 CF frame，坐标点击跳过")
        take_screenshot(page, "cf_no_frame")
        return False

    x = box["x"] + 25
    y = box["y"] + box["height"] / 2
    try:
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.2, 0.4))
        page.mouse.click(x, y)
        log.info(f"  ✅ 坐标点击 ({x:.0f}, {y:.0f})")
    except Exception as e:
        log.warning(f"  坐标点击失败: {e}")
        take_screenshot(page, "cf_click_failed")
        return False

    take_screenshot(page, "cf_clicked")
    log.info(f"【CF】等待登录页（最多 {timeout}s）...")
    for i in range(timeout * 2):
        if login_ready():
            log.info(f"  ✅ 登录页就绪（{i * 0.5:.1f}s）")
            return True
        if i % 10 == 0 and i > 0:
            take_screenshot(page, f"cf_wait_{i}")
        time.sleep(0.5)

    log.error("【CF】等待超时")
    take_screenshot(page, "cf_timeout")
    return False

def wait_for_page_settle(page, settle_timeout=10):
    deadline = time.time() + settle_timeout
    while time.time() < deadline:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        if is_cf_blocked(page):
            log.info("  页面稳定（CF 就绪）")
            return
        if len(body.strip()) > 100:
            log.info("  页面稳定（内容就绪）")
            return
        time.sleep(0.5)
    log.info("  页面稳定等待超时，继续...")

def navigate(page, url) -> bool:
    log.info(f"导航: {url}")
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"goto 异常: {e}，继续...")

    wait_for_page_settle(page, settle_timeout=12)

    if not is_cf_blocked(page):
        return True

    # 先等自动通过（Managed Challenge 通常 20-35s）
    if wait_cf_pass(page, timeout=40):
        return True

    # 自动没过，坐标点击
    log.info("CF 被动未通过，尝试坐标点击...")
    if click_cf_checkbox(page, timeout=45):
        return True

    # 最后刷新兜底
    log.info("坐标点击未过，刷新兜底...")
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    if not is_cf_blocked(page):
        return True
    if wait_cf_pass(page, timeout=30):
        return True
    return click_cf_checkbox(page, timeout=30)

# ---------- ALTCHA modal ----------
def solve_altcha_in_modal(page) -> bool:
    log.info("  >> 处理 modal 内 ALTCHA...")
    # 等 altcha-widget 出现
    for _ in range(20):
        exists = page.evaluate(
            "() => !!document.querySelector('#renew-modal altcha-widget')"
        )
        if exists:
            break
        time.sleep(0.5)
    else:
        log.warning("  >> modal 内未发现 ALTCHA")
        return False

    for attempt in range(1, 6):
        log.info(f"  >> ALTCHA JS 点击 {attempt}/5")
        clicked = page.evaluate("""() => {
            const cb = document.querySelector('#renew-modal altcha-widget input[type="checkbox"]');
            if (!cb) return false;
            cb.focus();
            cb.click();
            cb.dispatchEvent(new Event('input', {bubbles: true}));
            cb.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""")
        if not clicked:
            time.sleep(1)
            continue

        for _ in range(12):
            time.sleep(1)
            state = page.evaluate("""() => {
                const w = document.querySelector('#renew-modal altcha-widget .altcha');
                if (!w) return 'no-widget';
                return w.getAttribute('data-state') || 'unknown';
            }""")
            log.info(f"  >> ALTCHA state={state}")
            if state == "verified":
                log.info("  >> ✅ ALTCHA verified")
                page.evaluate("""() => {
                    const modal = document.querySelector('#renew-modal');
                    if (!modal) return;
                    const btn = modal.querySelector('button[type="submit"]');
                    if (btn) setTimeout(() => btn.click(), 300);
                }""")
                time.sleep(2)
                return True
            if state == "error":
                log.warning("  >> ALTCHA error，重试")
                break
        time.sleep(0.5)

    log.error("  >> ALTCHA 多次失败")
    return False

# ---------- 获取下次续期时间 ----------
def get_next_renew_time(page) -> str:
    try:
        info = page.evaluate("""() => {
            const lines = document.body.innerText.split('\\n');
            let expiry = null, period = null;
            for (let i = 0; i < lines.length; i++) {
                if (lines[i].includes('Expiry'))
                    expiry = lines[i].replace('Expiry', '').trim();
                if (lines[i].includes('Renew period'))
                    period = (lines[i+1] || '').trim();
            }
            if (expiry) return JSON.stringify({expiry, period});
            return null;
        }""")
        if info and info != "null":
            d = json.loads(info)
            expiry = d.get("expiry", "")
            period = d.get("period", "")
            if expiry:
                return f"到期日: {expiry}" + (f" (周期: {period})" if period else "")
    except Exception:
        pass

    # 正则兜底
    try:
        body = get_text(page)
        m = re.search(
            r'expir\w*\s*[:\-]?\s*([\d]{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})',
            body, re.I
        )
        if m:
            return f"到期日: {m.group(1).strip()}"
    except Exception:
        pass

    return "续期成功（未能获取具体时间）"

# ---------- 登录 ----------
def do_login(page, user: dict) -> bool:
    u, p = user["username"], user["password"]

    # 先清理旧 session
    try:
        page.goto(LOGOUT_URL, timeout=15000, wait_until="domcontentloaded")
        time.sleep(1)
    except Exception:
        pass

    for attempt in range(1, 4):
        log.info(f"\n🔑 登录 {attempt}/3: {mask_email(u)}")

        if not navigate(page, LOGIN_URL):
            log.error("  CF 验证失败，重试")
            continue

        # 检查是否已登录
        if "login" not in page.url and "dashboard" in page.url:
            log.info("  ✅ Session 仍有效")
            return True

        try:
            page.wait_for_selector(
                'input[name="email"], input[type="email"]',
                timeout=10000
            )
        except Exception:
            log.warning("  找不到邮箱输入框，重试")
            take_screenshot(page, f"login_no_input_{attempt}")
            continue

        log.info("  填写表单...")
        try:
            email_el = page.locator('input[name="email"], input[type="email"]').first
            email_el.click()
            email_el.fill("")
            page.type('input[name="email"], input[type="email"]', u, delay=random.randint(60, 140))
            human_delay()

            pass_el = page.locator('input[name="password"], input[type="password"]').first
            pass_el.click()
            pass_el.fill("")
            page.type('input[name="password"], input[type="password"]', p, delay=random.randint(60, 140))
            human_delay()
        except Exception as e:
            log.warning(f"  填表失败: {e}")
            continue

        # Turnstile token 等待（CloakBrowser 通常自动处理，这里给 10s）
        log.info("  等待 Turnstile token...")
        for _ in range(20):
            has_token = page.evaluate("""() => {
                function deepQ(root, sel) {
                    let el = root.querySelector(sel);
                    if (el) return el;
                    for (const h of root.querySelectorAll('*'))
                        if (h.shadowRoot) { el = deepQ(h.shadowRoot, sel); if (el) return el; }
                    return null;
                }
                const el = deepQ(document, 'input[name="cf-turnstile-response"]');
                return el ? el.value.length > 10 : false;
            }""")
            if has_token:
                log.info("  ✅ Turnstile token 就绪")
                break

            # 检查是否需要手动点击
            has_cf = page.evaluate("""() => !!(
                document.querySelector('div.cf-turnstile') ||
                document.querySelector('input[name="cf-turnstile-response"]')
            )""")
            if has_cf:
                time.sleep(0.5)
            else:
                break  # 无 Turnstile，直接登录
        else:
            # 10s 未自动通过 → 手动坐标点击
            log.info("  Turnstile 未自动通过，尝试坐标点击...")
            cf_frame, box = _find_stable_cf_frame(page, stable_timeout=10)
            if cf_frame and box:
                x = box["x"] + 25
                y = box["y"] + box["height"] / 2
                try:
                    page.mouse.move(x, y)
                    time.sleep(random.uniform(0.2, 0.4))
                    page.mouse.click(x, y)
                    log.info(f"  坐标点击 ({x:.0f}, {y:.0f})")
                    # 等 token
                    for _ in range(20):
                        time.sleep(0.5)
                        ok = page.evaluate("""() => {
                            function deepQ(root, sel) {
                                let el = root.querySelector(sel);
                                if (el) return el;
                                for (const h of root.querySelectorAll('*'))
                                    if (h.shadowRoot) { el = deepQ(h.shadowRoot, sel); if (el) return el; }
                                return null;
                            }
                            const el = deepQ(document, 'input[name="cf-turnstile-response"]');
                            return el ? el.value.length > 10 : false;
                        }""")
                        if ok:
                            log.info("  ✅ Turnstile token 就绪（坐标点击后）")
                            break
                    else:
                        log.warning("  Turnstile token 仍未就绪，继续尝试登录")
                except Exception as e:
                    log.warning(f"  坐标点击失败: {e}")
            else:
                log.warning("  未找到 CF frame，继续尝试登录")

        take_screenshot(page, f"login_before_submit_{attempt}")
        log.info("  点击 Login 按钮...")
        try:
            page.locator("button[type='submit']").first.click()
        except Exception:
            page.evaluate("""() => {
                const btn = document.querySelector('button[type="submit"]');
                if (btn) btn.click();
            }""")

        # 等跳转
        for _ in range(12):
            time.sleep(1)
            if "login" not in page.url and ("dashboard" in page.url or page.url == BASE_URL + "/"):
                log.info("  ✅ 登录成功")
                take_screenshot(page, "login_success")
                return True

        log.warning(f"  登录后未跳转（url={page.url}），重试")
        take_screenshot(page, f"login_no_redirect_{attempt}")

    return False

# ---------- 续期 ----------
def do_renew(page, user: dict):
    u   = user["username"]
    sid = user.get("serverId") or os.getenv("KATABUMP_SERVER_ID", "")
    if not sid:
        log.error("  未提供 serverId，跳过")
        return False, "缺少 serverId"
    sf = u.replace("@", "_").replace(".", "_")

    url = f"{BASE_URL}/servers/edit?id={sid}"
    log.info(f"  导航到 servers/edit?id={mask_sid(sid)}")
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        time.sleep(2)
    except Exception as e:
        log.warning(f"  导航失败: {e}")

    for attempt in range(1, 4):
        log.info(f"\n🔄 续期 {attempt}/3: {mask_email(u)}")

        # 找 Renew 按钮
        try:
            btn = page.locator("button", has_text="Renew").first
            btn.wait_for(timeout=5000, state="visible")
        except Exception:
            log.warning("  找不到 Renew 按钮")
            return False, "找不到 Renew 按钮"

        btn.click()
        log.info("  已点击 Renew，等待 modal...")

        # 等 modal 出现
        modal_visible = False
        for _ in range(10):
            time.sleep(1)
            visible = page.evaluate("""() => {
                const m = document.querySelector('#renew-modal');
                return m && m.getBoundingClientRect().width > 0;
            }""")
            if visible:
                modal_visible = True
                break

        if not modal_visible:
            log.warning("  modal 未出现，刷新重试")
            page.reload(wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            continue

        log.info("  modal 已打开，处理 ALTCHA...")
        if not solve_altcha_in_modal(page):
            log.warning("  ALTCHA 失败，刷新重试")
            page.reload(wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            continue

        # 等续期结果
        for _ in range(8):
            time.sleep(1)
            gone = page.evaluate("() => !document.querySelector('#renew-modal')")
            if gone:
                log.info("  ✅ 续期成功！")
                time.sleep(2)
                next_time = get_next_renew_time(page)
                take_screenshot(page, f"{sf}_ok")
                return True, next_time

            # 检查"不能续期"提示
            body = get_text(page)
            m = re.search(r"You can't renew[^.]*\.[^.]*?\.", body, re.I)
            if not m:
                m = re.search(r"You can't renew[^.]*\.", body, re.I)
            if m:
                msg = re.sub(r'\s+', ' ', m.group(0)).strip()
                log.info(f"  ⏳ {msg}")
                take_screenshot(page, f"{sf}_skip")
                return False, msg

        page.reload(wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

    return False, "多次尝试未成功"

# ---------- 主流程 ----------
def main():
    users = load_users()
    if not users:
        log.error("❌ 未设置 USERS_JSON")
        sys.exit(1)

    from cloakbrowser import launch
    log.info("启动 CloakBrowser...")
    browser = launch(
        headless=False,
        humanize=True,
        proxy=PROXY_SERVER,
        geoip=True,
    )
    page = browser.new_page()

    results = []
    try:
        for user in users:
            u = user["username"]
            log.info(f"\n{'='*40} [{mask_email(u)}] {'='*40}")
            try:
                if not do_login(page, user):
                    results.append((u, False, "登录失败"))
                    continue
                ok, detail = do_renew(page, user)
                results.append((u, ok, detail))
            except Exception as e:
                import traceback
                log.exception(e)
                take_screenshot(page, f"error_{u.replace('@','_')}")
                results.append((u, False, f"异常: {str(e)[:60]}"))

        # WxPusher 汇总
        if results:
            lines = ["📊 Katabump 自动续期报告"]
            for name, ok, detail in results:
                icon = "✅" if ok else "❌"
                lines.append(f"\n{icon} {name}")
                if detail:
                    lines.append(f"   {detail}")
            wxpush("\n".join(lines).strip())

        log.info("\n所有流程结束，5s 后关闭...")
        time.sleep(5)

    finally:
        try:
            browser.close()
        except Exception:
            pass

    log.info("✅ 整体流程结束")

if __name__ == "__main__":
    main()
