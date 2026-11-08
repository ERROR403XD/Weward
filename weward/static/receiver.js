const element = id => document.getElementById(id);
const state = { tab: 'messages', jobView: 'active', rooms: [], selected: null, before: null, trail: [], oldest: null, filters: {}, jobs: [], historyBefore: null, historyOldest: null, editing: null, monitoring: null, requests: {}, messageSignature: '', resetScroll: true };
const roomKey = room => JSON.stringify([room.account_id, room.room_id]);
const time = value => value ? new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value).toLocaleString('zh-CN', { hour12: false }) : '—';
function node(tag, text, className) { const result = document.createElement(tag); if (text !== undefined) result.textContent = text; if (className) result.className = className; return result; }
function errorAt(id, error) { element(id).textContent = error ? error.message : ''; element(id).classList.toggle('hidden', !error); }
function toast(text) { element('toast').textContent = text; element('toast').classList.remove('hidden'); clearTimeout(toast.timer); toast.timer = setTimeout(() => element('toast').classList.add('hidden'), 3000); }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const result = await response.json(); if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`); return result;
}
function ticket(name) { state.requests[name] = (state.requests[name] || 0) + 1; return state.requests[name]; }
function action(label, callback, className = 'quiet', errorId = 'jobs-error') {
  const button = node('button', label, className); button.type = 'button';
  button.onclick = async () => { button.disabled = true; try { await callback(); errorAt(errorId, null); } catch (error) { errorAt(errorId, error); } finally { button.disabled = false; } };
  return button;
}
function status(text, kind = '') { return node('span', text, 'status ' + kind); }
function cell(content, className) { const result = node('td', undefined, className); result.append(typeof content === 'string' || typeof content === 'number' ? document.createTextNode(content) : content); return result; }
function link(url, title) {
  try { const parsed = new URL(url); if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error(); const anchor = node('a', title || url); anchor.href = parsed.href; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; return anchor; }
  catch { return node('span', title || '链接地址缺失', 'subtle'); }
}
function richText(value) {
  const content = node('span');
  for (const part of String(value || '').split(/(https?:\/\/[^\s<>]+)/g)) content.append(/^https?:\/\//.test(part) ? link(part) : document.createTextNode(part));
  return content;
}
function messageText(message) {
  const raw = message.raw || {};
  if (message.type === 'text') return raw.text?.content || '';
  if (message.type === 'reply') return raw.reply?.title || '[引用]';
  if (message.type === 'sphfeed') return '[视频号] ' + (raw.sphfeed?.feed_desc || raw.sphfeed?.sph_name || '');
  if (message.type === 'emotion') return '[表情]';
  if (message.type === 'image') return '[图片]';
  if (message.type === 'file') return message.file?.filename || raw.file?.filename || '[文件]';
  if (message.type === 'link') return raw.link?.title || raw.link?.description || raw.link?.link_url || '[链接]';
  return raw.text?.content || raw.content || `[${message.type}]`;
}
function openDetail(title) { element('detail-title').textContent = title; element('detail-content').replaceChildren(); if (!element('detail-dialog').open) element('detail-dialog').showModal(); return element('detail-content'); }
function metadata(entries) { const list = node('dl', undefined, 'detail-meta'); for (const [label, value] of entries) list.append(node('dt', label), node('dd', String(value ?? '—'))); return list; }
function fileContent(message) {
  const attachment = message.file, content = node('div', undefined, 'file-content');
  if (attachment?.status === 'available') {
    const download = node('a', '下载 · ' + messageText(message));
    download.href = '/files/' + message.event_id; download.download = attachment.filename; content.append(download);
    const size = attachment.size < 1024 ? attachment.size + ' B' : attachment.size < 1048576 ? (attachment.size / 1024).toFixed(1) + ' KiB' : (attachment.size / 1048576).toFixed(2) + ' MiB';
    content.append(node('small', size, 'subtle'));
  } else {
    content.append(node('span', messageText(message)), node('small', attachment?.error || '旧消息仅保存了文件信息，未收到文件字节', 'subtle'));
  }
  return content;
}
function showMessage(message) {
  const content = openDetail('消息详情');
  if (message.image?.status === 'available') { const image = node('img', undefined, 'detail-image'); image.src = '/media/' + message.event_id; image.alt = '聊天图片'; content.append(image); }
  if (message.type === 'file') content.append(fileContent(message));
  content.append(node('div', String(messageText(message)), 'detail-text'), metadata([['发送者', message.sender_name || message.sender_id], ['消息时间', time(message.message_time_ms)], ['收到时间', time(message.received_at)], ['群聊', message.room_name || message.room_id], ['投递', message.deliveries.map(delivery => `${delivery.name} · ${delivery.status === 'sent' ? '已发送' : '待回执'}`).join('；') || '未投递']]));
  const note = node('textarea', undefined, 'detail-note'); note.value = message.note; note.placeholder = '备注'; note.setAttribute('aria-label', '消息备注');
  const controls = node('div', undefined, 'dialog-footer');
  const detailError = node('p', undefined, 'inline-error hidden'); detailError.id = 'message-detail-error';
  controls.append(action(message.archived ? '恢复展示' : '收起', async () => { await api(`/api/messages/${message.event_id}/manage`, { archived: !message.archived }); element('detail-dialog').close(); toast(message.archived ? '已恢复' : '已收起'); await loadMessages(); }, 'quiet', 'message-detail-error'),
    action('保存备注', async () => { await api(`/api/messages/${message.event_id}/manage`, { note: note.value }); element('detail-dialog').close(); toast('已保存'); await loadMessages(); }, 'primary', 'message-detail-error'));
  const raw = node('details'); raw.append(node('summary', '原始数据'), node('pre', JSON.stringify(message, null, 2))); content.append(note, detailError, controls, raw);
}
function renderMessage(message) {
  const row = node('article', undefined, 'message-row'); row.dataset.eventId = message.event_id; row.dataset.signature = JSON.stringify(message);
  const clock = node('time', new Date(message.message_time_ms).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }), 'message-time'); clock.title = time(message.message_time_ms);
  const sender = node('span', message.is_self ? '我' : (message.sender_name || message.sender_id), 'message-sender'); sender.title = message.sender_name || message.sender_id;
  const body = node('div', undefined, 'message-body');
  if (message.type === 'image') {
    if (message.image?.status === 'available') { const image = node('img'); image.src = '/media/' + message.event_id; image.alt = '查看图片'; image.loading = 'lazy'; image.tabIndex = 0; image.onclick = () => showMessage(message); image.onkeydown = event => { if (event.key === 'Enter') showMessage(message); }; body.append(image); }
    else body.append(node('span', '[图片不可用]', 'subtle'));
  } else if (message.type === 'file') { body.append(fileContent(message)); }
  else if (message.type === 'link') { body.append(link(message.raw?.link?.link_url, messageText(message))); }
  else { const text = richText(messageText(message)); body.append(text); if (String(messageText(message)).length > 200) { text.className = 'clamped'; const expand = action('展开', () => { text.classList.toggle('clamped'); expand.textContent = text.classList.contains('clamped') ? '展开' : '收起'; }, 'quiet expand', 'messages-error'); body.append(expand); } }
  if (message.note) { const marker = node('span', '·', 'message-note'); marker.title = message.note; sender.append(marker); }
  const more = action('⋯', () => showMessage(message), 'message-more', 'messages-error'); more.setAttribute('aria-label', '消息详情');
  row.append(clock, sender, body, more); return row;
}
function renderRooms() {
  const query = element('room-search').value.trim().toLowerCase(); element('room-count').textContent = state.rooms.length;
  const names = new Map(); for (const room of state.rooms) names.set(room.room_name, (names.get(room.room_name) || 0) + 1);
  const rooms = state.rooms.filter(room => (room.room_name || room.room_id).toLowerCase().includes(query));
  element('room-list').replaceChildren(...rooms.map(room => {
    const selected = roomKey(room) === state.selected;
    const label = (room.room_name || room.room_id) + (names.get(room.room_name) > 1 ? ' · ' + room.room_id.split('@')[0].slice(-5) : '');
    const button = action('', () => selectRoom(roomKey(room)), 'room-button' + (selected ? ' active' : ''), 'messages-error'); button.dataset.roomKey = roomKey(room); button.setAttribute('aria-pressed', String(selected)); button.title = `${room.room_name || room.room_id}\n${room.account_id} / ${room.room_id}`;
    button.append(node('span', (room.room_name || '群').slice(0, 1), 'room-avatar'), node('span', label, 'room-label'), node('span', room.message_count, 'room-count')); return button;
  }));
}
function resetMessages() { state.before = null; state.trail = []; state.resetScroll = true; state.messageSignature = ''; }
async function selectRoom(key) { state.selected = key; try { localStorage.setItem('weward-room', key); } catch {} resetMessages(); renderRooms(); await loadMessages(); }
async function loadRooms() {
  const request = ticket('rooms'); const result = await api('/api/rooms'); if (state.requests.rooms !== request) return;
  state.rooms = result.rooms;
  if (!state.rooms.some(room => roomKey(room) === state.selected)) {
    let previous; try { previous = localStorage.getItem('weward-room'); } catch {}
    state.selected = state.rooms.some(room => roomKey(room) === previous) ? previous : state.rooms[0] ? roomKey(state.rooms[0]) : null; resetMessages();
  }
  renderRooms(); element('connection').textContent = '已连接'; element('connection').classList.remove('offline');
}
async function loadMessages() {
  const request = ticket('messages');
  if (!state.selected) { element('message-list').replaceChildren(node('div', '暂无群聊消息', 'empty')); element('older').disabled = true; element('newer').disabled = true; return; }
  const room = state.rooms.find(room => roomKey(room) === state.selected); if (!room) return;
  const query = new URLSearchParams({ account_id: room.account_id, room_id: room.room_id, limit: '50', archived: '0', ...state.filters }); if (state.before) query.set('before', state.before);
  const selection = state.selected;
  try {
    const result = await api('/api/messages?' + query); if (state.requests.messages !== request || state.selected !== selection) return;
    errorAt('messages-error', null); element('room-title').textContent = room.room_name || room.room_id; element('room-summary').textContent = `${result.total} 条`;
    state.oldest = result.messages.at(-1)?.sequence; element('older').disabled = result.messages.length < 50; element('newer').disabled = state.trail.length === 0;
    const signature = JSON.stringify([selection, query.toString(), result]); if (signature === state.messageSignature) return;
    const list = element('message-list'); const atTop = list.scrollTop < 60;
    const previous = new Map([...list.querySelectorAll('article')].map(row => [row.dataset.eventId, row]));
    const anchor = [...previous.values()].find(row => row.getBoundingClientRect().bottom > list.getBoundingClientRect().top);
    const offset = anchor ? anchor.getBoundingClientRect().top : 0; const oldTop = list.scrollTop;
    const children = []; let lastDay = '';
    for (const message of result.messages) {
      const day = new Date(message.message_time_ms).toLocaleDateString('zh-CN'); if (day !== lastDay) { children.push(node('div', day, 'message-day')); lastDay = day; }
      const old = previous.get(message.event_id); children.push(old?.dataset.signature === JSON.stringify(message) ? old : renderMessage(message));
    }
    list.replaceChildren(...(children.length ? children : [node('div', '暂无符合条件的消息', 'empty')]));
    if (state.resetScroll || atTop) list.scrollTop = 0;
    else { const retained = anchor ? [...list.querySelectorAll('article')].find(row => row.dataset.eventId === anchor.dataset.eventId) : null; list.scrollTop = retained ? oldTop + retained.getBoundingClientRect().top - offset : oldTop; }
    state.resetScroll = false; state.messageSignature = signature;
  } catch (error) { if (request === state.requests.messages) errorAt('messages-error', error); }
}
function setJobOptions() {
  const select = element('history-job'); const previous = select.value;
  select.replaceChildren(...[['', '全部任务'], ...state.jobs.map(job => [job.job_id, job.name])].map(([value, label]) => { const option = node('option', label); option.value = value; return option; })); select.value = previous;
}
function jobRooms(job) { return job.rooms.length ? job.rooms.map(selected => state.rooms.find(room => room.account_id === selected.account_id && room.room_id === selected.room_id)?.room_name || state.monitoring?.rooms.find(room => room.account_id === selected.account_id && room.room_id === selected.room_id)?.room_name || selected.room_id).join('、') : '全部群聊'; }
function renderJob(job) {
  const row = node('tr'); row.dataset.jobId = job.job_id; const name = node('span', job.name, 'cell-truncate'); name.title = job.target_url;
  const rooms = node('span', jobRooms(job), 'cell-truncate'); rooms.title = rooms.textContent;
  const plan = node('span', job.cron); const next = node('small', job.enabled ? time(job.next_run) : '已暂停', 'secondary'); plan.append(next); plan.title = job.timezone;
  const failing = Boolean(job.last_error || job.pending?.last_error);
  const current = status(!job.enabled ? '已暂停' : failing ? '失败待重试' : job.run_until !== null ? '投递中' : '运行中', !job.enabled ? '' : failing ? 'failed' : job.run_until !== null ? 'working' : 'success'); current.title = job.last_error || job.pending?.last_error || '';
  const controls = node('div', undefined, 'row-actions');
  const run = action(job.pending ? '重试' : '发送', async () => { await api(`/api/jobs/${job.job_id}/run`, {}); toast('已安排投递'); await loadJobs(); }); run.disabled = !job.enabled;
  controls.append(run, action(job.enabled ? '暂停' : '启用', async () => { await api(`/api/jobs/${job.job_id}/enabled`, { enabled: !job.enabled }); await loadJobs(); }), action('编辑', () => openJob(job)), action('历史', async () => { element('history-job').value = job.job_id; state.historyBefore = null; await jobView('history'); }));
  row.append(cell(name), cell(rooms), cell(plan), cell(`${job.unsent_count} / ${job.sent_count}`), cell(current), cell(controls)); return row;
}
async function loadJobs() {
  const request = ticket('jobs');
  try { const result = await api('/api/jobs'); if (request !== state.requests.jobs) return; state.jobs = result.jobs; setJobOptions(); element('job-count').textContent = state.jobs.length; element('job-list').replaceChildren(...state.jobs.map(renderJob)); element('jobs-empty').classList.toggle('hidden', state.jobs.length > 0); errorAt('jobs-error', null); if (state.jobView === 'history') await loadHistory(); }
  catch (error) { if (request === state.requests.jobs) errorAt('jobs-error', error); }
}
function showBatch(batch) {
  const content = openDetail('投递详情'); const job = state.jobs.find(job => job.job_id === batch.job_id);
  content.append(metadata([['任务', job?.name || batch.job_id], ['生成时间', time(batch.created_at)], ['确认时间', time(batch.sent_at)], ['消息数', batch.message_count], ['尝试次数', batch.attempts], ['接收地址', batch.target_url], ['投递方式', batch.delivery_mode === 'multipart' ? 'multipart/form-data' : 'ZIP 直传'], ['结果', batch.status === 'sent' ? '成功' : batch.last_error ? '失败，等待重试' : '等待投递']]));
  if (batch.last_error) content.append(node('p', batch.last_error, 'inline-error'));
  if (batch.receipt) { content.append(node('h2', '回执'), node('pre', JSON.stringify(batch.receipt, null, 2))); }
  const download = node('a', '下载文件包'); download.href = `/api/batches/${batch.batch_id}/download`; download.download = `weward-${batch.batch_id}.zip`; content.append(download);
  if (batch.status === 'pending') {
    const retry = action('立即重试', async () => { await api(`/api/batches/${batch.batch_id}/retry`, {}); element('detail-dialog').close(); toast('已安排原批次重试'); await loadJobs(); }, 'quiet', 'batch-detail-error'); retry.disabled = !job?.enabled;
    const error = node('p', undefined, 'inline-error hidden'); error.id = 'batch-detail-error'; content.append(retry, error);
    if (!job?.enabled) content.append(node('p', '请先启用任务，再重试此批次。', 'field-hint'));
  }
  const details = node('details'); details.append(node('summary', '批次信息'), node('pre', JSON.stringify({ batch_id: batch.batch_id, sha256: batch.sha256, from_sequence: batch.from_sequence, through_sequence: batch.through_sequence, success_response: batch.success_response, headers: Object.fromEntries(Object.keys(batch.headers || {}).map(key => [key, '已隐藏'])), text: batch.text }, null, 2))); content.append(details);
}
async function loadHistory() {
  const request = ticket('history'); const query = new URLSearchParams(); if (element('history-job').value) query.set('job_id', element('history-job').value); if (state.historyBefore) query.set('before', state.historyBefore);
  try {
    const result = await api('/api/batches?' + query); if (request !== state.requests.history) return;
    state.historyOldest = result.batches.at(-1)?.sequence; element('history-older').disabled = result.batches.length < 30;
    element('history-empty').classList.toggle('hidden', result.batches.length > 0);
    element('history-list').replaceChildren(...result.batches.map(batch => {
      const row = node('tr'); row.dataset.batchId = batch.batch_id; const controls = node('div', undefined, 'row-actions'); controls.append(action('详情', () => showBatch(batch)));
      const result = status(batch.status === 'sent' ? '成功' : batch.last_error ? '失败 · 待重试' : '待发送', batch.status === 'sent' ? 'success' : batch.last_error ? 'failed' : 'working'); result.title = batch.last_error || batch.receipt?.receipt_id || '';
      row.append(cell(time(batch.created_at)), cell(state.jobs.find(job => job.job_id === batch.job_id)?.name || batch.job_id), cell(batch.message_count), cell(result), cell(batch.attempts), cell(controls)); return row;
    })); errorAt('jobs-error', null);
  } catch (error) { if (request === state.requests.history) errorAt('jobs-error', error); }
}
async function jobView(view) {
  state.jobView = view; for (const button of document.querySelectorAll('[data-job-view]')) { const selected = button.dataset.jobView === view; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); }
  element('active-jobs').classList.toggle('hidden', view !== 'active'); element('job-history').classList.toggle('hidden', view !== 'history'); element('history-job').classList.toggle('hidden', view !== 'history'); if (view === 'history') await loadHistory();
}
function openJob(job = null) {
  state.editing = job?.job_id || null; element('job-form').reset(); errorAt('job-form-error', null); element('job-form-title').textContent = job ? '编辑任务' : '新建任务';
  for (const [id, value] of Object.entries({ 'job-name': job?.name || '', 'job-url': job?.target_url || '', 'job-cron': job?.cron || '0 9 * * *', 'job-timezone': job?.timezone || 'Asia/Shanghai' })) element(id).value = value;
  element('job-delivery-mode').value = job?.delivery_mode || 'zip'; element('job-text').value = job?.text ?? '';
  const rule = job?.success_response || { mode: 'receipt' };
  element('job-success-mode').value = rule.mode; element('job-status-codes').value = Array.isArray(rule.status_codes) ? rule.status_codes.join(',') : '2xx';
  element('job-response-field').value = rule.response_field || ''; element('job-response-value').value = 'response_value' in rule ? JSON.stringify(rule.response_value) : '"submitted"';
  element('job-headers').replaceChildren(); for (const name of Object.keys(job?.headers || {})) addJobHeader(name);
  element('job-snapshot-hint').classList.toggle('hidden', !job?.pending); updateDeliveryFields();
  element('job-enabled').checked = job?.enabled || false; element('job-start-field').classList.toggle('hidden', Boolean(job)); element('job-all-rooms').checked = !job?.rooms.length;
  const rooms = new Map([...state.rooms, ...(state.monitoring?.rooms || [])].map(room => [roomKey(room), room])); for (const room of job?.rooms || []) if (!rooms.has(roomKey(room))) rooms.set(roomKey(room), room);
  element('job-rooms').replaceChildren(...[...rooms.values()].map(room => { const label = node('label', undefined, 'check-field'); const input = node('input'); input.type = 'checkbox'; input.value = JSON.stringify({ account_id: room.account_id, room_id: room.room_id }); input.checked = Boolean(job?.rooms.some(selected => roomKey(selected) === roomKey(room))); label.append(input, node('span', room.room_name || room.room_id)); label.title = `${room.account_id} / ${room.room_id}`; return label; }));
  element('job-rooms').classList.toggle('hidden', element('job-all-rooms').checked); element('job-dialog').showModal();
}
function addJobHeader(savedName = '') {
  const row = node('div', undefined, 'job-header-row'); row.dataset.savedName = savedName;
  const nameLabel = node('label', '名称'); const name = node('input'); name.className = 'job-header-name'; name.value = savedName; name.placeholder = 'Authorization'; name.required = true; name.autocomplete = 'off'; nameLabel.append(name);
  const valueLabel = node('label', '值'); const value = node('input'); value.className = 'job-header-value'; value.type = 'password'; value.autocomplete = 'new-password'; value.placeholder = savedName ? '已保存；留空保留' : 'Bearer …'; valueLabel.append(value);
  name.oninput = () => { value.placeholder = savedName && name.value.toLowerCase() === savedName.toLowerCase() ? '已保存；留空保留' : '输入新值'; };
  row.append(nameLabel, valueLabel, action('移除', () => row.remove(), 'quiet', 'job-form-error')); element('job-headers').append(row);
}
function updateDeliveryFields() {
  element('job-text-field').classList.toggle('hidden', element('job-delivery-mode').value !== 'multipart');
  const custom = element('job-success-mode').value === 'custom';
  for (const id of ['job-status-field', 'job-response-field-label', 'job-response-value-label']) element(id).classList.toggle('hidden', !custom);
}
function renderMonitoring() {
  const result = state.monitoring; if (!result) return;
  element('collector-state').textContent = `${result.account_name} · ${result.rooms.filter(room => room.enabled).length} 个群`;
  element('monitored-empty').classList.toggle('hidden', result.rooms.length > 0);
  element('monitored-list').replaceChildren(...result.rooms.map(room => {
    const row = node('tr'); row.dataset.roomId = room.room_id; const name = node('span', room.room_name || room.room_id, 'cell-truncate'); name.title = room.room_id;
    const controls = node('div', undefined, 'row-actions'); controls.append(action(room.enabled ? '暂停' : '恢复', () => changeMonitoring(room, 'set', !room.enabled), 'quiet', 'settings-error'), action('移除', () => changeMonitoring(room, 'remove'), 'quiet', 'settings-error'));
    row.append(cell(name), cell(status(room.enabled ? '监视中' : '已暂停', room.enabled ? 'success' : '')), cell(controls)); return row;
  }));
}
function renderMatches(matches) {
  element('group-results').classList.remove('hidden');
  element('group-results').replaceChildren(...matches.map(room => {
    const result = node('div', undefined, 'search-result'); const name = node('span', room.room_name || room.room_id); name.append(node('small', room.room_id));
    const button = action(room.monitored ? '已添加' : '监视', () => changeMonitoring(room, 'add'), 'quiet', 'settings-error'); button.disabled = room.monitored; result.append(name, button); return result;
  })); if (!matches.length) element('group-results').append(node('div', '未找到群聊', 'empty'));
}
async function loadMonitoring(query) {
  if (query !== undefined) state.monitorQuery = query;
  else if (state.monitorQuery) query = state.monitorQuery;
  const request = ticket('monitoring');
  try { const result = await api('/api/monitoring' + (query === undefined ? '' : '?' + new URLSearchParams({ query }))); if (request !== state.requests.monitoring) return; state.monitoring = result; renderMonitoring(); if (result.matches) renderMatches(result.matches); errorAt('settings-error', null); }
  catch (error) { if (request === state.requests.monitoring) { element('collector-state').textContent = '采集端未连接'; errorAt('settings-error', error); } }
}
async function changeMonitoring(room, actionName, enabled) {
  ticket('monitoring'); state.monitoring = await api('/api/monitoring', { action: actionName, account_id: room.account_id, room_id: room.room_id, ...(enabled === undefined ? {} : { enabled }) });
  renderMonitoring(); toast(actionName === 'add' ? '已添加监视' : actionName === 'remove' ? '已移除监视' : enabled ? '已恢复监视' : '已暂停监视');
  if (!element('group-results').classList.contains('hidden')) await loadMonitoring(element('group-query').value.trim());
}
async function showTab(tab) {
  state.tab = tab;
  for (const button of document.querySelectorAll('[data-tab]')) { const selected = button.dataset.tab === tab; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); element(button.dataset.tab + '-panel').classList.toggle('hidden', !selected); }
  if (tab === 'messages') await loadMessages(); else if (tab === 'jobs') await loadJobs(); else await Promise.all([loadMonitoring(), loadNotifications(), loadFileSettings()]);
}
async function refresh() {
  try { await loadRooms(); if (state.tab === 'messages') await loadMessages(); else if (state.tab === 'jobs') await loadJobs(); else await Promise.all([loadMonitoring(), loadNotifications(), loadFileSettings()]); }
  catch (error) { element('connection').textContent = '离线'; element('connection').classList.add('offline'); errorAt(state.tab === 'messages' ? 'messages-error' : state.tab === 'jobs' ? 'jobs-error' : 'settings-error', error); }
}
for (const button of document.querySelectorAll('[data-tab]')) button.onclick = () => showTab(button.dataset.tab);
for (const button of document.querySelectorAll('[data-job-view]')) button.onclick = () => jobView(button.dataset.jobView);
for (const button of document.querySelectorAll('[data-close]')) button.onclick = () => element(button.dataset.close).close();
element('room-search').oninput = renderRooms;
element('message-filters').onsubmit = event => { event.preventDefault(); state.filters = { q: element('message-query').value.trim(), type: element('message-type').value, archived: element('message-archived').value }; resetMessages(); loadMessages(); };
element('older').onclick = () => { if (!state.oldest) return; state.trail.push(state.before); state.before = state.oldest; state.resetScroll = true; loadMessages(); };
element('newer').onclick = () => { if (!state.trail.length) return; state.before = state.trail.pop(); state.resetScroll = true; loadMessages(); };
element('latest').onclick = () => { resetMessages(); loadMessages(); };
element('history-older').onclick = () => { state.historyBefore = state.historyOldest; loadHistory(); };
element('history-latest').onclick = () => { state.historyBefore = null; loadHistory(); };
element('history-job').onchange = () => { state.historyBefore = null; loadHistory(); };
element('new-job').onclick = () => openJob();
element('job-add-header').onclick = () => addJobHeader();
element('job-delivery-mode').onchange = updateDeliveryFields;
element('job-success-mode').onchange = updateDeliveryFields;
element('job-dialog').addEventListener('close', () => { element('job-headers').replaceChildren(); });
element('job-all-rooms').onchange = () => element('job-rooms').classList.toggle('hidden', element('job-all-rooms').checked);
element('group-search-form').onsubmit = event => { event.preventDefault(); loadMonitoring(element('group-query').value.trim()); };
element('job-form').onsubmit = async event => {
  event.preventDefault(); element('save-job').disabled = true;
  try {
    const rooms = element('job-all-rooms').checked ? [] : [...element('job-rooms').querySelectorAll('input:checked')].map(input => JSON.parse(input.value)); if (!element('job-all-rooms').checked && !rooms.length) throw new Error('请选择群聊');
    const headers = Object.create(null), names = new Set();
    for (const row of element('job-headers').children) {
      const name = row.querySelector('.job-header-name').value.trim(), value = row.querySelector('.job-header-value').value;
      if (names.has(name.toLowerCase())) throw new Error('请求头名称不能重复'); names.add(name.toLowerCase());
      headers[name] = !value && row.dataset.savedName && name.toLowerCase() === row.dataset.savedName.toLowerCase() ? null : value;
    }
    const success_response = { mode: element('job-success-mode').value };
    if (success_response.mode === 'custom') {
      const codes = element('job-status-codes').value.trim();
      if (codes !== '2xx' && !/^2\d\d(?:\s*,\s*2\d\d)*$/.test(codes)) throw new Error('成功状态码请填写 2xx 或逗号分隔的 200–299 状态码');
      success_response.status_codes = codes === '2xx' ? codes : codes.split(',').map(Number); success_response.response_field = element('job-response-field').value.trim();
      if (success_response.response_field) {
        try { success_response.response_value = JSON.parse(element('job-response-value').value); }
        catch { throw new Error('预期值必须是 JSON；字符串请加双引号，例如 "submitted"'); }
      }
    }
    await api('/api/jobs' + (state.editing ? '/' + state.editing : ''), { name: element('job-name').value, target_url: element('job-url').value, cron: element('job-cron').value, timezone: element('job-timezone').value, enabled: element('job-enabled').checked, start: element('job-start').value, rooms, headers, delivery_mode: element('job-delivery-mode').value, text: element('job-text').value || null, success_response });
    element('job-dialog').close(); toast('任务已保存'); await loadJobs();
  } catch (error) { errorAt('job-form-error', error); } finally { element('save-job').disabled = false; }
};
async function loadNotifications() {
  try {
    const result = await api('/api/notifications');
    if (!state.notificationLoaded) {
      const config = result.config;
      element('notification-enabled').checked = config.enabled; element('notification-url').value = config.target_url;
      for (const key of ['headers', 'body']) element('notification-' + key).value = JSON.stringify(config[key], null, 2);
      element('notification-response-field').value = config.response_field; element('notification-response-value').value = JSON.stringify(config.response_value);
      element('notification-events').replaceChildren(...Object.entries(result.events).map(([eventName, label]) => {
        const row = node('div', undefined, 'notification-rule'); row.dataset.event = eventName;
        const toggleLabel = node('label', undefined, 'check-field'); const toggle = node('input'); toggle.type = 'checkbox'; toggle.checked = config.events[eventName].enabled;
        toggleLabel.append(toggle, document.createTextNode(label)); const template = node('textarea'); template.rows = 2; template.value = config.events[eventName].message; template.setAttribute('aria-label', label + '通知信息');
        row.append(toggleLabel, template); return row;
      }));
      element('notification-placeholders').textContent = '可用占位符：' + result.placeholders.map(key => '{{' + key + '}}').join('、') + '。{{message}} 仅用于请求体，展开为该事件的通知信息；不适用字段为空。';
      state.notificationLoaded = true;
    }
    const labels = { pending: '待发送 / 重试', sent: '已发送', failed: '失败（已停止重试）', cancelled: '已取消' };
    element('notification-history').replaceChildren(...result.history.map(item => {
      const row = node('tr'); row.append(cell(time(item.created_at)), cell(result.events[item.event] || item.event), cell(labels[item.status]), cell(item.attempts), cell(item.last_error || '—')); return row;
    }));
  } catch (error) { errorAt('notification-error', error); }
}
element('notification-form').onsubmit = async event => {
  event.preventDefault(); element('save-notifications').disabled = true;
  try {
    const events = Object.fromEntries([...element('notification-events').children].map(row => [row.dataset.event, { enabled: row.querySelector('input').checked, message: row.querySelector('textarea').value }]));
    await api('/api/notifications', { enabled: element('notification-enabled').checked, target_url: element('notification-url').value,
      headers: JSON.parse(element('notification-headers').value), body: JSON.parse(element('notification-body').value), events,
      response_field: element('notification-response-field').value.trim(), response_value: JSON.parse(element('notification-response-value').value) });
    errorAt('notification-error', null); element('notification-saved').textContent = '已保存'; toast('通知配置已保存'); await loadNotifications();
  } catch (error) { errorAt('notification-error', error); } finally { element('save-notifications').disabled = false; }
};
element('notification-form').oninput = () => { element('notification-saved').textContent = '有未保存修改'; };
async function loadFileSettings() {
  if (state.fileSettingsLoaded) return;
  try {
    const config = await api('/api/file-settings');
    element('files-enabled').checked = config.enabled; element('files-max-size').value = config.max_size_mb;
    element('files-extensions').value = config.extensions.join(', ');
    element('file-settings-fields').disabled = false; state.fileSettingsLoaded = true;
    errorAt('file-settings-error', null);
  } catch (error) { errorAt('file-settings-error', error); }
}
element('file-settings-form').onsubmit = async event => {
  event.preventDefault(); element('save-file-settings').disabled = true;
  try {
    const config = await api('/api/file-settings', { enabled: element('files-enabled').checked,
      extensions: element('files-extensions').value.split(/[,，;；\s]+/).filter(Boolean),
      max_size_mb: Number(element('files-max-size').value) });
    element('files-extensions').value = config.extensions.join(', ');
    errorAt('file-settings-error', null); element('file-settings-saved').textContent = '已保存'; toast('文件设置已保存');
  } catch (error) { errorAt('file-settings-error', error); } finally { element('save-file-settings').disabled = false; }
};
element('file-settings-form').oninput = () => { element('file-settings-saved').textContent = '有未保存修改'; };
refresh(); setInterval(refresh, 5000);
