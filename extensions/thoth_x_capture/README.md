# Thoth X Capture (Edge/Chrome extension)

## Install (Edge)
1. Open `edge://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select this folder: `extensions/thoth_x_capture`.

## Configure
Open the extension options and set:
- **Thoth base URL** (default `http://127.0.0.1:8750`)
- **X list URL** (default is the seeded analysts list)
- **Enable/disable** capture
- **Poll min/max minutes** and **active window** (start/end + timezone)
- **Include thread** and **max thread replies**
- **Seen-streak stop** (stop after N consecutive already-seen tweets)
- **Initial backfill count** (first run only)
- **Batch size** for streaming updates
- **Tweet dwell timing** (base/per-char/max) to let content load

## Behavior
- Scans the list from newest to older.
- Stops after N consecutive already-seen tweets (seen streak).
- First run can backfill a limited count or just mark the top.
- Optional self-thread capture (same author only).

## Use
- The extension runs on a schedule (alarms) within the active window.
- Use **Run now** in options or the toolbar action to capture immediately.

## Notes
- Posts to `/api/capture/tweets` on your Thoth instance.
- Uses your logged-in browser session (no automated login).
- If you prefer Playwright-based ingestion instead, use the `x_list` connector and `thoth x-login`.
