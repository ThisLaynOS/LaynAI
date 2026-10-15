const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat('pt-BR');
const state = { token: '', source: 'bot', busy: false, logs: '' };
const paths = { bot: '/var/log/laynai/bot.log', errors: '/var/log/laynai/bot.err', updates: '/var/log/laynai/update.log' };

async function json(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Erro ${response.status}`);
  return data;
}

function notice(message, error = false) {
  const element = $('notice');
  element.textContent = message;
  element.classList.toggle('error', error);
  element.hidden = false;
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => { element.hidden = true; }, 7000);
}

function updateControls(running) {
  $('start-btn').disabled = state.busy || !state.token || running;
  $('stop-btn').disabled = state.busy || !state.token || !running;
  $('restart-btn').disabled = state.busy || !state.token || !running;
}

async function loadStatus() {
  try {
    const data = await json('/api/status');
    const online = data.running && data.ready.ok;
    $('status-text').textContent = online ? 'Online e saudável' : data.running ? 'Iniciando ou com falha' : 'Desligado';
    $('status-led').className = `status-led ${online ? 'online' : 'offline'}`;
    $('status-detail').textContent = data.paused ? 'Atualizações automáticas pausadas até você ligar o bot.' : data.running ? 'Serviço supervisionado pelo OpenRC.' : 'O serviço não está em execução.';
    $('gateway-status').textContent = data.ready.gateway ? 'Conectado' : 'Desconectado';
    $('database-status').textContent = data.ready.database ? 'Saudável' : 'Indisponível';
    $('gateway-status').className = data.ready.gateway ? 'good' : 'bad';
    $('database-status').className = data.ready.database ? 'good' : 'bad';
    $('revision').textContent = data.revision || '—';
    $('requests').textContent = number.format(data.usage.requests);
    $('tokens').textContent = number.format(data.usage.tokens);
    $('calls').textContent = number.format(data.usage.calls);
    updateControls(data.running);
  } catch (error) {
    $('status-text').textContent = 'Painel sem dados';
    $('status-led').className = 'status-led offline';
    $('status-detail').textContent = error.message;
  }
}

function size(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
}

async function loadBackups() {
  try {
    const { items } = await json('/api/backups');
    $('backup-count').textContent = number.format(items.length);
    const list = $('backup-list');
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'Nenhum backup encontrado.'; list.append(empty); return;
    }
    for (const item of items) {
      const row = document.createElement('div'); row.className = 'backup-item';
      const icon = document.createElement('div'); icon.className = 'backup-icon'; icon.textContent = item.kind === 'Ponto completo' ? '◈' : '▣';
      const info = document.createElement('div'); info.className = 'backup-info';
      const title = document.createElement('strong'); title.textContent = item.kind;
      const detail = document.createElement('small'); detail.textContent = `${new Date(item.created).toLocaleString('pt-BR')} · ${item.revision}`;
      const bytes = document.createElement('span'); bytes.className = 'backup-size'; bytes.textContent = size(item.size);
      info.append(title, detail); row.append(icon, info, bytes); row.title = item.name; list.append(row);
    }
  } catch (error) { $('backup-list').textContent = `Não foi possível carregar os backups: ${error.message}`; }
}

async function loadLogs(force = false) {
  try {
    const { text } = await json(`/api/logs?source=${state.source}`);
    if (!force && text === state.logs) return;
    const output = $('log-output');
    const nearBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 30;
    output.textContent = text || 'Nenhuma linha de log ainda.';
    if (nearBottom || force) output.scrollTop = output.scrollHeight;
    state.logs = text;
  } catch (error) { $('log-output').textContent = `Falha ao carregar log: ${error.message}`; }
}

async function action(name) {
  const labels = { start: 'Ligar', stop: 'Desligar', restart: 'Reiniciar' };
  state.busy = true;
  document.querySelectorAll('[data-action]').forEach((button) => { button.disabled = true; });
  notice(`${labels[name]} o bot…`);
  try {
    await json('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.token }, body: JSON.stringify({ action: name }) });
    notice(`Bot ${name === 'stop' ? 'desligado' : name === 'start' ? 'ligado' : 'reiniciado'} com sucesso.`);
  } catch (error) { notice(error.message, true); }
  finally { state.busy = false; await Promise.all([loadStatus(), loadLogs(true)]); }
}

function confirmAction(name) {
  const dialog = $('confirm-dialog');
  $('confirm-title').textContent = name === 'stop' ? 'Desligar a LaynAI?' : 'Reiniciar a LaynAI?';
  $('confirm-copy').textContent = name === 'stop' ? 'Ela ficará offline, não iniciará no próximo boot e os deploys ficarão pausados até você ligar novamente.' : 'O bot pode ficar indisponível por alguns segundos.';
  dialog.showModal();
  dialog.addEventListener('close', () => { if (dialog.returnValue === 'confirm') action(name); }, { once: true });
}

document.querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => {
  const name = button.dataset.action;
  if (name === 'start') action(name); else confirmAction(name);
}));
document.querySelectorAll('[data-source]').forEach((button) => button.addEventListener('click', () => {
  state.source = button.dataset.source;
  state.logs = '';
  document.querySelectorAll('[data-source]').forEach((tab) => { tab.classList.toggle('active', tab === button); tab.setAttribute('aria-selected', tab === button ? 'true' : 'false'); });
  $('log-path').textContent = paths[state.source];
  loadLogs(true);
}));
$('refresh-logs').addEventListener('click', () => loadLogs(true));
function clock() { $('clock').textContent = new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' }); }
clock(); setInterval(clock, 30000);

(async () => {
  try { state.token = (await json('/api/csrf')).token; }
  catch (error) { notice(`Controles indisponíveis: ${error.message}`, true); }
  await Promise.all([loadStatus(), loadBackups(), loadLogs(true)]);
  setInterval(loadStatus, 5000);
  setInterval(loadLogs, 3000);
  setInterval(loadBackups, 30000);
})();
