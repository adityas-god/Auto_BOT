"""
Headless Playwright Grafana Capture & Screenshot Engine for GreyOrange OpsBot.
Provides:
- 100% Headless Chromium browser automation via Playwright
- Zero-Trust Proxy integration (local Cloudflare Warp / Socks5)
- Automated Google SSO OAuth flow traversal and session caching
- Cookie and token injection for persistent Grafana access
- Multi-layer data extraction (DOM parser + OCR)
"""

import os
import sys
import time
import tempfile
import json
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

from core.config import BASE_DIR
from core.database import SiteManager
from core.auth import resolve_link_auth
from core.logger import bot_log
from core.utils import safe_int, get_effective_proxy
from engines.ocr_engine import extract_image_ocr
from engines.page_extractor import extract_page_data


class GrafanaCapture:
    def __init__(self, site_dict=None):
        self.site = site_dict or {}
        self.global_settings = SiteManager.get_global_settings()

    def capture_screenshot(self, target_url=None, output_path=None, username=None, password=None, token=None, return_extracted=False):
        url_to_capture = target_url or self.site.get("grafana_url")
        if not url_to_capture:
            raise ValueError("No Grafana URL configured.")

        if not output_path:
            tmp_dir = tempfile.gettempdir()
            filename = f"grafana_{self.site.get('id', 'snap')}_{int(time.time() * 1000)}.png"
            output_path = os.path.join(tmp_dir, filename)

        prepared_url = url_to_capture.strip()
        site_name = self.site.get("name", "Site")
        bot_log(f"[{site_name}] Navigating headlessly to Grafana: {prepared_url}", site_id=self.site.get("id"))

        # Resolve best credentials using intelligent fallback
        user, pwd, auth_token = resolve_link_auth(
            self.site,
            {"username": username, "password": password, "token": token, "url": prepared_url},
            target_url=prepared_url
        )

        parsed_u = urlparse(prepared_url)
        base_url = f"{parsed_u.scheme}://{parsed_u.netloc}"
        c_domain = parsed_u.hostname or "127.0.0.1"
        is_https = prepared_url.lower().startswith("https")

        is_bearer_token = False
        extra_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if auth_token:
            auth_token_str = str(auth_token).strip()
            if auth_token_str.startswith("ey") or auth_token_str.startswith("glsa_") or auth_token_str.startswith("glpat_"):
                is_bearer_token = True
                extra_headers["Authorization"] = f"Bearer {auth_token_str}"

        cookie_dict = {}
        session_key = ""
        if auth_token and not is_bearer_token:
            cookie_raw = str(auth_token).strip()
            if "=" in cookie_raw:
                for part in cookie_raw.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k and v:
                            cookie_dict[k] = v
            else:
                cookie_dict["grafana_session"] = cookie_raw

            if "grafana_session" in cookie_dict:
                session_key = cookie_dict["grafana_session"].strip("'\"")
                cookie_dict["grafana_session"] = session_key

            # Always ensure grafana_session_expiry is set to far future (+1 year)
            # so Grafana's client-side React router NEVER triggers a false redirect to /login!
            cookie_dict["grafana_session_expiry"] = str(int(time.time()) + 31536000)
            cookie_dict["grafana_logged_in"] = "true"

        auth_state_path = os.path.join(BASE_DIR, "grafana_auth_state.json")
        has_auth_state = os.path.exists(auth_state_path) and os.path.isfile(auth_state_path)

        with sync_playwright() as p:
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--hide-scrollbars",
                "--mute-audio",
                "--ignore-certificate-errors",
                "--disable-web-security",
                "--disable-blink-features=AutomationControlled"
            ]
            launch_kwargs = {"headless": True, "args": launch_args}
            effective_proxy = get_effective_proxy(self.global_settings.get("http_proxy"))
            if effective_proxy:
                launch_kwargs["proxy"] = {"server": effective_proxy}
                bot_log(f"[{site_name}] Playwright routing via Zero-Trust Proxy: {effective_proxy}", site_id=self.site.get("id"))

            try:
                browser = p.chromium.launch(**launch_kwargs)
            except Exception as e:
                err_str = str(e).lower()
                if "executable" in err_str or "playwright install" in err_str:
                    try:
                        chrome_kwargs = dict(launch_kwargs)
                        chrome_kwargs["channel"] = "chrome"
                        browser = p.chromium.launch(**chrome_kwargs)
                    except Exception:
                        raise e
                else:
                    raise e

            viewport_w = safe_int(self.global_settings.get("viewport_width"), 1920)
            viewport_h = safe_int(self.global_settings.get("viewport_height"), 1080)
            page_wait = safe_int(self.global_settings.get("page_load_wait_seconds"), 8)

            auth_state_path = os.path.join(BASE_DIR, "grafana_auth_state.json")
            context_kwargs = {
                "viewport": {"width": viewport_w, "height": viewport_h},
                "device_scale_factor": 1.0,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "extra_http_headers": extra_headers,
                "ignore_https_errors": True
            }
            # ALWAYS load persistent Google SSO session if available!
            # Having Google session cookies in the browser allows automatic Google SSO login
            # for ANY corporate Grafana server (gem-dash, cloudwatch, etc.)!
            temp_st_to_clean = None
            sanitized_cookies = []
            if os.path.exists(auth_state_path) and os.path.isfile(auth_state_path):
                try:
                    with open(auth_state_path, "r", encoding="utf-8") as af:
                        st_data = json.load(af)

                    now_ts = int(time.time())
                    far_future_ts = now_ts + 31536000
                    sanitized_cookies = []
                    for c in st_data.get("cookies", []):
                        c_name = c.get("name", "")
                        c_dom = c.get("domain", "")
                        # If link provides a dedicated cookie, discard conflicting old domain cookies from auth_state
                        if cookie_dict and c_name.startswith("grafana_session") and (c_domain in c_dom or c_dom in c_domain):
                            continue
                        # Ensure session expiry is in the future (+1 year)
                        if c_name == "grafana_session_expiry":
                            c["value"] = str(far_future_ts)
                            c["expires"] = far_future_ts
                        if c.get("expires", 0) and c["expires"] < now_ts:
                            c["expires"] = far_future_ts
                        # If target URL is HTTPS and cookie belongs to target domain, mark secure=True
                        if is_https and (c_domain in c_dom or c_dom in c_domain):
                            c["secure"] = True
                        sanitized_cookies.append(c)

                    st_data["cookies"] = sanitized_cookies
                    temp_st_to_clean = os.path.join(tempfile.gettempdir(), f"st_clean_{os.getpid()}_{int(time.time()*1000)}.json")
                    with open(temp_st_to_clean, "w", encoding="utf-8") as tf:
                        json.dump(st_data, tf)
                    context_kwargs["storage_state"] = temp_st_to_clean
                    bot_log(f"[{site_name}] Loaded persistent Google SSO session from grafana_auth_state.json", site_id=self.site.get("id"))
                except Exception as st_err:
                    bot_log(f"[{site_name}] Warning loading auth state: {st_err}", site_id=self.site.get("id"))
                    context_kwargs["storage_state"] = auth_state_path

            context = browser.new_context(**context_kwargs)

            # Inject dedicated session cookies cleanly using url=base_url and domain=c_domain:
            if cookie_dict:
                cookies_to_add = []
                future_ts = int(time.time()) + 31536000

                for c_name, c_val in cookie_dict.items():
                    is_sess = (c_name == "grafana_session")
                    val_str = str(c_val).strip()

                    # 1. Bind with url=base_url (Playwright matches exact scheme, host, and path)
                    cookies_to_add.append({
                        "name": c_name,
                        "value": val_str,
                        "url": base_url,
                        "path": "/",
                        "sameSite": "Lax",
                        "httpOnly": True if is_sess else False,
                        "secure": is_https,
                        "expires": future_ts
                    })
                    # 2. Bind with domain=c_domain
                    cookies_to_add.append({
                        "name": c_name,
                        "value": val_str,
                        "domain": c_domain,
                        "path": "/",
                        "sameSite": "Lax",
                        "httpOnly": True if is_sess else False,
                        "secure": is_https,
                        "expires": future_ts
                    })

                injected_names = set()
                for ck in cookies_to_add:
                    try:
                        context.add_cookies([ck])
                        injected_names.add(ck["name"])
                    except Exception:
                        pass
                bot_log(f"[{site_name}] Injected Grafana session cookies ({', '.join(injected_names)}) for {c_domain}", site_id=self.site.get("id"))
            elif os.path.exists(auth_state_path):
                try:
                    bound_names = set()
                    future_ts = int(time.time()) + 31536000
                    for ck in sanitized_cookies:
                        ck_name = ck.get("name", "")
                        ck_dom = ck.get("domain", "")
                        if (c_domain in ck_dom or ck_dom in c_domain) and ck_name in ("grafana_session", "grafana_session_expiry", "grafana_logged_in"):
                            val = str(ck.get("value", "")).strip()
                            if ck_name == "grafana_session_expiry":
                                val = str(future_ts)
                            context.add_cookies([{
                                "name": ck_name,
                                "value": val,
                                "url": base_url,
                                "path": "/",
                                "sameSite": "Lax",
                                "httpOnly": (ck_name == "grafana_session"),
                                "secure": is_https,
                                "expires": future_ts
                            }])
                            context.add_cookies([{
                                "name": ck_name,
                                "value": val,
                                "domain": c_domain,
                                "path": "/",
                                "sameSite": "Lax",
                                "httpOnly": (ck_name == "grafana_session"),
                                "secure": is_https,
                                "expires": future_ts
                            }])
                            bound_names.add(ck_name)
                    if bound_names:
                        bot_log(f"[{site_name}] Bound persistent Grafana cookies ({', '.join(bound_names)}) from auth_state to {base_url}", site_id=self.site.get("id"))
                except Exception as b_err:
                    bot_log(f"[{site_name}] Warning binding persistent session cookies: {b_err}", site_id=self.site.get("id"))

            page = context.new_page()

            # Pre-inject document.cookie before scripts load so client-side router never redirects
            effective_expiry = str(int(time.time()) + 31536000)
            sec_flag = "; Secure" if is_https else ""
            try:
                page.add_init_script(f"""
                    try {{
                        document.cookie = "grafana_session_expiry={effective_expiry}; path=/; max-age=31536000; SameSite=Lax{sec_flag}";
                        document.cookie = "grafana_logged_in=true; path=/; max-age=31536000; SameSite=Lax{sec_flag}";
                    }} catch(e) {{}}
                """)
            except Exception:
                pass

            try:
                try:
                    response = page.goto(prepared_url, wait_until="domcontentloaded", timeout=60000)
                    if response and response.status >= 400:
                        bot_log(f"[{site_name}] Server returned HTTP status {response.status}", site_id=self.site.get("id"))
                except Exception as nav_err:
                    err_str = str(nav_err)
                    if "ERR_CONNECTION_REFUSED" in err_str:
                        raise ValueError(f"Connection refused at '{prepared_url}'. Verify the service is running and accessible.")
                    if "ERR_NAME_NOT_RESOLVED" in err_str:
                        raise ValueError(f"DNS lookup failed for '{prepared_url}'. Verify your DNS, hostname, or VPN connection.")
                    if "Timeout" in err_str:
                        try:
                            bot_log(f"[{site_name}] DOM load timed out, attempting fallback commit navigation...", site_id=self.site.get("id"))
                            response = page.goto(prepared_url, wait_until="commit", timeout=20000)
                        except Exception:
                            raise TimeoutError(f"Connection timed out (60s) reaching '{prepared_url}'. The network/VPN/firewall cannot reach this server. Please ensure you are connected to the required VPN or corporate network.")
                    else:
                        raise RuntimeError(f"Navigation failed: {nav_err}")

                try:
                    page.wait_for_selector(
                        "input[type='password'], input[name='user'], button:has-text('Log in'), button:has-text('Sign in with Google'), a:has-text('Google'), [href*='google'], .react-grid-layout, .dashboard-container, [data-testid='dashboard-content'], .panel-container",
                        timeout=10000
                    )
                except Exception:
                    pass

                if "/login" in page.url or "/auth/" in page.url:
                    bot_log(f"[{site_name}] [AUTH] Page landed on auth route ({page.url}). Waiting for login components...", site_id=self.site.get("id"))
                    try:
                        page.wait_for_selector(
                            "a[href*='google'], a[href*='login/google'], button:has-text('Google'), a:has-text('Google'), [aria-label*='Google' i], [data-testid*='google' i], input[type='password'], input[name='user'], button[type='submit']",
                            timeout=6000
                        )
                    except Exception:
                        pass

                if auth_token and ("/login" in page.url or "/auth/" in page.url):
                    bot_log(f"[{site_name}] [AUTH] Cookie injected but still on auth page — waiting for redirect to complete...", site_id=self.site.get("id"))
                    try:
                        page.wait_for_url(lambda url: "/login" not in url and "/auth/" not in url, timeout=8000)
                    except Exception:
                        pass

                google_sso_selector = (
                    "a[href*='/login/google'], a[href*='login/google'], a[href*='google'], "
                    "a.btn-service--google, [data-testid*='google' i], [aria-label*='Google' i], "
                    "button:has-text('Google'), a:has-text('Google'), span:has-text('Google'), "
                    "div[role='button']:has-text('Google')"
                )
                google_sso_loc = page.locator(google_sso_selector)
                has_google_sso = google_sso_loc.count() > 0
                has_pass_input = page.locator("input[type='password'], input[name='password'], input[placeholder*='password' i]").count() > 0
                has_login_btn = page.locator("button:has-text('Log in'), button:has-text('Login'), button:has-text('Sign in'), button[type='submit']").count() > 0
                is_login = "/login" in page.url or "/auth/" in page.url or (has_pass_input and has_login_btn) or (has_google_sso and not has_pass_input)

                prep_path = urlparse(prepared_url).path
                if is_login:
                    if auth_token:
                        bot_log(f"[{site_name}] [AUTH NOTICE] Landed on login screen despite session cookie/token. Validating credentials or cookie expiration...", site_id=self.site.get("id"))
                    if has_google_sso:
                        bot_log(f"[{site_name}] [AUTH] Detected Google SSO button. Auto-authenticating via stored Google session...", site_id=self.site.get("id"))
                        google_btn = google_sso_loc.first
                        if google_btn.count() > 0:
                            try:
                                google_btn.click()
                                bot_log(f"[{site_name}] Clicked Google SSO button. Monitoring OAuth flow...", site_id=self.site.get("id"))

                                for _ in range(18):
                                    time.sleep(1)
                                    curr_u = page.url
                                    if "accounts.google.com" in curr_u:
                                        if "/challenge" in curr_u or "/signin/challenge" in curr_u or "/rejected" in curr_u:
                                            bot_log(f"[{site_name}] Google requires interactive password/2FA re-authentication: {curr_u}", site_id=self.site.get("id"))
                                            break

                                        acct_elem = page.locator("[data-identifier], [data-email], div[role='link']:has-text('@greyorange.com'), div[role='button']:has-text('@greyorange.com'), [data-authuser]").first
                                        if acct_elem.count() > 0 and acct_elem.is_visible():
                                            bot_log(f"[{site_name}] Google account chooser displayed — clicking account...", site_id=self.site.get("id"))
                                            try:
                                                acct_elem.click(timeout=3000)
                                            except Exception:
                                                pass
                                            time.sleep(1.5)

                                        consent_btn = page.locator("button:has-text('Continue'), button:has-text('Allow'), span:has-text('Continue'), span:has-text('Allow'), div[role='button']:has-text('Continue'), div[role='button']:has-text('Allow')").first
                                        if consent_btn.count() > 0 and consent_btn.is_visible():
                                            bot_log(f"[{site_name}] Google OAuth consent screen displayed — clicking Continue/Allow...", site_id=self.site.get("id"))
                                            try:
                                                consent_btn.click(timeout=3000)
                                            except Exception:
                                                pass
                                            time.sleep(1.5)

                                    elif base_url in curr_u and "/login/google" not in curr_u and "/login" not in curr_u and "/auth/" not in curr_u:
                                        break

                                try:
                                    page.wait_for_url(lambda u: base_url in u and ("/login/google" in u or "/login" not in u), timeout=20000)
                                    bot_log(f"[{site_name}] Returned from Google to Grafana: {page.url}", site_id=self.site.get("id"))
                                    time.sleep(3)
                                    if auth_state_path:
                                        context.storage_state(path=auth_state_path)
                                        bot_log(f"[{site_name}] Auto-saved new session state to {auth_state_path}", site_id=self.site.get("id"))

                                    try:
                                        for ck in context.cookies():
                                            if ck.get("name") == "grafana_session" and (c_domain in ck.get("domain", "") or not ck.get("domain")):
                                                val = ck.get("value")
                                                if val and len(val) >= 16:
                                                    s_id = self.site.get("id")
                                                    if s_id:
                                                        for lk in SiteManager.get_site_links(s_id):
                                                            if lk.get("url") and c_domain in lk.get("url"):
                                                                SiteManager.update_site_link(s_id, lk["id"], {"token": val}, log=False)
                                                                bot_log(f"[{site_name}] Auto-updated session token for link '{lk.get('title')}'", site_id=s_id)
                                                                break
                                    except Exception:
                                        pass

                                except Exception as wt_err:
                                    bot_log(f"[{site_name}] Post-OAuth redirect status: {page.url}", site_id=self.site.get("id"))

                                bot_log(f"[{site_name}] Navigating to target dashboard URL: {prepared_url}", site_id=self.site.get("id"))
                                page.goto(prepared_url, wait_until="domcontentloaded", timeout=60000)

                            except Exception as g_err:
                                bot_log(f"[{site_name}] Google SSO flow exception: {g_err}", site_id=self.site.get("id"))

                    if user and pwd and has_pass_input:
                        bot_log(f"[{site_name}] Detected login screen. Authenticating as '{user}'...", site_id=self.site.get("id"))
                        user_elem = page.locator("input[name='user'], input[id='login-view-username'], input[placeholder*='email' i], input[placeholder*='username' i], input[data-testid*='Username'], input[type='text']").first
                        if user_elem.count() > 0:
                            user_elem.fill(user)
                        time.sleep(0.5)

                        pass_elem = page.locator("input[name='password'], input[id='login-view-password'], input[placeholder*='password' i], input[data-testid*='Password'], input[type='password']").first
                        if pass_elem.count() > 0:
                            pass_elem.fill(pwd)
                        time.sleep(0.5)

                        submit_elem = page.locator("button[type='submit'], button:has-text('Log in'), button:has-text('Login'), button:has-text('Sign in')").first
                        if submit_elem.count() > 0:
                            submit_elem.click()

                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        time.sleep(3)

                        try:
                            skip_btn = page.locator("button:has-text('Skip'), a:has-text('Skip')").first
                            if skip_btn.count() > 0:
                                skip_btn.click()
                                time.sleep(2)
                        except Exception:
                            pass

                        try:
                            curr_url = page.url
                            curr_path = urlparse(curr_url).path
                            if "/login" in curr_path:
                                bot_log(f"[{site_name}] Still on login screen after password submission. Please check credentials.", site_id=self.site.get("id"))
                            elif prep_path and prep_path != "/" and prep_path not in curr_path:
                                page.goto(prepared_url, wait_until="domcontentloaded", timeout=60000)
                        except Exception:
                            pass

                final_url = page.url
                still_on_login = ("/login" in final_url or "/auth/" in final_url) and "/login/google" not in final_url and "accounts.google.com" not in final_url
                still_google_sso = False
                try:
                    google_check = page.locator(google_sso_selector)
                    if google_check.count() > 0 and google_check.first.is_visible():
                        still_google_sso = True
                except Exception:
                    pass

                if still_on_login or still_google_sso:
                    err_msg = (f"Grafana is stuck on the login screen. "
                               f"The session cookie in 'grafana_auth_state.json' is expired or Google requires interactive re-login.\n"
                               f"To refresh automatically:\n"
                               f"1. Run in terminal: python save_google_session.py {base_url}\n"
                               f"   (Opens Chrome once, log in via Google, and saves session state to grafana_auth_state.json)\n"
                               f"OR manually:\n"
                               f"2. Open {base_url} in Chrome → F12 → Application → Cookies → copy 'grafana_session' → paste in Link Tab → Credentials → Save")
                    bot_log(f"[{site_name}] [AUTH ERROR] Session expired or invalid — still on login page ({final_url})! {err_msg}", site_id=self.site.get("id"))
                    raise RuntimeError(err_msg)

                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass

                try:
                    page.wait_for_selector(".panel-loading, .loading-bar", state="hidden", timeout=8000)
                except Exception:
                    pass

                try:
                    page.wait_for_selector(".react-grid-layout, .panel-content, [data-testid*='panel'], .dashboard-container, .panel-container, [id*='panel'], table", state="visible", timeout=10000)
                except Exception:
                    pass

                time.sleep(page_wait)

                try:
                    page.add_style_tag(content="""
                        .grafana-tooltip, .portal-wrapper { display: none !important; }
                        body { overflow: hidden !important; }
                    """)
                except Exception:
                    pass

                shot_taken = False
                if "/d-solo/" in prepared_url:
                    try:
                        panel = page.locator(".panel-container, .react-grid-item, .panel-content").first
                        if panel.count() > 0 and panel.is_visible():
                            box = panel.bounding_box()
                            if box and box["width"] > 300 and box["height"] > 200:
                                panel.screenshot(path=output_path)
                                shot_taken = True
                    except Exception:
                        pass

                if not shot_taken:
                    page.screenshot(path=output_path, full_page=False)

                if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    raise RuntimeError("Screenshot capture failed: output image was not created.")

                extracted_data = extract_page_data(page)
                ocr_res = extract_image_ocr(output_path)
                extracted_data["ocr"] = ocr_res
                if ocr_res.get("pixel_colors"):
                    for pc in ocr_res["pixel_colors"]:
                        if pc not in extracted_data["colors_detected"]:
                            extracted_data["colors_detected"].append(pc)
                if ocr_res.get("success") and ocr_res.get("numbers"):
                    for num_metric in ocr_res["numbers"]:
                        if num_metric not in extracted_data["stats"]:
                            extracted_data["stats"].append(f"[Image OCR] {num_metric}")

                file_size = os.path.getsize(output_path)
                bot_log(f"[{site_name}] Captured snapshot successfully! ({file_size / 1024:.1f} KB)", site_id=self.site.get("id"))
                if return_extracted:
                    return output_path, extracted_data
                return output_path

            finally:
                try: context.close()
                except Exception: pass
                try: browser.close()
                except Exception: pass
                if temp_st_to_clean and os.path.exists(temp_st_to_clean):
                    try: os.remove(temp_st_to_clean)
                    except Exception: pass

    def extract_dashboard_data(self, target_url=None, username=None, password=None, token=None):
        temp_img = os.path.join(tempfile.gettempdir(), f"extract_temp_{int(time.time() * 1000)}.png")
        try:
            _, extraction = self.capture_screenshot(
                target_url=target_url,
                output_path=temp_img,
                username=username,
                password=password,
                token=token,
                return_extracted=True
            )
            return extraction
        finally:
            if os.path.exists(temp_img):
                try:
                    os.remove(temp_img)
                except Exception:
                    pass
