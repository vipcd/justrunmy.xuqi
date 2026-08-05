#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import random
import socket
import subprocess
import time
from pathlib import Path

import requests
from seleniumbase import SB

APP_URL = os.getenv("JUSTRUNMY_APP_URL", "").strip() or "https://justrunmy.app/panel/application/46186/"
SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", "screenshots"))
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
RAW_COOKIE = os.getenv("JUSTRUNMY_COOKIE", "").strip()
ssh_process = None


def notify(message):
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message}, timeout=15,
        ).raise_for_status()
        print("📩 Telegram 通知发送成功！")
    except Exception as exc:
        print(f"⚠️ Telegram 通知失败: {exc}")


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def start_proxy():
    global ssh_process
    host = os.getenv("SSH_HOST", "").strip()
    user = os.getenv("SSH_USER", "").strip()
    password = os.getenv("SSH_PASS", "")
    port = os.getenv("SSH_PORT", "22").strip() or "22"
    socks_port = int(os.getenv("SOCKS_PORT", "51080"))
    if not host or not user:
        print("⚠️ 未配置 SSH 代理，使用直连")
        return None
    cmd = ["sshpass", "-p", password, "ssh", "-N", "-D", f"127.0.0.1:{socks_port}",
           "-p", port, "-o", "StrictHostKeyChecking=no", "-o", "ExitOnForwardFailure=yes",
           "-o", "ServerAliveInterval=30", f"{user}@{host}"]
    ssh_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not wait_port(socks_port):
        err = ssh_process.stderr.read().decode("utf-8", "replace") if ssh_process.stderr else ""
        raise RuntimeError(f"SSH 动态隧道启动失败: {err[-500:]}")
    proxy = f"socks5://127.0.0.1:{socks_port}"
    print(f"✅ SSH 动态隧道已就绪: {proxy}")
    return proxy


def save_shot(sb, name):
    path = SCREENSHOT_DIR / name
    try:
        sb.save_screenshot(str(path))
        print(f"📸 截图已保存: {path}")
    except Exception as exc:
        print(f"⚠️ 截图保存失败: {exc}")


def get_turnstile_token(sb):
    """读取页面中的 Turnstile 响应 Token。Selenium 脚本必须显式 return。"""
    script = """
    return (() => {
        const selectors = [
            'input[name="cf-turnstile-response"]',
            'textarea[name="cf-turnstile-response"]',
            'input[id^="cf-chl-widget-"][id$="_response"]',
            'textarea[id^="cf-chl-widget-"][id$="_response"]'
        ];
        for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
                const value = (el.value || el.getAttribute('value') || '').trim();
                if (value.length > 20) return value;
            }
        }
        return '';
    })();
    """
    try:
        return sb.execute_script(script) or ""
    except Exception:
        return ""


def get_turnstile_rect(sb):
    """返回 Turnstile 外层控件在浏览器视口内的矩形。"""
    script = """
    return (() => {
        const selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="/cdn-cgi/challenge-platform/"]',
            'iframe[title*="Cloudflare"]',
            '.cf-turnstile',
            '[data-sitekey]'
        ];
        for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
                const r = el.getBoundingClientRect();
                if (r.width >= 40 && r.height >= 30 &&
                    r.bottom > 0 && r.right > 0 &&
                    r.top < window.innerHeight && r.left < window.innerWidth) {
                    return {
                        left: r.left,
                        top: r.top,
                        width: r.width,
                        height: r.height
                    };
                }
            }
        }
        return null;
    })();
    """
    try:
        return sb.execute_script(script)
    except Exception:
        return None


def human_cdp_click(sb, click_x, click_y):
    """通过 CDP 分步移动、停顿、按下和抬起，产生浏览器原生鼠标事件。"""
    click_x = float(click_x)
    click_y = float(click_y)
    start_x = max(5.0, click_x - random.uniform(55, 110))
    start_y = max(5.0, click_y + random.uniform(25, 70))
    steps = random.randint(10, 16)

    for step in range(1, steps + 1):
        progress = step / steps
        eased = 1 - (1 - progress) ** 2
        x = start_x + (click_x - start_x) * eased
        y = start_y + (click_y - start_y) * eased
        if step != steps:
            x += random.uniform(-1.2, 1.2)
            y += random.uniform(-0.8, 0.8)
        sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "button": "none"
        })
        time.sleep(random.uniform(0.018, 0.045))

    time.sleep(random.uniform(0.08, 0.18))
    sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": click_x, "y": click_y,
        "button": "left", "buttons": 1, "clickCount": 1
    })
    time.sleep(random.uniform(0.055, 0.11))
    sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": click_x, "y": click_y,
        "button": "left", "buttons": 0, "clickCount": 1
    })


