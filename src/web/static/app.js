const $ = id => document.getElementById(id);
const state = {workspace: null, node: null, detail: null, nodeDetail: null, conversation: null, allConversations: false, before: null, zoom: 1};
const labels = {stopping: "正在停止", stopped: "已停止", stop_failed: "停止未完成", awaiting_review: "等待人工审核", done: "完成", active: "进行中", running: "执行中", ready: "就绪", pending: "等待依赖", failed: "失败", initializing: "初始化"};
const escapeHTML = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const base = () => `/api/workspaces/${encodeURIComponent(state.workspace)}`;
const asset = path => `/workspace-assets/${encodeURIComponent(state.workspace)}/${path.split('/').map(encodeURIComponent).join('/')}`;
async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  return data;
}
function showError(error) { $('error').textContent = error.message; $('error').hidden = false; }
function badge(id, status) { $(id).textContent = t(labels[status]) || status; $(id).dataset.status = status; }
function empty(id, text) { $(id).replaceChildren(Object.assign(document.createElement('p'), {className:'empty', textContent:text})); }
function workspaceDisplay(name) {
  // Workspace suffixes use server-local wall time; preserve it across browser time zones.
  const match = name.match(/^(.+)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
  if (!match) return {title: name};
  const [, title, year, month, day, hour, minute, second] = match;
  const datetime = `${year}-${month}-${day}T${hour}:${minute}:${second}`;
  const date = new Date(datetime + 'Z');
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 19) !== datetime) return {title: name};
  const calendar = new Intl.DateTimeFormat(preferences.language, {
    year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC',
  }).format(date);
  return {title, datetime, dateLabel: `${calendar} · ${hour}:${minute}`};
}
async function refresh(updateDetail = true) {
  const selected = state.workspace;
  $('error').hidden = true;
  const index = await api('/api/workspaces');
  $('workspaces').replaceChildren(...index.workspaces.map(workspace => {
    const button = document.createElement('button');
    button.className = `workspace ${workspace.name === state.workspace ? 'selected' : ''}`;
    const display = workspaceDisplay(workspace.name);
    button.title = workspace.name;
    button.innerHTML = `<span class="workspace-name">${escapeHTML(display.title)}</span>
      ${display.datetime ? `<time class="workspace-date" datetime="${display.datetime}"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18"/></svg>${escapeHTML(display.dateLabel)}</time>` : ''}
      <small>${escapeHTML(t(labels[workspace.status]) || workspace.status)} · ${workspace.counts.done || 0}/${Object.values(workspace.counts).reduce((a,b) => a+b, 0)}${t(" 节点</small>")}`;
    button.dataset.search = `${workspace.name} ${workspace.goal}`.toLowerCase();
    button.onclick = () => selectWorkspace(workspace.name).catch(showError);
    return button;
  }));
  filterWorkspaces();
  if (!index.workspaces.length) empty('workspaces', t("暂无工作区"));
  if (selected && state.workspace === selected && !index.workspaces.some(item => item.name === selected)) showNewTask();
  if (updateDetail && state.workspace) await loadWorkspace();
}
function showNewTask() {
  $('evidence-dialog').close();
  calculationMonitor.clear();
  state.workspace = null; state.node = null; state.detail = null;
  $('new-task').hidden = false;
  $('workspace-header').hidden = true; $('workspace-nav').hidden = true; $('detail').hidden = true;
  $('error').hidden = true;
  document.querySelectorAll('.workspace.selected').forEach(el => el.classList.remove('selected'));
  toggleSidebar(false);
  history.replaceState(null, '', location.pathname + location.search);
  window.scrollTo(0, 0);
  resizeGoal(); $('launch').elements.goal.focus();
}
$('new-run').onclick = showNewTask;
$('home-link').onclick = event => { event.preventDefault(); showNewTask(); };
async function selectWorkspace(name) {
  calculationMonitor.clear();
  $('evidence-dialog').close();
  renderedDocument = null;
  toggleSidebar(false);
  state.workspace = name; state.node = null; state.allConversations = false; state.nodeDetail = null; state.conversation = null;
  $('structure-preview').hidden = true; previewKey = null; $('preview-display').hidden = true;
  $('stages').replaceChildren(); $('structures').replaceChildren(); $('document-content').textContent = ''; $('visuals').replaceChildren(); $('node-inputs').replaceChildren();
  $('node-title').textContent = t("节点详情"); $('node-goal').textContent = t("点击 DAG 中的节点查看输入和结果。");
  $('node-error').textContent = ''; $('node-status').textContent = ''; $('document').replaceChildren();
  history.replaceState(null, '', `#${encodeURIComponent(name)}`);
  await refresh();
}
async function loadWorkspace() {
  const name = state.workspace;
  const detail = await api(base());
  if (name !== state.workspace) return;
  state.detail = detail;
  $('new-task').hidden = true; $('workspace-header').hidden = false; $('workspace-nav').hidden = false;
  $('weave-link').hidden = !detail.weave.url;
  if (detail.weave.url) $('weave-link').href = detail.weave.url;
  $('detail').hidden = false; $('title').textContent = name; $('goal').textContent = detail.summary.goal;
  badge('status', detail.summary.status);
  $('task-actions').hidden = !detail.managed;
  $('stop-run').disabled = ['stopping', 'stopped'].includes(detail.summary.status);
  if (detail.control.error) showError(new Error(detail.control.error));
  $('counts').textContent = `${detail.summary.counts.done || 0} / ${detail.nodes.length}${t(" 节点完成")}`;
  $('report').innerHTML = detail.report || t("等待计算证据完成后生成报告。");
  $('report-link').href = asset('final_report.md'); $('report-link').hidden = !detail.report;
  $('log').textContent = detail.log || t("此工作区没有 Web 启动日志；执行过程见 Agent trace。");
  renderApiWaits(detail.api_waits);
  renderReview(detail);
  renderDAG();
  if (state.node) await loadNode(state.node);
  else { renderConversations(detail.conversations); await loadTrace(); }
}
function renderApiWaits(waits) {
  const now = Date.now();
  const active = waits.filter(item => new Date(item.retry_at).getTime() > now);
  $('api-wait-panel').hidden = !active.length;
  if (!active.length) return;
  const reasons = {
    APIConnectionError: t("API 连接失败"),
    RateLimitError: t("请求频率受限"),
    ServiceUnavailableError: t("API 服务暂不可用"),
    Timeout: t("API 请求超时"),
    InternalServerError: t("API 服务内部错误"),
    LLMNoResponseError: t("模型未返回有效内容"),
  };
  $('api-wait-count').textContent = `${active.length}${t(" 个请求等待中")}`;
  $('api-waits').replaceChildren(...active.map(item => {
    const row = document.createElement('div');
    const seconds = Math.max(1, Math.ceil((new Date(item.retry_at).getTime() - now) / 1000));
    const attempts = item.max_attempts ? `${item.attempt}/${item.max_attempts}` : `${item.attempt}${t("/不限")}`;
    row.className = 'api-wait';
    row.innerHTML = `<strong>${escapeHTML(item.role)} · ${escapeHTML(item.model)}</strong><span>${escapeHTML(reasons[item.reason] || item.reason)}${t(" · 第 ")}${attempts}${t(" 次尝试 · ")}${seconds}${t(" 秒后重试")}</span>`;
    return row;
  }));
}
setInterval(() => {
  if (state.detail) renderApiWaits(state.detail.api_waits);
}, 1000);
function renderDAG() {
  const nodes = state.detail.nodes;
  if (!nodes.length) return empty('dag', t("Supervisor 正在规划计算 DAG。"));
  const depths = new Map();
  const remaining = new Map(nodes.map(n => [n.id, n]));
  while (remaining.size) {
    let advanced = false;
    for (const [id, node] of remaining) {
      if (node.dependencies.every(dep => depths.has(dep))) {
        depths.set(id, Math.max(-1, ...node.dependencies.map(dep => depths.get(dep))) + 1);
        remaining.delete(id); advanced = true;
      }
    }
    if (!advanced) throw new Error(t("DAG 存在缺失依赖或循环，无法绘制。"));
  }
  const rows = new Map(), positions = new Map();
  for (const node of nodes) {
    const depth = depths.get(node.id), row = rows.get(depth) || 0;
    positions.set(node.id, {x: 28 + (depth + 1) * 265, y: 28 + row * 95}); rows.set(depth, row + 1);
  }
  const width = (Math.max(...depths.values()) + 2) * 265 + 24;
  const height = Math.max(...rows.values()) * 95 + 40;
  let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width*state.zoom}" height="${height*state.zoom}" viewBox="0 0 ${width} ${height}"><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#b7c8d2"/></marker></defs>`;
  const roots = nodes.filter(node => !node.dependencies.length);
  const startY = roots.reduce((sum, node) => sum + positions.get(node.id).y, 0) / roots.length;
  svg += `<g class="dag-start" role="img" aria-label="${t("Start，工作流入口")}" transform="translate(28,${startY})"><rect width="220" height="64" rx="32"/><text x="110" y="38" text-anchor="middle">Start</text></g>`;
  for (const node of roots) {
    const b = positions.get(node.id);
    svg += `<path class="edge start-edge" marker-end="url(#arrow)" d="M248 ${startY+32} C272 ${startY+32},${b.x-24} ${b.y+32},${b.x-3} ${b.y+32}"/>`;
  }
  for (const node of nodes) for (const dep of node.dependencies) {
    const a = positions.get(dep), b = positions.get(node.id), x = a.x + 220;
    svg += `<path class="edge" marker-end="url(#arrow)" d="M${x} ${a.y+32} C${x+24} ${a.y+32},${b.x-24} ${b.y+32},${b.x-3} ${b.y+32}"/>`;
  }
  for (const node of nodes) {
    const p = positions.get(node.id), id = escapeHTML(node.id);
    svg += `<g class="dag-node ${state.node === node.id ? 'selected' : ''}" data-id="${id}" data-status="${escapeHTML(node.status)}" tabindex="0" role="button" aria-label="${id}: ${escapeHTML(t(labels[node.status]) || node.status)}" transform="translate(${p.x},${p.y})"><title>${escapeHTML(node.title)}</title><rect width="220" height="64" rx="8"/><circle cx="16" cy="21" r="4"/><text x="28" y="25">${escapeHTML(node.id.length > 25 ? node.id.slice(0,24)+'…' : node.id)}</text><text class="node-subtitle" x="14" y="47">${escapeHTML(t(labels[node.status]) || node.status)} · ${node.dependencies.length}${t(" 个依赖")}</text></g>`;
  }
  $('dag').innerHTML = svg + '</svg>';
  for (const node of $('dag').querySelectorAll('.dag-node')) {
    const select = () => { state.node = node.dataset.id; state.allConversations = false; state.conversation = null; renderDAG(); loadNode(state.node).catch(showError); };
    node.onclick = select;
    node.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } };
  }
}
async function loadNode(id) {
  if (state.nodeDetail?.node.node_id !== id) calculationMonitor.clear();
  const workspace = state.workspace, detail = await api(`${base()}/nodes/${encodeURIComponent(id)}`);
  if (workspace !== state.workspace || state.node !== id) return;
  if (state.nodeDetail?.node.node_id !== id) { $('structures').replaceChildren();  }
  state.nodeDetail = detail;
  $('node-title').textContent = detail.node.title; $('node-goal').textContent = detail.node.goal;
  badge('node-status', detail.node.status); $('node-error').textContent = detail.node.last_error || '';
  $('node-inputs').replaceChildren(...Object.entries(detail.node.inputs).map(([role, ref]) => {
    const row = document.createElement('div'); row.className = 'input-row';
    row.innerHTML = `<strong>${escapeHTML(role)}</strong><button>${escapeHTML(ref)}</button>`;
    row.querySelector('button').onclick = () => { state.node = ref.split('/')[0]; state.conversation = null; renderDAG(); loadNode(state.node).catch(showError); };
    return row;
  }));
  const selected = $('document').value;
  $('document').replaceChildren(...Object.keys(detail.documents).map(name => new Option(name, name)));
  if (selected in detail.documents) $('document').value = selected;
  renderInspection(detail.inspection);
  renderParameters();
  renderDocument();
  renderStructurePreview();
  $('visuals').replaceChildren(...detail.visuals.map(path => {
    const link = document.createElement('a'); link.href = asset(path); link.target = '_blank'; link.rel = 'noopener';
    if (path.endsWith('.html')) link.textContent = `${t("结构查看器 ↗ ")}${path.split('/').at(-1)}`;
    else { const img = document.createElement('img'); img.src = asset(path); img.alt = path.split('/').at(-1); img.loading = 'lazy'; link.append(img); }
    return link;
  }));
  renderConversations(state.allConversations ? state.detail.conversations : detail.conversations); await loadTrace();
}
let previewKey = null;
function renderStructurePreview() {
  const data = state.nodeDetail.inspection, select = $('preview-source');
  const selected = select.value;
  select.replaceChildren(...data.structure_files.map(path => new Option(path, path)));
  if (data.structure_files.includes(selected)) select.value = selected;
  $('structure-preview').hidden = !data.structure_files.length;
  const saved = data.structure_previews[select.value];
  const key = saved ? asset(saved.path) + '?v=' + saved.revision : null;
  $('generate-preview').textContent = saved ? t("重新生成并保存") : t("生成并保存");
  if (key && key === previewKey) return;
  previewKey = key;
  $('preview-display').hidden = true;
  $('preview-status').textContent = saved ? t("正在加载结构快照…") : t("尚未生成，点击后保存并展示三维结构。");
  if (saved) showStructurePreview(key);
}
async function showStructurePreview(key) {
  try {
    const renderer = await import('/static/structure-viewer.js');
    const [snapshot] = await Promise.all([api(key), renderer.prepare()]);
    if (previewKey !== key) return;
    $('preview-display').hidden = false;
    renderer.show(snapshot);
    $('preview-status').textContent = t("已保存的结构快照；文件变化后可重新生成。");
  } catch (error) {
    if (previewKey !== key) return;
    previewKey = null;
    $('preview-display').hidden = true;
    $('preview-status').textContent = error.message;
  }
}
$('preview-source').onchange = renderStructurePreview;
$('generate-preview').onclick = async () => {
  const workspace = state.workspace, node = state.node, button = $('generate-preview');
  button.disabled = true; $('preview-status').textContent = t("正在生成…");
  try {
    await api(`${base()}/nodes/${encodeURIComponent(node)}/preview`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:$('preview-source').value}),
    });
    if (state.workspace === workspace && state.node === node) await loadNode(node);
  } catch (error) {
    if (state.workspace === workspace && state.node === node) $('preview-status').textContent = `${t("生成失败：")}${error.message}`;
  } finally { button.disabled = false; }
};
function evidence(name) {
  $('document').value = name;
  selectTab('evidence');
  renderDocument();
}
function selectTab(tab) {
  for (const name of ['overview', 'parameters', 'evidence']) {
    $(`node-${name}`).hidden = name !== tab;
    $(`tab-${name}`).setAttribute('aria-selected', String(name === tab));
  }
  if (tab === 'overview') calculationMonitor.refresh();
}
function renderParameters() {
  const files = state.nodeDetail.inspection.parameter_files;
  const select = $('parameter-calculation'), selected = select.value;
  const names = Object.keys(files);
  select.replaceChildren(...names.map(name => new Option(name === '.' ? t("节点根目录") : name, name)));
  if (names.includes(selected)) select.value = selected;
  select.disabled = !names.length;
  $('parameter-empty').hidden = !!names.length;
  $('parameter-files').hidden = !names.length;
  renderParameterFiles();
}
function renderParameterFiles() {
  const files = state.nodeDetail.inspection.parameter_files[$('parameter-calculation').value] || {};
  for (const name of ['INCAR', 'KPOINTS']) {
    $(`parameter-${name.toLowerCase()}`).textContent = files[name] === undefined
      ? `${t("尚未生成 ")}${name}` : files[name] || `${name}${t(" 文件为空。")}`;
  }
  const lattice = files.POSCAR;
  const message = !lattice ? t("尚未生成 POSCAR。") : lattice.error || '';
  $('parameter-lattice-message').textContent = message;
  $('parameter-lattice-message').hidden = !message;
  $('parameter-lattice').hidden = !!message;
  $('parameter-lattice').replaceChildren();
  if (!message) {
    const labels = ['a / Å', 'b / Å', 'c / Å', t("α（b 与 c）/ °"), t("β（a 与 c）/ °"), t("γ（a 与 b）/ °")];
    $('parameter-lattice').innerHTML = [...lattice.lengths, ...lattice.angles].map((value, index) =>
      `<div><dt>${labels[index]}</dt><dd>${value.toFixed(index < 3 ? 5 : 3)}</dd></div>`).join('');
  }
}
$('parameter-calculation').onchange = renderParameterFiles;
function renderInspection(data) {
  const opened = new Set([...document.querySelectorAll('#structures details[open]')].map(row => row.className + row.querySelector('strong').textContent));
  const stage = (title, status, filename) => {
    const button = document.createElement('button');
    button.className = 'stage'; button.dataset.status = status;
    button.innerHTML = `<span>${title}</span><strong>${escapeHTML(status)}</strong>`;
    button.onclick = () => evidence(filename);
    return button;
  };
  $('stages').replaceChildren(
    stage(t("建模验证"), data.modeling_validation.status || (data.modeling.structures ? t("待验证") : t("暂无证据")), 'modeling_validation.json'),
    stage(t("参数验证"), data.parameter_validation.status || (data.parameters.calculations ? t("待验证") : t("暂无证据")), 'parameter_validation.json'),
    stage(t("结果分析"), data.result.node_status || t("暂无结果"), 'node_result.json'),
  );
  $('structures').innerHTML = t("<h3>结构与来源</h3>");
  for (const structure of data.modeling.structures || []) {
    const row = document.createElement('details'); row.className = 'structure-row';
    row.innerHTML = `<summary><strong>${escapeHTML(structure.subdirectory)}</strong><span>${escapeHTML(structure.atom_count ?? '—')}${t(" 原子 · ")}${escapeHTML(structure.task_type_hint || '')}</span></summary><p>${escapeHTML(structure.content || '')}</p><p class="muted">${t("来源：")}${escapeHTML(structure.provenance?.source_path || structure.poscar_source || t("未记录"))}</p>`;
    $('structures').append(row);
  }
  if (!data.modeling.structures?.length) $('structures').append(Object.assign(document.createElement('p'), {className:'muted', textContent:t("尚未生成结构摘要。")}));
  $('structures').append(Object.assign(document.createElement('h3'), {textContent:t("结构文件下载")}));
  for (const path of data.structure_files) {
    const row = document.createElement('div'); row.className = 'input-row';
    const link = document.createElement('a');
    link.href = asset(path); link.download = path.split('/').at(-1);
    link.textContent = `${t("下载 ")}${path}`;
    row.append(link); $('structures').append(row);
  }
  if (!data.structure_files.length) $('structures').append(Object.assign(document.createElement('p'), {className:'muted', textContent:t("尚未生成 POSCAR/CONTCAR。")}));
  calculationMonitor.selectNode(state.workspace, state.node, data);
  for (const row of document.querySelectorAll('#structures details')) row.open = opened.has(row.className + row.querySelector('strong').textContent);
}
function renderTimeline(items) {
  const timed = items.filter(item => item.started_at && item.last_event_at);
  if (!timed.length) return empty('timeline', t("暂无时间记录"));
  const start = Math.min(...timed.map(item => Date.parse(item.started_at)));
  const span = Math.max(1, Math.max(...timed.map(item => Date.parse(item.last_event_at))) - start);
  $('timeline').replaceChildren(...timed.map(item => {
    const elapsed = Date.parse(item.last_event_at) - Date.parse(item.started_at);
    const duration = elapsed >= 3600000 ? `${(elapsed / 3600000).toFixed(1)} h` : elapsed >= 60000 ? `${(elapsed / 60000).toFixed(1)} min` : `${(elapsed / 1000).toFixed(1)} s`;
    const button = document.createElement('button'); button.className = 'timeline-row';
    button.classList.toggle('selected', item.id === state.conversation);
    button.title = `${item.agent} · ${item.started_at} → ${item.last_event_at} · ${item.status}`;
    button.innerHTML = `<span class="agent-name">${item.parent ? '↳ ' : ''}${escapeHTML(item.agent)}</span><span class="time-track"><i style="left:${100*(Date.parse(item.started_at)-start)/span}%;width:${100*elapsed/span}%"></i></span><small>${duration}</small>`;
    button.onclick = () => { state.conversation = item.id; renderConversations(items); loadTrace().catch(showError); };
    return button;
  }));
}
$('tab-overview').onclick = () => selectTab('overview');
$('tab-parameters').onclick = () => selectTab('parameters');
$('tab-evidence').onclick = () => selectTab('evidence');
let renderedDocument = null;
function jsonTree(value, key = '', depth = 0) {
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value), array = Array.isArray(value);
    const details = document.createElement('details'); details.className = 'json-branch'; details.open = depth < 2;
    const summary = document.createElement('summary');
    summary.textContent = `${key ? key + ': ' : ''}${array ? '[' : '{'} ${entries.length}${t(" 项 ")}${array ? ']' : '}'}`;
    const children = document.createElement('div'); children.className = 'json-children';
    children.append(...entries.map(([name, item]) => jsonTree(item, name, depth + 1)));
    details.append(summary, children); return details;
  }
  const row = document.createElement('div'); row.className = 'json-leaf';
  const label = document.createElement('span'); label.className = 'json-key'; label.textContent = `${key}: `;
  const content = document.createElement('span'); content.className = `json-${value === null ? 'null' : typeof value}`;
  content.textContent = JSON.stringify(value); row.append(label, content); return row;
}
function renderDocument() {
  const name = $('document').value, detail = state.nodeDetail;
  const text = detail.documents[name] || '';
  const source = $('document-source').getAttribute('aria-pressed') === 'true';
  const key = `${state.workspace}/${state.node}/${name}/${source}`;
  $('document-download').href = asset(`${detail.node.workspace_subdir}/${name}`);
  $('document-download').hidden = !text;
  $('evidence-title').textContent = `${detail.node.title} · ${name}`;
  if (renderedDocument?.key === key && renderedDocument.text === text) return;
  renderedDocument = {key, text};
  const container = $('document-content'); container.replaceChildren(); container.scrollTop = 0;
  if (!text) { container.textContent = t("尚未生成。"); return; }
  if (!source && name.endsWith('.md')) {
    const article = document.createElement('article'); article.className = 'report evidence-markdown';
    article.innerHTML = detail.document_html[name]; container.append(article); return;
  }
  if (!source && name.endsWith('.json')) {
    try { container.append(jsonTree(JSON.parse(text))); return; }
    catch (error) {
      const notice = document.createElement('p'); notice.className = 'failure';
      notice.textContent = `${t("JSON 格式错误，以下显示原文：")}${error.message}`; container.append(notice);
    }
  }
  const pre = document.createElement('pre'); pre.textContent = text; container.append(pre);
}
$('document-source').onclick = () => {
  const button = $('document-source'); button.setAttribute('aria-pressed', button.getAttribute('aria-pressed') !== 'true');
  renderDocument();
};
$('expand-evidence').onclick = () => {
  $('evidence-dialog-body').append($('evidence-viewer'));
  $('expand-evidence').hidden = true; $('evidence-dialog').showModal();
};
$('close-evidence').onclick = () => $('evidence-dialog').close();
$('evidence-dialog').onclose = () => {
  $('node-evidence').append($('evidence-viewer')); $('expand-evidence').hidden = false;
};
function renderConversations(items) {
  $('conversation').replaceChildren(...items.map(c => new Option(`${c.parent ? '↳ ' : ''}${c.workspace || 'workspace'} / ${c.agent} · ${c.status} · ${c.id.split('/').at(-1).slice(0,8)}`, c.id)));
  if (!items.some(c => c.id === state.conversation)) state.conversation = items[0]?.id || null;
  $('conversation').value = state.conversation || '';
  renderTimeline(items);
}
async function loadTrace(older = false) {
  const workspace = state.workspace, conversation = state.conversation;
  if (!conversation) { $('older').hidden = true; return empty('trace', t("暂无持久化会话。")); }
  const query = new URLSearchParams({conversation});
  if (older && state.before !== null) query.set('before', state.before);
  const data = await api(`${base()}/trace?${query}`);
  if (workspace !== state.workspace || conversation !== state.conversation) return;
  const changed = $('trace').dataset.conversation !== conversation;
  if (changed) { $('trace').replaceChildren(); $('trace').dataset.conversation = conversation; }
  const existing = new Set([...$('trace').querySelectorAll('[data-event]')].map(e => e.dataset.event));
  const elements = data.events.filter(event => !existing.has(event.id)).map(event => {
    const item = document.createElement('details'); item.className = 'event'; item.dataset.kind = event.kind; item.dataset.event = event.id;
    const summary = document.createElement('summary');
    const time = document.createElement('small'); time.textContent = `${event.timestamp.replace('T',' ')} · ${event.kind}`;
    summary.append(time, document.createTextNode(event.summary || event.tool || 'Message'));
    const pre = document.createElement('pre'); pre.textContent = event.content + (event.truncated ? '\n' + t('…内容过长，已截断') : '');
    item.append(summary, pre); return item;
  });
  if (older) $('trace').prepend(...elements); else $('trace').append(...elements);
  if (changed || older) state.before = data.before;
  $('older').hidden = state.before === null;
  if (!data.events.length && !older) empty('trace', t("当前页暂无工具或消息事件。"));
}
$('document').onchange = renderDocument;
$('conversation').onchange = () => { state.conversation = $('conversation').value; renderTimeline(state.allConversations || !state.nodeDetail ? state.detail.conversations : state.nodeDetail.conversations); loadTrace().catch(showError); };
$('older').onclick = () => loadTrace(true).catch(showError);
$('all-traces').onclick = () => { state.allConversations = true; renderConversations(state.detail.conversations); loadTrace().catch(showError); };
$('refresh').onclick = () => refresh().catch(showError);
$('zoom-in').onclick = () => { state.zoom = Math.min(2, state.zoom + .15); renderDAG(); };
$('zoom-out').onclick = () => { state.zoom = Math.max(.5, state.zoom - .15); renderDAG(); };
function renderAttachment() {
  const file = $('structure-file').files[0];
  $('attachment').hidden = !file;
  $('attachment-name').textContent = file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KB` : '';
}
$('attach-structure').onclick = () => $('structure-file').click();
$('structure-file').onchange = renderAttachment;
$('remove-structure').onclick = () => { $('structure-file').value = ''; renderAttachment(); };
$('review-mode').onclick = () => {
  const button = $('review-mode');
  button.setAttribute('aria-pressed', button.getAttribute('aria-pressed') !== 'true');
};
function resizeGoal() {
  const input = $('launch').elements.goal;
  input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 280)}px`;
}
$('launch').elements.goal.oninput = resizeGoal;
$('launch').elements.goal.onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('launch').querySelector('[type=submit]').disabled) $('launch').requestSubmit();
  }
};
$('launch').onsubmit = async event => {
  event.preventDefault();
  const form = event.target, button = form.querySelector('[type=submit]');
  const file = $('structure-file').files[0];
  if (file && (!file.size || file.size > 10 * 1024 * 1024)) {
    $('launch-status').textContent = t("请选择非空且不超过 10 MB 的结构文件。"); return;
  }
  const payload = Object.fromEntries(new FormData(form));
  payload.agent_connections = modelSettings.connections();
  payload.review_mode = $('review-mode').getAttribute('aria-pressed') === 'true' ? 'enabled' : 'disabled';
  button.disabled = true; $('launch-status').textContent = t("正在提交…");
  try {
    if (file) {
      const content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(',')[1]);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      payload.structure = {name: file.name, content};
    }
    const result = await api('/api/runs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    form.reset(); $('review-mode').setAttribute('aria-pressed', 'false'); renderAttachment();
    $('launch-status').textContent = ''; await selectWorkspace(result.workspace_name);
  } catch (error) { $('launch-status').textContent = error.message; } finally { button.disabled = false; }
};
let refreshPending = false, detailPending = false, refreshing = false;
async function updateFromEvents(workspaces) {
  refreshPending = true;
  detailPending ||= workspaces === null || workspaces.includes(state.workspace);
  if (refreshing) return;
  refreshing = true;
  try {
    while (refreshPending) {
      const updateDetail = detailPending;
      refreshPending = false; detailPending = false;
      await refresh(updateDetail);
    }
  } catch (error) { showError(error); } finally { refreshing = false; }
}
state.workspace = location.hash ? decodeURIComponent(location.hash.slice(1)) : null;
const events = new EventSource('/api/events');
events.onopen = () => { $('live-status').textContent = t("实时更新已连接"); $('live-status').dataset.connected = 'true'; };
events.onmessage = event => updateFromEvents(JSON.parse(event.data).workspaces);
events.onerror = () => { $('live-status').textContent = t("连接中断，正在重连…"); $('live-status').dataset.connected = 'false'; };
window.addEventListener('pagehide', () => events.close());

function filterWorkspaces() {
  const query = $('workspace-search').value.trim().toLowerCase();
  for (const button of $('workspaces').querySelectorAll('button')) button.hidden = !button.dataset.search.includes(query);
}
function toggleSidebar(open) {
  $('sidebar').classList.toggle('open', open);
  $('scrim').hidden = !open;
  $('open-sidebar').setAttribute('aria-expanded', String(open));
  if (open) $('workspace-search').focus();
}
$('workspace-search').oninput = filterWorkspaces;
$('open-sidebar').onclick = () => toggleSidebar(true);
$('close-sidebar').onclick = () => { toggleSidebar(false); $('open-sidebar').focus(); };
$('scrim').onclick = () => toggleSidebar(false);
document.addEventListener('keydown', event => { if (event.key === 'Escape') toggleSidebar(false); });
for (const link of document.querySelectorAll('.topbar a')) link.onclick = event => {
  event.preventDefault();
  document.querySelector(link.getAttribute('href')).scrollIntoView({behavior: 'smooth'});
};

function renderReview(detail) {
  const review = detail.review;
  const key = review.id ? `${state.workspace}/${review.id}` : '';
  if ($('review-panel').dataset.request !== key) {
    $('review-feedback').value = ''; $('review-status').textContent = '';
    $('review-panel').dataset.request = key;
  }
  $('review-panel').hidden = !review.id;
  if (!review.id) return;
  const titles = {dag_plan: t("DAG 方案审核"), conversation: t("Agent 请求审核"), node_result: t("节点结果审核"), restart_plan: t("重启计划审核")};
  $('review-title').textContent = titles[review.kind] || t("人工审核");
  $('review-prompt').textContent = review.prompt;
  $('review-result').textContent = review.result ? JSON.stringify(review.result, null, 2) : '';
  $('review-result').parentElement.hidden = !review.result;
  $('review-node').hidden = !review.node_id;
  $('review-node').onclick = () => {
    state.node = review.node_id; state.allConversations = false; state.conversation = null;
    renderDAG(); loadNode(review.node_id).catch(showError);
    $('inspection').scrollIntoView({behavior: 'smooth'});
  };
  $('review-form').querySelectorAll('button, textarea').forEach(el => el.disabled = detail.review_submitted);
  if (detail.review_submitted) $('review-status').textContent = t("意见已提交，等待执行器处理。");
}
$('review-form').onsubmit = async event => {
  event.preventDefault();
  const workspace = state.workspace, review = state.detail.review;
  const decision = event.submitter.value, feedback = $('review-feedback').value.trim();
  if (decision === 'REVISE' && !feedback) {
    $('review-status').textContent = t("请填写修改意见。"); $('review-feedback').focus(); return;
  }
  const controls = $('review-form').querySelectorAll('button, textarea');
  controls.forEach(el => el.disabled = true);
  try {
    await api(`${base()}/review`, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: review.id, decision, feedback})});
    if (state.workspace === workspace) await loadWorkspace();
  } catch (error) {
    if (state.workspace === workspace) {
      $('review-status').textContent = error.message; controls.forEach(el => el.disabled = false);
    }
  }
};

$('stop-run').onclick = async () => {
  const workspace = state.workspace;
  $('stop-run').disabled = true;
  try {
    await api(`${base()}/stop`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    if (state.workspace === workspace) await refresh();
  } catch (error) {
    if (state.workspace === workspace) { showError(error); $('stop-run').disabled = false; }
  }
};
$('delete-run').onclick = () => {
  $('delete-name').textContent = state.workspace;
  $('delete-dialog').dataset.workspace = state.workspace;
  $('delete-error').textContent = '';
  $('delete-dialog').showModal();
  $('cancel-delete').focus();
};
$('cancel-delete').onclick = () => $('delete-dialog').close();
$('delete-form').onsubmit = async event => {
  event.preventDefault();
  const workspace = $('delete-dialog').dataset.workspace;
  $('confirm-delete').disabled = true;
  try {
    await api(`/api/workspaces/${encodeURIComponent(workspace)}/delete`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirmation: workspace}),
    });
    $('delete-dialog').close();
    if (state.workspace === workspace) showNewTask();
    await refresh();
  } catch (error) { $('delete-error').textContent = error.message; }
  finally { $('confirm-delete').disabled = false; }
};

window.addEventListener('languagechange', () => {
  renderedDocument = null;
  $('live-status').textContent = $('live-status').dataset.connected === 'true'
    ? t('实时更新已连接') : t('连接中断，正在重连…');
  if (state.detail) {
    badge('status', state.detail.summary.status);
    renderReview(state.detail);
    renderApiWaits(state.detail.api_waits);
    renderDAG();
  }
  if (!state.node) {
    $('node-title').textContent = t('节点详情');
    $('node-goal').textContent = t('点击 DAG 中的节点查看输入和结果。');
  }
  if (state.node && state.nodeDetail) {
    badge('node-status', state.nodeDetail.node.status);
    renderInspection(state.nodeDetail.inspection);
    renderParameters();
    renderDocument();
    renderStructurePreview();
  }
  refresh().catch(showError);
  calculationMonitor.refresh();
  resizeGoal();
});
