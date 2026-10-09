"""
Grafana Authentication & Credential Resolution for GreyOrange OpsBot.
Intelligently resolves credentials (username, password, session token / API token)
for sites and links, respecting domain boundaries and persistent Chrome SSO states.
"""

import os
import json
from urllib.parse import urlparse
from core.config import BASE_DIR


def resolve_link_auth(site, link=None, target_url=None):
    """
    Intelligently resolves (username, password, token) for a link/site execution.
    - Domain-aware: Never borrows tokens/sessions across different hostnames/domains.
    - If link has a valid token/session-cookie for its domain, uses it.
    - If link has no token, falls back to site.grafana_token ONLY IF site URL has the exact same hostname.
    - Filters out URLs accidentally inserted into the username field by browser autofill.
    """
    site = site or {}
    link = link or {}

    effective_url = target_url or link.get("url") or site.get("grafana_url") or ""

    def _get_host(u):
        try:
            return (urlparse(str(u or "")).hostname or "").lower()
        except Exception:
            return ""

    target_host = _get_host(effective_url)
    site_host = _get_host(site.get("grafana_url"))

    def _is_valid_token(t):
        if not t or not isinstance(t, str):
            return False
        t_clean = t.strip()
        if len(t_clean) < 16:
            return False
        if t_clean.startswith("\u2022") or t_clean.startswith("•") or "•" in t_clean:
            return False
        if t_clean.isdigit() and len(t_clean) <= 12:
            return False
        return True

    # 1. Resolve token:
    token = ""
    if "token" in link:
        link_token = link.get("token")
        if _is_valid_token(link_token):
            token = str(link_token).strip()
    else:
        if target_host and site_host and target_host == site_host:
            site_token = site.get("grafana_token")
            if _is_valid_token(site_token):
                token = str(site_token).strip()

    # Intelligent fallback: If token is still empty, look up grafana_auth_state.json for matching host
    if not token and target_host:
        try:
            auth_state_path = os.path.join(BASE_DIR, "grafana_auth_state.json")
            if os.path.isfile(auth_state_path):
                with open(auth_state_path, "r", encoding="utf-8") as af:
                    as_data = json.load(af)
                for ck in as_data.get("cookies", []):
                    c_dom = str(ck.get("domain", "")).lower().lstrip(".")
                    if ck.get("name") == "grafana_session" and (target_host in c_dom or c_dom in target_host):
                        val = str(ck.get("value", "")).strip()
                        if _is_valid_token(val):
                            token = val
                            break
        except Exception:
            pass

    # 2. Resolve username and password
    raw_user = str(link.get("username") or "").strip()
    if raw_user.startswith("http://") or raw_user.startswith("https://"):
        raw_user = ""
    raw_pass = str(link.get("password") or "").strip()

    if raw_user or raw_pass:
        username = raw_user
        password = raw_pass
    elif not token and (not target_host or not site_host or target_host == site_host):
        site_user = str(site.get("grafana_username") or "").strip()
        if site_user.startswith("http://") or site_user.startswith("https://"):
            site_user = ""
        username = site_user
        password = str(site.get("grafana_password") or "").strip()
    else:
        username = ""
        password = ""

    return username, password, token
