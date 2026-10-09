import os
import sys
import json
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

AUTH_FILE = "grafana_auth_state.json"
SITES_FILE = "sites.json"

# Target URLs from sys.argv, interactive paste, or configured sites.json
target_urls = []
cli_urls = [u.strip() for u in sys.argv[1:] if u.strip().startswith("http")]
if cli_urls:
    target_urls = cli_urls
else:
    print("=" * 65)
    print("👉 Paste a new Grafana link URL below, or press ENTER to use existing sites:")
    try:
        user_url = input("👉 URL (press Enter to skip): ").strip()
        if user_url.startswith("http"):
            target_urls = [user_url]
    except Exception:
        pass

if not target_urls and os.path.exists(SITES_FILE):
    try:
        with open(SITES_FILE, "r", encoding="utf-8") as sf:
            s_data = json.load(sf)
        seen_hosts = set()
        for s in s_data.get("sites", []):
            for u in [s.get("grafana_url")] + [lk.get("url") for lk in s.get("links", [])]:
                if u and str(u).startswith("http"):
                    h = (urlparse(u).hostname or "").lower()
                    if h and h not in seen_hosts:
                        seen_hosts.add(h)
                        target_urls.append(u)
    except Exception:
        pass

if not target_urls:
    target_urls = [
        "https://cloudwatch.greymatter.greyorange.com",
        "https://gem-dash.greyorange.org"
    ]

print("=" * 65)
print(f"[INFO] Launching real Chrome for session capture:")
for u in target_urls:
    print(f"   👉 {u}")
print("=" * 65)

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"]
        )
    except Exception:
        print("[WARN] Chrome channel not found, launching bundled Chromium...")
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )

    # Load existing state if available so previously saved logins aren't lost
    context_kwargs = {}
    if os.path.exists(AUTH_FILE):
        context_kwargs["storage_state"] = AUTH_FILE

    context = browser.new_context(**context_kwargs)

    # Open first target page
    main_page = context.new_page()
    try:
        print(f"\n[INFO] Opening tab: {target_urls[0]} ...")
        main_page.goto(target_urls[0])
    except Exception as e:
        print(f"[WARN] Error opening {target_urls[0]}: {e}")

    # If multiple URLs detected (e.g. cloudwatch and gem-dash), open each in a separate tab
    for add_url in target_urls[1:]:
        try:
            print(f"[INFO] Opening additional tab: {add_url} ...")
            t_page = context.new_page()
            t_page.goto(add_url)
        except Exception as e:
            print(f"[WARN] Error opening {add_url}: {e}")

    print("\n" + "=" * 65)
    print("👉 A Chrome browser window has opened with your Grafana tabs.")
    print("👉 Log into Grafana via Google (enter password/2FA if prompted).")
    print("👉 Verify your dashboard(s) load properly in Chrome.")
    print("=" * 65 + "\n")

    input("👉 Once your Grafana dashboard is visible, press ENTER here in terminal... ")

    # Save complete storage state (all Google SSO & Grafana cookies) to JSON
    context.storage_state(path=AUTH_FILE)
    print(f"\n[SUCCESS] Authentication state saved to: {os.path.abspath(AUTH_FILE)}")

    # Auto-sync captured sessions for ALL hosts directly into sites.json
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as af:
            st_data = json.load(af)

        tokens_by_host = {}
        for ck in st_data.get("cookies", []):
            if ck.get("name") == "grafana_session":
                val = str(ck.get("value", "")).strip()
                dom = str(ck.get("domain", "")).lower().lstrip(".")
                if val and len(val) >= 16:
                    tokens_by_host[dom] = val

        if tokens_by_host and os.path.exists(SITES_FILE):
            with open(SITES_FILE, "r", encoding="utf-8") as sf:
                sites_data = json.load(sf)

            updated = False
            far_future_ts = int(time.time()) + 31536000

            for s in sites_data.get("sites", []):
                s_host = (urlparse(s.get("grafana_url") or "").hostname or "").lower()
                for dom, tok in tokens_by_host.items():
                    if s_host and (dom in s_host or s_host in dom):
                        s["grafana_token"] = tok
                        updated = True

                for lk in s.get("links", []):
                    lk_host = (urlparse(lk.get("url") or "").hostname or "").lower() or s_host
                    for dom, tok in tokens_by_host.items():
                        if lk_host and (dom in lk_host or lk_host in dom):
                            formatted_tok = f"grafana_session={tok}; grafana_session_expiry={far_future_ts}"
                            lk["token"] = formatted_tok
                            updated = True
                            print(f"[SUCCESS] Auto-synchronized session for '{lk.get('title')}' ({dom})")

            if updated:
                with open(SITES_FILE, "w", encoding="utf-8") as sf:
                    json.dump(sites_data, sf, indent=2)
                print("[SUCCESS] All matching links in sites.json updated with active session tokens!")
    except Exception as e:
        print(f"[WARN] Notice syncing to sites.json: {e}")

    browser.close()
    print("[SUCCESS] Complete session capture finished! OpsBot will now run seamlessly.")

