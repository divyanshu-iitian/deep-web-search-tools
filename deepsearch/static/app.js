const $ = id => document.getElementById(id);
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
};

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function showReport(report) {
  const box = $('report');
  box.replaceChildren();
  $('message').hidden = true;
  $('result-count').textContent = `${report.evidence.length} source${report.evidence.length === 1 ? '' : 's'}`;
  const summary = element('div', 'summary');
  const top = element('div', 'summary-top');
  top.append(element('p', 'eyebrow', 'IDENTITY ASSESSMENT'));
  top.append(element('span', `badge ${report.status}`, report.status.replaceAll('_', ' ')));
  summary.append(top, element('h3', '', `${report.name} · ${report.company || report.company_domain || 'Unspecified company'}`));
  summary.append(element('p', '', report.explanation));
  report.warnings.forEach(warning => summary.append(element('p', 'warning', warning)));
  box.append(summary);
  report.evidence.forEach(item => {
    const card = element('article', 'panel evidence');
    const head = element('div', 'evidence-head');
    const title = element('div');
    title.append(element('h3', '', item.title || item.host));
    title.append(element('small', '', `${item.category.replaceAll('_', ' ')} · ${item.host}`));
    head.append(title, element('span', 'score', `${item.score}/100`));
    card.append(head, element('p', '', item.excerpt || 'No excerpt available.'));
    const link = element('a', '', item.url);
    link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    card.append(link);
    const signals = element('div', 'signals');
    item.signals.forEach(signal => signals.append(element('span', 'signal', signal)));
    card.append(signals);
    box.append(card);
  });
}

async function loadHistory() {
  const runs = await request('/api/runs');
  const list = $('history-list');
  list.replaceChildren();
  if (!runs.length) list.append(element('p', 'fine', 'No research runs yet.'));
  runs.forEach(run => {
    const button = element('button', 'history-item', run.name);
    button.type = 'button';
    button.append(element('span', '', `${run.company || run.company_domain || 'No company'} · ${run.status.replaceAll('_', ' ')}`));
    button.addEventListener('click', () => showReport(run));
    list.append(button);
  });
}

async function runSearch(event, demo = false) {
  if (event) event.preventDefault();
  const payload = {
    name: $('name').value.trim(), company: $('company').value.trim() || null,
    company_domain: $('domain').value.trim() || null,
    work_email: $('email').value.trim() || null, demo
  };
  const submit = $('submit');
  submit.disabled = true;
  submit.textContent = 'Searching sources…';
  const message = $('message');
  message.hidden = false; message.className = 'notice';
  message.textContent = 'Searching public results, then checking company pages and public profiles…';
  try {
    showReport(await request('/api/search', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }));
    await loadHistory();
  } catch (error) {
    message.hidden = false; message.className = 'notice error';
    message.textContent = error.message;
  } finally {
    submit.disabled = false; submit.textContent = 'Search public sources ↗';
  }
}

$('search-form').addEventListener('submit', event => runSearch(event));
$('demo').addEventListener('click', () => {
  $('name').value = 'Maya Chen';
  $('company').value = 'Cedar Utilities';
  $('domain').value = 'cedar.example.org';
  $('email').value = 'maya@cedar.example.org';
  void runSearch(null, true);
});
request('/api/health').then(data => {
  $('provider-status').textContent = data.configured ?
    `${data.provider.toUpperCase()} CONNECTED` : 'LIVE SEARCH NEEDS A KEY';
}).catch(() => {$('provider-status').textContent = 'PROVIDER OFFLINE';});
loadHistory().catch(() => {});
