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
      const response = await fetch('/api/status');
      if (!response.ok) throw new Error('Status request failed');
      const data = await response.json(), s = data.state;
      document.getElementById('available').textContent = data.available;
      document.getElementById('scan-progress').textContent = `${s.completed} / ${s.total}`;
      document.getElementById('scan-status').textContent = `${s.running ? 'Scanning' : (s.enabled ? 'Waiting for next round' : 'Scanner disabled')} · ${data.available} available (${data.ready} US remote) · ${s.new_alerts} new alerts this round · Directory cursor ${s.cursor} / ${s.directory_size} · ${s.errors.length} source errors`;
      const errors = document.getElementById('errors'); errors.replaceChildren();
      s.errors.forEach(error => { const item = document.createElement('li'); item.textContent = error; errors.appendChild(item); });
      const added = data.jobs.filter(j => !seen.has(j.key) && j.status === 'New');
      if (initialized && added.length && 'Notification' in window && Notification.permission === 'granted')
        new Notification(`${added.length} new job matches`, {body: added.slice(0,3).map(j => `${j.title} — ${j.company}`).join('\n')});
      seen = new Set(data.jobs.map(j => j.key)); initialized = true;
      const visible = data.jobs.filter(j => list.dataset.view === 'ready' ? j.eligibility === 'Ready' :
        list.dataset.view === 'review' ? j.eligibility === 'Review' : true);
      const cards = [...list.querySelectorAll('.job')];
      visible.forEach(j => { const card = cards.find(c => c.dataset.key === j.key); if (card) card.querySelector('.badge').textContent = j.age; });
      if (JSON.stringify(visible.map(j=>j.key).sort()) !== JSON.stringify(pageKeys)) location.reload();
    } catch (error) { document.getElementById('scan-status').textContent = 'Connection lost. Retrying…'; }
  }
  refresh(); setInterval(refresh, 10000);
}
