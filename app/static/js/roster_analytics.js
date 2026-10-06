// Roster Analytics — /roster/analytics client.
//
// One page, six tabs. Data flows: on tab switch, fetch the section's
// JSON endpoint (unless already loaded for the current filter combo)
// and render the associated chart. Tab HTML is server-rendered; JS is
// only responsible for chart hydration and filter changes.
//
// Filter changes invalidate ALL tab caches — the enrolled counts and
// windows apply everywhere. Cheap: one fetch per active tab as user
// scrubs.

(function () {
  const CHART_COLORS = {
    added:       '#2ecc71',
    removed:     '#e74c3c',
    transferred: '#3498db',
    enrolled:    '#f39c12',
    current:     '#4a90e2',
    prior:       '#95a5a6',
    active:      '#2ecc71',
    suspended:   '#f39c12',
    archived:    '#95a5a6',
    missing:     '#e74c3c',
    bucket:      ['#2ecc71', '#7ed321', '#f5a623', '#f39c12', '#e74c3c', '#95a5a6'],
  };

  const state = {
    activeTab: 'enrollment',
    building: '',
    days: 30,
    charts: {},  // chartId → Chart instance
    loaded: new Set(),  // section keys that have been fetched for the current filter combo
  };

  function el(sel, root) { return (root || document).querySelector(sel); }
  function all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  function fmtDate(iso) {
    // "2026-09-09" → "9/9"
    if (!iso) return '';
    const parts = iso.split('-');
    return `${parseInt(parts[1], 10)}/${parseInt(parts[2], 10)}`;
  }

  function destroyChart(id) {
    if (state.charts[id]) { state.charts[id].destroy(); delete state.charts[id]; }
  }

  function renderEmpty(canvasId, msg) {
    destroyChart(canvasId);
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--text-muted') || '#888';
    ctx.font = '13px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(msg || 'No data', canvas.width / 2, canvas.height / 2);
  }

  async function fetchJSON(url) {
    const r = await fetch(url, {
      headers: { 'X-Requested-With': 'CommandNexus', 'Accept': 'application/json' },
      credentials: 'same-origin',
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return await r.json();
  }

  // ── Section loaders ────────────────────────────────────────────────

  async function loadBuildings() {
    try {
      const data = await fetchJSON('/api/roster/analytics/buildings');
      const sel = el('#filter-building');
      const cur = sel.value;
      // Clear existing option nodes past the first (District all).
      while (sel.options.length > 1) sel.remove(1);
      for (const b of data.buildings) {
        const opt = document.createElement('option');
        opt.value = b; opt.textContent = b;
        sel.appendChild(opt);
      }
      sel.value = cur;
    } catch (e) { /* leave dropdown as-is */ }
  }

  async function loadEnrollment() {
    const url = `/api/roster/analytics/enrollment?days=${state.days}&building=${encodeURIComponent(state.building)}`;
    const data = await fetchJSON(url);
    destroyChart('chart-enrollment');
    if (!data.series.length) { renderEmpty('chart-enrollment', 'No snapshots yet — run snapshot_roster_analytics'); return; }
    const labels = data.series.map(p => fmtDate(p.date));
    state.charts['chart-enrollment'] = new Chart(el('#chart-enrollment'), {
      type: 'bar',
      data: {
        labels,
        datasets: [
          { label: 'Added',       type: 'bar', backgroundColor: CHART_COLORS.added,       data: data.series.map(p => p.added),       stack: 'delta' },
          { label: 'Removed',     type: 'bar', backgroundColor: CHART_COLORS.removed,     data: data.series.map(p => -p.removed),    stack: 'delta' },
          { label: 'Transferred', type: 'bar', backgroundColor: CHART_COLORS.transferred, data: data.series.map(p => p.transferred), stack: 'delta' },
          { label: 'Enrolled',    type: 'line', borderColor: CHART_COLORS.enrolled, backgroundColor: 'transparent', yAxisID: 'y2', data: data.series.map(p => p.enrolled), tension: 0.2, pointRadius: 0 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, position: 'left', title: { display: true, text: 'Δ per day' } },
          y2: { position: 'right', grid: { display: false }, title: { display: true, text: 'Enrolled' } },
        },
        plugins: { legend: { position: 'top' } },
      },
    });
  }

  async function loadGrade() {
    const url = `/api/roster/analytics/grade?building=${encodeURIComponent(state.building)}`;
    const data = await fetchJSON(url);
    destroyChart('chart-grade');
    el('#grade-yoy-label').style.display = data.yoy_available ? '' : 'none';
    if (!data.grades.length) { renderEmpty('chart-grade', 'No grade data'); return; }
    const datasets = [{ label: `Current (${data.current_date})`, backgroundColor: CHART_COLORS.current, data: data.current }];
    if (data.yoy_available) {
      datasets.push({ label: `YoY (${data.prior_date})`, backgroundColor: CHART_COLORS.prior, data: data.prior });
    }
    state.charts['chart-grade'] = new Chart(el('#chart-grade'), {
      type: 'bar',
      data: { labels: data.grades, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { title: { display: true, text: 'Students enrolled' } } },
        plugins: { legend: { position: 'top' } },
      },
    });
  }

  async function loadGoogle() {
    const url = `/api/roster/analytics/google?days=${state.days}`;
    const data = await fetchJSON(url);
    destroyChart('chart-google-trend');
    if (data.trend.length) {
      const labels = data.trend.map(p => fmtDate(p.date));
      state.charts['chart-google-trend'] = new Chart(el('#chart-google-trend'), {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'Active',    backgroundColor: CHART_COLORS.active,    borderColor: CHART_COLORS.active,    data: data.trend.map(p => p.active),    fill: 'origin', tension: 0.2, pointRadius: 0 },
            { label: 'Suspended', backgroundColor: CHART_COLORS.suspended, borderColor: CHART_COLORS.suspended, data: data.trend.map(p => p.suspended), fill: 'origin', tension: 0.2, pointRadius: 0 },
            { label: 'Archived',  backgroundColor: CHART_COLORS.archived,  borderColor: CHART_COLORS.archived,  data: data.trend.map(p => p.archived),  fill: 'origin', tension: 0.2, pointRadius: 0 },
            { label: 'Missing',   backgroundColor: CHART_COLORS.missing,   borderColor: CHART_COLORS.missing,   data: data.trend.map(p => p.missing),   fill: 'origin', tension: 0.2, pointRadius: 0 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: { y: { stacked: true, title: { display: true, text: 'Accounts' } } },
          plugins: { legend: { position: 'top' } },
        },
      });
    } else {
      renderEmpty('chart-google-trend', 'No snapshots yet');
    }

    // Provision SLA histogram — archive intentionally omitted until
    // the reconcile Dir-B write path is unpaused (see snapshot job
    // comment). The template renders a placeholder in its slot.
    const canvasId = 'chart-sla-provision';
    destroyChart(canvasId);
    const total = (data.sla_provision || []).reduce((a, b) => a + b, 0);
    if (!total) {
      renderEmpty(canvasId, 'No SLA events yet — awaiting backfill');
    } else {
      state.charts[canvasId] = new Chart(el(`#${canvasId}`), {
        type: 'bar',
        data: {
          labels: data.sla_buckets,
          datasets: [{
            label: 'Provisions',
            backgroundColor: CHART_COLORS.bucket,
            data: data.sla_provision,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: { y: { title: { display: true, text: 'Count' } } },
          plugins: { legend: { display: false } },
        },
      });
    }
  }

  async function loadCapacity() {
    const data = await fetchJSON('/api/roster/analytics/capacity');
    const tbody = el('#tbl-capacity tbody');
    tbody.innerHTML = '';
    for (const b of data.buildings) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${b.building}</td>
        <td class="num">${b.enrolled}</td>
        <td class="num">${b.cb_assigned}</td>
        <td class="num">${b.coverage_pct}%</td>
        <td class="num">${b.avg_age_days ?? '—'}</td>
        <td class="num">${b.repairs_30d}</td>
        <td class="num">${b.repairs_per_100}</td>
      `;
      tbody.appendChild(tr);
    }

    destroyChart('chart-cb-coverage');
    if (!data.buildings.length) { renderEmpty('chart-cb-coverage', 'No capacity data'); return; }
    state.charts['chart-cb-coverage'] = new Chart(el('#chart-cb-coverage'), {
      type: 'bar',
      data: {
        labels: data.buildings.map(b => b.building),
        datasets: [
          { label: 'Enrolled',    backgroundColor: CHART_COLORS.enrolled, data: data.buildings.map(b => b.enrolled) },
          { label: 'CB Assigned', backgroundColor: CHART_COLORS.current,  data: data.buildings.map(b => b.cb_assigned) },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { title: { display: true, text: 'Count' } } },
        plugins: { legend: { position: 'top' } },
      },
    });
  }

  async function loadEngagement() {
    const data = await fetchJSON('/api/roster/analytics/engagement');
    // Server signals availability; nothing to render if unavailable — the
    // placeholder is already server-rendered.
    if (!data.available) return;
    // (Future: render login-rate bars here.)
  }

  async function loadAnomaly() {
    const data = await fetchJSON('/api/roster/analytics/anomaly');
    const tbody = el('#tbl-imports tbody');
    tbody.innerHTML = '';
    for (const imp of data.recent_imports) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${imp.at ? imp.at.replace('T', ' ').slice(0, 16) : '—'}</td>
        <td>${imp.source || '—'}</td>
        <td>${imp.status || '—'}</td>
        <td class="num">${imp.added ?? 0}</td>
        <td class="num">${imp.removed ?? 0}</td>
        <td class="num">${imp.errors ?? 0}</td>
        <td>${imp.notes || ''}</td>
      `;
      tbody.appendChild(tr);
    }

    const spTbody = el('#tbl-sparklines tbody');
    spTbody.innerHTML = '';
    for (const s of data.sparklines) {
      const latest = s.points.length ? s.points[s.points.length - 1].enrolled : 0;
      const first = s.points.length ? s.points[0].enrolled : 0;
      const delta = latest - first;
      const deltaStr = (delta > 0 ? '+' : '') + delta;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${s.building}</td>
        <td><canvas class="sparkline" data-points='${JSON.stringify(s.points.map(p => p.enrolled))}'></canvas></td>
        <td class="num">${latest}</td>
        <td class="num">${deltaStr}</td>
      `;
      spTbody.appendChild(tr);
    }

    // Draw sparklines
    for (const cv of all('.sparkline', spTbody)) {
      const pts = JSON.parse(cv.dataset.points || '[]');
      const dpr = window.devicePixelRatio || 1;
      cv.width = 120 * dpr; cv.height = 24 * dpr;
      const c = cv.getContext('2d');
      c.scale(dpr, dpr);
      if (!pts.length) continue;
      const min = Math.min(...pts), max = Math.max(...pts);
      const range = max - min || 1;
      c.strokeStyle = CHART_COLORS.current; c.lineWidth = 1.5; c.beginPath();
      pts.forEach((p, i) => {
        const x = (i / Math.max(pts.length - 1, 1)) * 120;
        const y = 24 - ((p - min) / range) * 22 - 1;
        if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
      });
      c.stroke();
    }
  }

  async function loadAttendance() {
    const url = `/api/roster/analytics/attendance?days=${state.days}&building=${encodeURIComponent(state.building)}`;
    const data = await fetchJSON(url);

    // Summary cards
    el('#att-chronic').textContent = data.summary.chronic_count.toLocaleString();
    el('#att-avg').textContent = data.summary.avg_rate_pct.toFixed(2) + '%';
    el('#att-total').textContent = data.summary.total_students.toLocaleString();
    el('#att-computed').textContent = data.summary.last_computed
      ? data.summary.last_computed.replace('T', ' ').slice(0, 16)
      : 'never';

    // Trend chart — one line per school
    destroyChart('chart-attendance-trend');
    if (!data.trend_by_school.length) {
      renderEmpty('chart-attendance-trend', 'No attendance data yet');
    } else {
      // Union of all dates so short-history schools still align
      const allDates = Array.from(new Set(
        data.trend_by_school.flatMap(s => s.points.map(p => p.date))
      )).sort();
      const labels = allDates.map(fmtDate);
      const palette = ['#4a90e2', '#2ecc71', '#f39c12', '#e74c3c', '#9b59b6', '#1abc9c', '#e67e22', '#95a5a6'];
      const datasets = data.trend_by_school.map((s, i) => {
        const byDate = Object.fromEntries(s.points.map(p => [p.date, p.rate]));
        return {
          label: s.school,
          borderColor: palette[i % palette.length],
          backgroundColor: 'transparent',
          data: allDates.map(d => (d in byDate ? byDate[d] : null)),
          tension: 0.2, pointRadius: 2, spanGaps: true,
        };
      });
      state.charts['chart-attendance-trend'] = new Chart(el('#chart-attendance-trend'), {
        type: 'line',
        data: { labels, datasets },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: { y: { beginAtZero: true, title: { display: true, text: 'Absence rate %' } } },
          plugins: { legend: { position: 'top' } },
        },
      });
    }

    // Chronic table
    const tbody = el('#tbl-attendance-chronic tbody');
    tbody.innerHTML = '';
    if (!data.chronic.length) {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td colspan="7" style="text-align:center;color:var(--text-muted);padding:24px">No chronic-absent students yet</td>';
      tbody.appendChild(tr);
    } else {
      for (const s of data.chronic) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td class="mono">${s.sis_id}</td>
          <td>${s.name}</td>
          <td>${s.school}</td>
          <td>${s.grade}</td>
          <td class="num">${s.absent}</td>
          <td class="num">${s.enrolled}</td>
          <td class="num"><span class="status-badge ${s.rate >= 20 ? 'err' : 'warn'}">${s.rate.toFixed(1)}%</span></td>
        `;
        tbody.appendChild(tr);
      }
    }
    el('#att-chronic-caption').textContent = data.chronic.length >= 200
      ? 'Top 200 by rate'
      : `${data.chronic.length} student${data.chronic.length === 1 ? '' : 's'}`;
  }

  const loaders = {
    enrollment: loadEnrollment,
    grade:      loadGrade,
    attendance: loadAttendance,
    google:     loadGoogle,
    capacity:   loadCapacity,
    engagement: loadEngagement,
    anomaly:    loadAnomaly,
  };

  async function loadActiveTab() {
    const key = `${state.activeTab}:${state.building}:${state.days}`;
    if (state.loaded.has(key)) return;
    try {
      await loaders[state.activeTab]();
      state.loaded.add(key);
    } catch (e) {
      console.error(`Failed to load ${state.activeTab}:`, e);
    }
  }

  function switchTab(tab) {
    state.activeTab = tab;
    all('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    all('.tab-pane').forEach(p => p.classList.toggle('active', p.dataset.panel === tab));
    loadActiveTab();
  }

  function bindControls() {
    all('.tab-btn').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
    el('#filter-building').addEventListener('change', e => {
      state.building = e.target.value;
      state.loaded.clear();
      loadActiveTab();
    });
    el('#filter-days').addEventListener('change', e => {
      state.days = parseInt(e.target.value, 10) || 30;
      state.loaded.clear();
      loadActiveTab();
    });
  }

  document.addEventListener('DOMContentLoaded', async () => {
    bindControls();
    await loadBuildings();
    loadActiveTab();
  });
})();
