const notifyButton = document.getElementById('notifications');
if (notifyButton) notifyButton.addEventListener('click', async () => {
  if (!('Notification' in window)) { notifyButton.textContent = 'Notifications unsupported'; return; }
  const permission = await Notification.requestPermission();
  notifyButton.textContent = permission === 'granted' ? 'Alerts enabled — keep this tab open' : 'Alerts not permitted';
});
const list = document.getElementById('job-list');
if (list) {
  const pageKeys = JSON.parse(list.dataset.keys).sort();
  let initialized = false;
  let seen = new Set();
  async function refresh() {
    try {
      const response = await fetch('/api/status?' + new URLSearchParams({scope:list.dataset.scope, view:list.dataset.view, page:list.dataset.page}));
      if (!response.ok) throw new Error('Status request failed');
      const data = await response.json(), s = data.state;
      document.getElementById('available').textContent = data.total;
      document.getElementById('directory-size').textContent = s.directory_size.toLocaleString();
      document.getElementById('scan-progress').textContent = `${s.completed} / ${s.total}`;
      document.getElementById('scan-status').textContent = `${s.running ? 'Scanning' : (s.enabled ? 'Waiting for next round' : 'Scanner disabled')} · ${data.available} under 24 hours (${data.ready} US remote) · ${s.new_alerts} new alerts · ${s.successful} successful sources · ${s.skipped} skipped or not due · Directory ${s.cursor} / ${s.directory_size} · ${s.errors.length} connection failures`;
      document.getElementById('exclusions').textContent = `Fresh alert feed excludes: ${data.excluded.outside_window} outside the 24-hour window or missing dates; ${data.excluded.applied} applied; ${data.excluded.dismissed} dismissed. No dates are changed to fill the board.`;
      document.getElementById('directory-notice').textContent = s.directory_notice || (s.directory_updated ? `Directory checked ${new Date(s.directory_updated*1000).toLocaleString()}. Jobicy is checked hourly.` : 'Directory refresh pending.');
      document.getElementById('unavailable-summary').textContent = `Unavailable endpoints — ${s.unavailable.length} temporarily skipped`;
      const unavailable = document.getElementById('unavailable'); unavailable.replaceChildren();
      s.unavailable.forEach(board => {const item = document.createElement('li'); item.textContent = `${board.name}: unavailable; retry ${new Date(board.retry_at*1000).toLocaleDateString()}${board.replacement ? ' — alternate: '+board.replacement : ''}`; unavailable.appendChild(item);});
      const errors = document.getElementById('errors'); errors.replaceChildren();
      s.errors.forEach(error => { const item = document.createElement('li'); item.textContent = error; errors.appendChild(item); });
      const added = data.jobs.filter(j => !seen.has(j.key) && j.status === 'New');
      if (initialized && added.length && 'Notification' in window && Notification.permission === 'granted')
        new Notification(`${added.length} new job matches`, {body: added.slice(0,3).map(j => `${j.title} — ${j.company}`).join('\n')});
      seen = new Set(data.jobs.map(j => j.key)); initialized = true;
      const visible = data.visible_jobs;
      const cards = [...list.querySelectorAll('.job')];
      visible.forEach(j => { const card = cards.find(c => c.dataset.key === j.key); if (card) card.querySelector('.badge').textContent = j.age; });
      if (data.page !== Number(list.dataset.page) || data.pages !== Number(list.dataset.pages) || data.total !== Number(list.dataset.total) || JSON.stringify(visible.map(j=>j.key).sort()) !== JSON.stringify(pageKeys)) location.reload();
    } catch (error) { document.getElementById('scan-status').textContent = 'Connection lost. Retrying…'; }
  }
  refresh(); setInterval(refresh, 10000);
}
