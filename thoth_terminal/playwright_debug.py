from __future__ import annotations

from pathlib import Path


def _maybe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def open_playwright_page(config: dict, url: str, *, open_seconds: int = 600) -> None:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
        ) from exc

    storage_state = config.get("storage_state_path")
    if storage_state:
        storage_state = str(storage_state)
    user_data_dir = config.get("user_data_dir") or config.get("browser_user_data_dir")
    if user_data_dir:
        user_data_dir = str(user_data_dir)
    browser_channel = config.get("browser_channel")
    profile_directory = config.get("profile_directory") or config.get("browser_profile_directory")
    slow_mo_ms = _maybe_int(config.get("slow_mo_ms", 0), 0)
    timeout_ms = _maybe_int(config.get("timeout_ms", 120000), 120000)
    wait_ms = _maybe_int(config.get("wait_after_load_ms", 1200), 1200)

    with sync_playwright() as p:
        launch_kwargs: dict[str, object] = {"headless": False}
        if browser_channel:
            launch_kwargs["channel"] = str(browser_channel)
        if slow_mo_ms > 0:
            launch_kwargs["slow_mo"] = slow_mo_ms

        browser = None
        if user_data_dir:
            if profile_directory:
                launch_args = list(launch_kwargs.get("args") or [])
                launch_args.append(f"--profile-directory={profile_directory}")
                launch_kwargs["args"] = launch_args
            context = p.chromium.launch_persistent_context(str(user_data_dir), **launch_kwargs)
        else:
            browser = p.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, object] = {}
            if storage_state and Path(storage_state).exists():
                context_kwargs["storage_state"] = storage_state
            context = browser.new_context(**context_kwargs)

        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms)

        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        try:
            page.wait_for_event("close", timeout=open_seconds * 1000)
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
