(() => {
  const frame = document.getElementById('harness-frame');
  if (!frame) return;
  const status = document.getElementById('harness-status');
  const start = document.getElementById('harness-start');
  const reload = document.getElementById('harness-reload');
  const controller = new AbortController();
  let busy = false;
  async function connect(launch = false) {
    if (busy) return;
    busy = true;
    start.disabled = reload.disabled = true;
    status.textContent = launch ? 'Starting Harness…' : 'Connecting to Harness…';
    try {
      const response = await fetch('/api/agents/harness/' + (launch ? 'start' : 'status'), {
        method: launch ? 'POST' : 'GET', signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Harness connection failed.');
      start.hidden = Boolean(data.available);
      if (!data.available) {
        frame.removeAttribute('src');
        frame.hidden = true;
        status.textContent = data.message || 'Harness is stopped.';
        return;
      }
      frame.hidden = false;
      frame.src = '/agents/harness/native/';
      status.textContent = 'Loading Harness workspace…';
    } catch (error) {
      if (error.name !== 'AbortError') status.textContent = error.message;
    } finally { busy = false; start.disabled = reload.disabled = false; }
  }
  frame.addEventListener('load', () => {
    const body = frame.contentDocument?.body;
    const error = body?.innerText;
    status.textContent = !body || !body.querySelector('#root') ? (error || 'Harness could not load. Reconnect to retry.') : 'DeepSeek Harness';
  });
  start.addEventListener('click', () => connect(true));
  reload.addEventListener('click', () => connect());
  window.addEventListener('pagehide', () => controller.abort(), {once: true});
  connect();
})();