def click_element_like_human(sb, selector):
    """对普通页面按钮执行带鼠标轨迹的原生坐标点击。"""
    try:
        element = sb.find_element(selector)
        sb.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});", element
        )
        time.sleep(random.uniform(0.15, 0.35))
        rect = sb.driver.execute_script("""
            const r = arguments[0].getBoundingClientRect();
            return {left:r.left, top:r.top, width:r.width, height:r.height};
        """, element)
        if rect and rect.get("width", 0) > 0 and rect.get("height", 0) > 0:
            human_cdp_click(
                sb,
                rect["left"] + rect["width"] / 2,
                rect["top"] + rect["height"] / 2,
            )
            return
    except Exception as exc:
        print(f"⚠️ 原生坐标点击失败，回退 WebDriver 点击: {exc}")
    sb.click(selector)


def run_gui_captcha_click(sb):
    """调用 SeleniumBase 官方 GUI 验证码点击能力作为坐标点击的补充。"""
    for method_name in ("uc_gui_click_cf", "uc_gui_click_captcha", "solve_captcha"):
        method = getattr(sb, method_name, None)
        if callable(method):
            try:
                print(f"🖱️ 调用 SeleniumBase {method_name}() 点击验证框...")
                method()
                return True
            except Exception as exc:
                print(f"⚠️ {method_name}() 执行失败: {exc}")
    return False


def force_cdp_click_cf(sb):
    """点击 Turnstile 复选框，并等待页面生成响应 Token。"""
    token = get_turnstile_token(sb)
    if token:
        print("🎉 Cloudflare 已自动通过，无需再次点击。")
        return True

    rect = get_turnstile_rect(sb)
    if rect:
        checkbox_offset = min(30.0, max(22.0, float(rect["width"]) * 0.08))
        click_x = float(rect["left"]) + checkbox_offset
        click_y = float(rect["top"]) + float(rect["height"]) / 2
        print(f"📍 Turnstile 复选框坐标: X={click_x:.1f}, Y={click_y:.1f}")
        try:
            human_cdp_click(sb, click_x, click_y)
            print("✅ 已执行带鼠标轨迹的 Turnstile 原生点击。")
        except Exception as exc:
            print(f"⚠️ CDP 原生点击异常: {exc}")
    else:
        print("⚠️ DOM 中未直接找到 Turnstile 外框，将使用 GUI 识别点击。")

    print("⏳ 等待 Cloudflare 生成 Token...")
    for second in range(12):
        time.sleep(1)
        if get_turnstile_token(sb):
            print(f"🎉 Turnstile 验证通过，{second + 1} 秒后捕获 Token。")
            return True

    if run_gui_captcha_click(sb):
        for second in range(15):
            time.sleep(1)
            if get_turnstile_token(sb):
                print(f"🎉 GUI 点击后验证通过，{second + 1} 秒后捕获 Token。")
                return True

    print("⚠️ 未能从顶层 DOM 读到 Token；仍将按真实页面流程点击 Just Reset，交由服务端确认。")
    return False


