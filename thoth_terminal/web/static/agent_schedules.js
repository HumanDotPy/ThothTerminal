(() => {
  const root = document.querySelector('[data-agent-schedules]');
  if (!root) return;
  const form = root.querySelector('#agent-schedule-form');
  const feedback = root.querySelector('#agent-schedule-feedback');
  const save = root.querySelector('#agent-schedule-save');

  function message(element, text, state = '') {
    element.textContent = text;
    element.dataset.state = state;
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || data.detail || `Request failed (${response.status}).`);
    return data;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const input = (name) => form.elements.namedItem(name);
    const body = { enabled: input('enabled').checked };
    for (const mode of ['nightly', 'heartbeat', 'inquisitive']) {
      const field = input(`${mode}_local_times`);
      field.setCustomValidity('');
      const localTimes = field.value.trim();
      const valid = !localTimes || localTimes.split(',').every((part) => /^([01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(part.trim()));
      if (!valid) {
        field.setCustomValidity('Use comma-separated times such as 08:00,20:00.');
        field.reportValidity();
        return;
      }
      body[mode] = { enabled: input(`${mode}_enabled`).checked, local_times: localTimes,
        lookback_hours: Number(input(`${mode}_lookback_hours`).value) };
    }
    body.heartbeat.interval_hours = Number(input('heartbeat_interval_hours').value);
    save.disabled = true;
    message(feedback, 'Saving schedules…');
    try {
      const data = await post('/api/agents/cloud/settings', body);
      for (const mode of ['nightly', 'heartbeat', 'inquisitive']) {
        const current = data.settings[mode];
        input(`${mode}_enabled`).checked = current.enabled;
        input(`${mode}_local_times`).value = current.local_times;
        input(`${mode}_lookback_hours`).value = current.lookback_hours;
      }
      input('enabled').checked = data.settings.enabled;
      input('heartbeat_interval_hours').value = data.settings.heartbeat.interval_hours;
      message(feedback, 'Schedules saved. Queued work is unchanged.', 'success');
    } catch (error) {
      message(feedback, error.message, 'error');
    } finally { save.disabled = false; }
  });
  form.addEventListener('input', (event) => event.target.setCustomValidity?.(''));

})();
