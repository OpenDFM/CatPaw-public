const modelSettings = (() => {
  const get = id => document.getElementById(id);
  const roles = {
    supervisor: 'Supervisor', coordinator: 'Coordinator',
    modeling_worker: 'Modeling Worker', modeling_validator: 'Modeling Validator',
    param_worker: 'Param Worker', param_validator: 'Param Validator',
    calculation_runner: 'Calculation Runner', analyzer: 'Analyzer', reporter: 'Reporter',
  };
  const dialog = get('settings-dialog');
  const fields = {base_url: get('settings-url'), api_key: get('settings-key'), model: get('settings-model')};
  let saved = {mode: 'all', all: {}, individual: {}};
  let draft, selected = 'supervisor';
  const current = () => draft.mode === 'all' ? draft.all : (draft.individual[selected] ||= {});
  function capture() {
    for (const [key, input] of Object.entries(fields)) current()[key] = input.value.trim();
  }
  function render() {
    get('settings-mode').value = draft.mode;
    get('settings-agent-title').textContent = draft.mode === 'all' ? t("全部 Agent") : roles[selected];
    const items = draft.mode === 'all' ? {all: t("全部 Agent")} : roles;
    get('settings-nav').replaceChildren(...Object.entries(items).map(([role, label]) => {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = label;
      button.setAttribute('aria-pressed', String(role === selected || role === 'all'));
      button.onclick = () => { capture(); if (role !== 'all') selected = role; render(); };
      return button;
    }));
    for (const [key, input] of Object.entries(fields)) input.value = current()[key] || '';
    get('settings-error').textContent = '';
  }
  get('open-settings').onclick = () => {
    draft = structuredClone(saved); selected = 'supervisor'; render(); dialog.showModal();
  };
  get('settings-mode').onchange = event => {
    capture(); draft.mode = event.target.value; render();
  };
  get('close-settings').onclick = () => dialog.close();
  dialog.onclick = event => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
  };
  dialog.onclose = () => {
    draft = null;
    for (const input of Object.values(fields)) input.value = '';
  };
  get('reset-settings').onclick = () => { draft = {mode: 'all', all: {}, individual: {}}; render(); };
  function connections(settings) {
    const entries = settings.mode === 'all' ? {all: settings.all} : settings.individual;
    return Object.fromEntries(Object.entries(entries).filter(([, values]) => Object.values(values).some(Boolean)));
  }
  get('settings-form').onsubmit = event => {
    event.preventDefault(); capture();
    for (const [role, values] of Object.entries(connections(draft))) {
      let validURL = false;
      try {
        const url = new URL(values.base_url);
        validURL = ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password;
      } catch { /* Display validation in the relevant Agent pane. */ }
      if (!values.base_url || !values.api_key || !values.model || !validURL) {
        if (draft.mode === 'individual') selected = role;
        render(); get('settings-error').textContent = t("请填写有效的 HTTP(S) Base URL、API Key 和 Model，或将三项全部清空。");
        return;
      }
    }
    saved = structuredClone(draft); dialog.close();
    get('open-settings').title = Object.keys(connections(saved)).length ? t("已保存当前页面的模型设置") : t("使用服务端配置");
  };
  window.addEventListener('languagechange', () => { if (draft) { capture(); render(); } });
  return {connections: () => structuredClone(connections(saved))};
})();