def first_visible(sb, selectors, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        for selector in selectors:
            try:
                if sb.is_element_visible(selector):
                    return selector
            except Exception:
                pass
        time.sleep(0.5)
    return None


def main():
    if not RAW_COOKIE:
        raise RuntimeError("缺少 JUSTRUNMY_COOKIE 环境变量，无法注入 Cookie 登录！")

    proxy = start_proxy()
    kwargs = dict(uc=True, headless=False, locale="en-US")
    if proxy:
        kwargs["proxy"] = proxy

    with SB(**kwargs) as sb:
        try:
            sb.maximize_window()
            # 1. 打开首页并注入 Cookie
            sb.open("https://justrunmy.app")
            sb.sleep(2)

            for cookie_pair in RAW_COOKIE.split(";"):
                if "=" in cookie_pair:
                    parts = cookie_pair.strip().split("=", 1)
                    if len(parts) == 2:
                        c_name, c_val = parts
                        try:
                            sb.add_cookie({
                                "name": c_name.strip(),
                                "value": c_val.strip(),
                                "domain": "justrunmy.app"
                            })
                        except Exception:
                            pass
            print("🍪 已成功注入 Cookie")

            # 2. 直奔应用详情页
            sb.open(APP_URL)
            sb.wait_for_ready_state_complete(timeout=30)
            sb.sleep(5)

            current_url = (sb.get_current_url() or "").lower()
            if "/account/login" in current_url:
                save_shot(sb, "cookie_expired.png")
                raise RuntimeError("Cookie 已失效，页面被重定向到了登录页，请更换最新的 JUSTRUNMY_COOKIE！")

            print(f"✅ 成功进入应用页面: {sb.get_current_url()}")

            # 3. 最多尝试 3 次：打开弹窗 -> 点击真人验证 -> 点击 Just Reset。
            reset_xpath = ("//button[contains(translate(normalize-space(.), "
                           "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reset timer')]")
            confirm_xpath = ("//button[contains(translate(normalize-space(.), "
                             "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'just reset')]")

            final_result = None
            last_error = "未知错误"
            for attempt in range(1, 4):
                if attempt > 1:
                    print(f"🔄 第 {attempt} 次尝试：刷新页面以获取新的 Turnstile challenge...")
                    sb.driver.refresh()
                    sb.wait_for_ready_state_complete(timeout=30)
                    sb.sleep(4)

                reset = first_visible(sb, [reset_xpath], 25)
                if not reset:
                    save_shot(sb, f"renew_reset_btn_not_found_{attempt}.png")
                    last_error = "找不到 Reset timer 按钮"
                    continue

                click_element_like_human(sb, reset)
                print("✅ 已原生点击 Reset timer，等待续期弹窗和 Cloudflare 渲染...")
                sb.sleep(4)
                save_shot(sb, f"renew_confirmation_opened_{attempt}.png")

                token_ready = force_cdp_click_cf(sb)
                if not token_ready:
                    print("⚠️ 本地未读取到 Token，但继续点击 Just Reset 让服务端进行最终校验。")

                confirm_btn = first_visible(sb, [confirm_xpath], 12)
                if not confirm_btn:
                    save_shot(sb, f"confirm_btn_not_found_{attempt}.png")
                    last_error = "未找到 Just Reset 按钮"
                    continue

                print("👉 正在以原生鼠标事件点击 Just Reset...")
                click_element_like_human(sb, confirm_btn)

                disappeared_since = None
                result = None
                result_text = ""
                deadline = time.time() + 18
                while time.time() < deadline:
                    time.sleep(0.5)
                    page_text = (sb.get_page_source() or "").lower()

                    if "please complete the captcha" in page_text:
                        result = "captcha_error"
                        result_text = "服务端提示 Please complete the captcha"
                        break

                    if ("can't reset your" in page_text or
                            "cannot reset your" in page_text or
                            "not available for reset" in page_text):
                        result = "not_due"
                        result_text = "当前尚未到可重置时间"
                        break

                    if any(marker in page_text for marker in (
                        "timer has been reset", "timer reset successfully",
                        "successfully reset", "reset successful"
                    )):
                        result = "success"
                        break

                    try:
                        visible = sb.is_element_visible(confirm_btn)
                    except Exception:
                        visible = False
                    if not visible:
                        if disappeared_since is None:
                            disappeared_since = time.time()
                        elif time.time() - disappeared_since >= 2:
                            result = "success"
                            break
                    else:
                        disappeared_since = None

                save_shot(sb, f"renew_result_{attempt}.png")

                if result == "success":
                    final_result = "success"
                    break
                if result == "not_due":
                    final_result = "not_due"
                    last_error = result_text
                    break
                if result == "captcha_error":
                    last_error = result_text
                    print(f"⚠️ 第 {attempt} 次验证未被服务端接受，将刷新后重试。")
                    continue

                last_error = "点击 Just Reset 后未检测到明确成功状态"
                print(f"⚠️ {last_error}，将刷新后重试。")

            if final_result == "success":
                save_shot(sb, "renew_success.png")
                print("🎉 自动续期成功！")
                notify("✅ JustRunMy.app 自动续期成功！")
            elif final_result == "not_due":
                save_shot(sb, "renew_not_due.png")
                print(f"⏳ {last_error}，本次无需续期。")
                notify(f"⏳ JustRunMy.app 本次无需续期: {last_error}")
            else:
                save_shot(sb, "renew_captcha_failed.png")
                raise RuntimeError(f"续期失败，已重试 3 次: {last_error}")

        except Exception as exc:
            save_shot(sb, "renew_failed.png")
            notify(f"❌ JustRunMy.app 自动续期失败: {exc}")
            raise
        finally:
            if ssh_process and ssh_process.poll() is None:
                ssh_process.terminate()


if __name__ == "__main__":
    main()
