/* Keep connection checks inside the Terminal instead of navigating to JSON. */
(() => {
  for (const button of document.querySelectorAll('[data-ibkr-status]')) {
    const output = document.getElementById(button.dataset.ibkrStatus);
    if (!output) continue;
    let pending = null;
    window.addEventListener('pagehide', () => pending?.abort());
    button.addEventListener('click', async () => {
      if (pending) return;
      pending = new AbortController();
      const timeout = setTimeout(() => pending?.abort(), 8000);
      button.disabled = true;
      output.hidden = false;
      output.textContent = 'Checking IBKR connection...';
      try {
        const response = await fetch('/api/ibkr/status', {
          cache: 'no-store', signal: pending.signal,
        });
        if (!response.ok) throw new Error('Status unavailable');
        const status = await response.json();
        output.textContent = !status.enabled ? 'IBKR integration is disabled.'
          : status.connected ? 'IBKR is connected.'
          : status.competing ? 'Another IBKR brokerage session is active. Sign in here to connect Thoth.'
          : status.auth_required ? 'IBKR is not signed in. Use Sign in to connect.'
          : 'IBKR could not be reached. Try checking again.';
      } catch (_) {
        output.textContent = 'IBKR status could not be checked. Try again.';
      } finally {
        clearTimeout(timeout);
        pending = null;
        button.disabled = false;
      }
    });
  }
})();
