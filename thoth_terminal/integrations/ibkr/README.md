# IBKR Read-Only Adapter

This package is local-only and intentionally narrow. It supports account, portfolio,
P&L, read-style performance/transaction, and delayed market-data views through the
Interactive Brokers Client Portal Gateway.

It does not implement order placement, modification, cancellation, option exercise,
or raw broker API passthrough.

Default gateway base:

```text
https://localhost:5000/v1/api
```

Enable only in local desktop mode:

```text
THOTH_MODE=local_desktop
THOTH_ENABLE_IBKR=true
THOTH_ENABLE_TRADING=false
```

For UI work without a live gateway:

```text
THOTH_IBKR_MOCK_ENABLED=true
```
