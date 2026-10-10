from __future__ import annotations

import unittest
from dataclasses import dataclass

from thoth_terminal.security import (
    StartupSecurityError,
    assert_no_trading_routes,
    public_safe_path,
    validate_security_posture,
)


@dataclass
class _Settings:
    mode: str = "local_desktop"
    host: str = "127.0.0.1"
    require_auth: bool = True
    app_auth_token: str | None = None
    public_build: bool = False
    public_allow_unauthenticated: bool = False
    enable_ibkr: bool = False
    enable_trading: bool = False
    enable_omori_local_assets: bool = False


class SecurityModeTests(unittest.TestCase):
    def test_invalid_security_postures_are_refused(self) -> None:
        cases = (
            (_Settings(mode="public", enable_ibkr=True), {}, "IBKR"),
            (_Settings(mode="public", enable_omori_local_assets=True), {}, "Omori"),
            (_Settings(enable_trading=True), {}, "ENABLE_TRADING"),
            (_Settings(mode="local_desktop"), {"bind_host": "0.0.0.0"}, "local_desktop"),
            (_Settings(mode="public", require_auth=False), {"bind_host": "0.0.0.0"}, "without REQUIRE_AUTH"),
            (_Settings(mode="public", require_auth=False), {}, "PUBLIC_ALLOW_UNAUTHENTICATED"),
        )
        for settings, kwargs, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(StartupSecurityError, message):
                    validate_security_posture(settings, **kwargs)

    def test_public_landing_can_be_explicitly_unauthenticated(self) -> None:
        settings = _Settings(
            mode="public",
            require_auth=True,
            public_build=True,
            public_allow_unauthenticated=True,
        )
        posture = validate_security_posture(settings, bind_host="0.0.0.0")
        self.assertTrue(posture.public)

    def test_no_order_cancel_exercise_routes(self) -> None:
        class _Route:
            def __init__(self, path: str) -> None:
                self.path = path

        assert_no_trading_routes([_Route("/api/ibkr/status"), _Route("/api/portfolio/mood")])
        with self.assertRaisesRegex(StartupSecurityError, "order"):
            assert_no_trading_routes([_Route("/api/ibkr/order")])

    def test_public_safe_path_blocks_internal_routes(self) -> None:
        self.assertTrue(public_safe_path("/"))
        self.assertTrue(public_safe_path("/static/public-site.css"))
        self.assertFalse(public_safe_path("/static/app.css"))
        self.assertTrue(public_safe_path("/public"))
        self.assertTrue(public_safe_path("/public/reports/example/reader"))
        self.assertTrue(public_safe_path("/public/newsletter/subscribe", "POST"))
        self.assertFalse(public_safe_path("/public/newsletter/subscribe", "DELETE"))
        self.assertFalse(public_safe_path("/api/ibkr/status"))
        self.assertFalse(public_safe_path("/notes"))


if __name__ == "__main__":
    unittest.main()
