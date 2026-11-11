/* Read-only progress polling, scoped to the selected node and calculation. */
const calculationMonitor = (() => {
  let context = null, controller = null;
  const element = id => document.getElementById(id);
  const number = (value, digits = 6) => value == null ? '—' : Number(value).toFixed(digits);
  const time = value => value ? new Date(value).toLocaleString(preferences.language) : t("暂无记录");

  function chart(points, label) {
    if (!points.length) return t("<p class=\"muted\">暂无可绘制的迭代记录</p>");
    const low = Math.min(...points.map(p => p.energy)), high = Math.max(...points.map(p => p.energy));
    const first = points[0].step, last = points.at(-1).step;
    const coords = points.map(p => [48 + (p.step - first) / Math.max(last - first, 1) * 286,
      20 + (high - p.energy) / (high - low || 1) * 92]);
    return `<svg viewBox="0 0 350 145" role="img" aria-label="${escapeHTML(label)}">
      <path class="plot-axis" d="M48 16V116H336"/>
      <text x="44" y="20" text-anchor="end">${high.toFixed(2)}</text>
      <text x="44" y="116" text-anchor="end">${low.toFixed(2)}</text>
      <text x="48" y="137">${first}</text><text x="334" y="137" text-anchor="end">${last}${t(" 步</text>")}
      <polyline class="plot-line" points="${coords.map(p => p.join(',')).join(' ')}"/>
      <circle class="plot-dot" cx="${coords.at(-1)[0]}" cy="${coords.at(-1)[1]}" r="2.5"/>
    </svg>`;
  }

  function clearDisplay() {
    for (const name of ['metrics', 'charts', 'log', 'message', 'summary', 'resources', 'freshness', 'source'])
      element('calculation-' + name).textContent = '';
  }

  function clear() {
    controller?.abort(); controller = null; context = null;
    element('calculation-live').hidden = true;
    clearDisplay();
  }

  function selectNode(workspace, node, data) {
    const plans = data.parameters.calculations || [];
    const names = [...new Set([...data.jobs.map(job => job.subdir), ...plans.map(plan => plan.subdir)])];
    const key = `${workspace}/${node}`;
    const changed = context?.key !== key;
    if (changed) clear();
    const select = element('calculation-select'), previous = changed ? null : select.value;
    const preferred = data.jobs.find(job => ['RUNNING', 'SUBMITTED'].includes(job.state))?.subdir;
    select.replaceChildren(...names.map(name => new Option(
      `${name}${data.authoritative_calculations[name] ? ' ' + t('· 已被修复尝试替代') : ''}`, name)));
    select.value = names.includes(previous) ? previous : preferred || names[0] || '';
    context = {key, workspace, node, plans};
    element('calculation-live').hidden = !names.length;
    if (names.length && (changed || previous !== select.value)) {
      controller?.abort(); controller = null; clearDisplay(); refresh();
    }
  }

  async function refresh() {
    if (!context || document.hidden || element('node-overview').hidden || element('calculation-live').hidden || controller) return;
    const selected = element('calculation-select').value, current = context;
    const request = new AbortController(); controller = request;
    element('calculation-message').textContent = t("正在读取计算输出…");
    try {
      const url = `/api/workspaces/${encodeURIComponent(current.workspace)}/nodes/${encodeURIComponent(current.node)}/progress?subdir=${encodeURIComponent(selected)}`;
      const response = await fetch(url, {signal: request.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      if (context?.key !== current.key || element('calculation-select').value !== selected) return;
      const electronic = data.history.electronic.at(-1), ionic = data.history.ionic.at(-1);
      const job = data.job, plan = context.plans.find(item => item.subdir === selected);
      const metrics = [
        [t("最近电子步"), electronic?.step ?? t("尚无记录")],
        [t("电子迭代能量 / eV"), number(electronic?.energy)],
        [t("电子 ΔE / eV"), electronic ? electronic.delta.toExponential(3) : '—'],
        [t("最近完整离子步"), ionic?.step ?? t("尚无记录")],
        [t("离子步 E₀ / eV"), number(ionic?.energy)],
        [t("最近最大力 / eV·Å⁻¹"), number(data.max_force, 4)],
      ];
      element('calculation-metrics').innerHTML = metrics.map(([label, value]) =>
        `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd></div>`).join('');
      element('calculation-summary').textContent = `${t("作业 ")}${data.job_id || t("未提交")} · ${job.state || t("暂无调度快照")}${t(" · 科学收敛：")}${job.converged == null ? t("未确认") : job.converged ? t("已收敛") : t("未收敛")}`;
      const resources = plan?.submit_params;
      element('calculation-resources').textContent = [
        resources?.total_cpus ? `${resources.total_cpus} CPU` : null, plan?.resource_profile,
        job.time_used ? `${t("已用时 ")}${job.time_used}` : null, job.time_limit ? `${t("时限 ")}${job.time_limit}` : null,
      ].filter(Boolean).join(' · ');
      const age = data.output_updated_at ? Math.max(0, Math.floor((Date.now() - Date.parse(data.output_updated_at)) / 60000)) : null;
      element('calculation-freshness').textContent = `${t("本次读取 ")}${time(data.sampled_at)}${t(" · 输出更新 ")}${time(data.output_updated_at)}${age == null ? '' : ` (${age}${t(" 分钟前）")}`}\n${t("调度快照 ")}${time(job.last_checked)}${t("。输出无新增不等于作业失败。")}`;
      element('calculation-charts').innerHTML =
        `<div><h4>${t("最近电子迭代 · E / eV")}</h4>${chart(data.history.electronic, t("电子迭代能量"))}</div>
         <div><h4>${t("最近离子步 · E₀ / eV")}</h4>${chart(data.history.ionic, t("离子步能量"))}</div>`;
      const log = element('calculation-log'), bottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
      log.textContent = data.log_tail || t("尚无 Slurm 输出。");
      if (bottom) log.scrollTop = log.scrollHeight;
      element('calculation-log-name').textContent = data.log_name || t("Slurm 日志");
      element('calculation-message').textContent = job.error_message ||
        (electronic && !ionic ? t("正在电子迭代，近期尚无完整离子步记录；电子能量不是最终收敛能量。") : t("电子与离子能量分开展示；科学收敛以分析结果为准。"));
      element('calculation-source').textContent = `${t("进度来源：")}${data.source || t("暂无")}${t(" · 限读文件尾部，最多 120 个点，不代表完整轨迹。")}`;
    } catch (error) {
      if (error.name !== 'AbortError' && context?.key === current.key)
        element('calculation-message').textContent = `${t("更新失败，保留上次数据：")}${error.message}`;
    } finally {
      if (controller === request) controller = null;
    }
  }

  element('calculation-select').onchange = () => {
    controller?.abort(); controller = null;
    clearDisplay();
    refresh();
  };
  element('calculation-refresh').onclick = refresh;
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(refresh, 10000);
  return {selectNode, clear, refresh};
})();
