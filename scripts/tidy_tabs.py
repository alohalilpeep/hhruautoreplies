"""Оставить в профиле BitBrowser одну пустую вкладку вместо закрытия профиля.

Профили не закрываем: у тарифа BitBrowser суточный лимит на открытия.
Работает через служебный HTTP-интерфейс браузера, без Playwright.
"""
import time

import requests

API = "http://127.0.0.1:54345"


def tidy(pid):
    try:
        port = (requests.post(f"{API}/browser/ports", json={}, timeout=15)
                .json().get("data") or {}).get(pid)
        if not port:
            return
        cdp = f"http://127.0.0.1:{port}"
        keep = requests.put(f"{cdp}/json/new?about:blank", timeout=10).json().get("id")
        for _ in range(3):              # закрытие асинхронное — добиваем остаток
            rest = [t for t in requests.get(f"{cdp}/json/list", timeout=10).json()
                    if t.get("type") == "page" and t.get("id") != keep]
            if not rest:
                break
            for t in rest:
                requests.get(f"{cdp}/json/close/{t['id']}", timeout=10)
            time.sleep(1)
    except Exception:
        pass
