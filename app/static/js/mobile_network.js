/*
 * Command Nexus — Mobile network shell driver.
 *
 * Loaded only by network.html. Powers the .net-m-shell section
 * that's rendered inside the same template as the desktop UI
 * but gated to mobile viewports via mobile.css media queries.
 *
 * Reuses every existing /api/network/* endpoint the desktop
 * page uses — no new routes except the QR PNG.
 *
 * Screen state machine:
 *   buildings → switches (in one building)
 *             → ports   (specific switch)
 *   search    → from any screen via the top search bar
 *
 * On first init we check the viewport. If we're on desktop, we
 * bail entirely — the existing desktop JS (loadSummary, loadTopology,
 * etc.) still drives everything. On mobile, we take over.
 */

(function () {
  // Bail out on desktop — desktop JS already handles the page.
  if (!window.matchMedia('(max-width: 768px)').matches) {
    return;
  }

  // ── State ──────────────────────────────────────────────────────
  const M = {
    screen: 'buildings',
    building: null,      // selected building code
    buildingName: null,
    switchId: null,      // selected switch device_id
    switchName: null,
    switchIp: null,
    selectedPort: null,  // ifName of selected port
    ports: [],
    portNotes: {},
    allVlans: [],        // global VLAN list for the picker
    vlanTemplates: [],   // saved template list (global, desktop-managed)
    portVlans: null,     // {untagged: N, tagged: [N,N]}
    beacon: false,
    searchTimer: null,
    searchQuery: '',
    scanner: null,       // ZXing BrowserQRCodeReader instance
    scannerStream: null, // MediaStream for the video element
    // Cache device list so we don't refetch on every building click
    allDevices: null,
    // Debounce the load that summarises buildings so rapid refresh
    // (e.g. from a back navigation) doesn't triple-fetch.
    initialized: false,
  };

  const H = { 'X-Requested-With': 'CommandNexus' };
  const HP = { 'X-Requested-With': 'CommandNexus', 'Content-Type': 'application/json' };

  // Expose a couple of functions globally so inline onclick=""
  // attributes in the template can reach them.
  window.mNetBack = mNetBack;
  window.mNetBeaconToggle = mNetBeaconToggle;
  window.mNetQrOpen = mNetQrOpen;
  window.mNetQrClose = mNetQrClose;
  window.mNetSearchInput = mNetSearchInput;
  window.mNetSearchClear = mNetSearchClear;
  window.mNetVlanSheetClose = mNetVlanSheetClose;

  // ── Utilities ──────────────────────────────────────────────────
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;',
      '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  let toastTimer;
  function toast(msg, kind) {
    const el = document.getElementById('mtoast');
    if (!el) return;
    el.className = 'mtoast';
    if (kind) el.classList.add(kind);
    el.textContent = msg;
    // Force reflow so re-adding .show retriggers the animation
    void el.offsetWidth;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
  }

  function showScreen(name) {
    document.querySelectorAll('.net-m-shell .mscreen').forEach(s => s.classList.remove('active'));
    const el = document.getElementById('mscreen-' + name);
    if (el) el.classList.add('active');
    M.screen = name;
    updTopbar();
  }

  function updTopbar() {
    const back = document.getElementById('mstb-back');
    const beacon = document.getElementById('mstb-beacon');
    const title = document.getElementById('mstb-title');
    const sub = document.getElementById('mstb-sub');

    if (M.screen === 'buildings' || M.screen === 'search') {
      back.style.display = M.screen === 'search' ? 'flex' : 'none';
      beacon.style.display = 'none';
      title.textContent = 'Network';
      sub.textContent = 'COMMAND NEXUS';
    } else if (M.screen === 'switches') {
      back.style.display = 'flex';
      beacon.style.display = 'none';
      title.textContent = M.buildingName || M.building || 'Building';
      const count = (M.allDevices || []).filter(d => d.building === M.building).length;
      sub.textContent = `${count} SWITCH${count === 1 ? '' : 'ES'}`;
    } else if (M.screen === 'ports') {
      back.style.display = 'flex';
      beacon.style.display = 'flex';
      beacon.classList.toggle('beacon-on', M.beacon);
      title.textContent = M.switchName || 'Switch';
      sub.textContent = M.switchIp || '';
    }
  }

  function mNetBack() {
    if (M.screen === 'ports') {
      M.selectedPort = null;
      M.beacon = false;
      showScreen('switches');
      renderSwitches();
    } else if (M.screen === 'switches') {
      M.building = null;
      M.buildingName = null;
      showScreen('buildings');
    } else if (M.screen === 'search') {
      // Return to wherever the user came from
      document.getElementById('mssearch-input').value = '';
      document.getElementById('mssearch-clear').classList.remove('on');
      M.searchQuery = '';
      if (M.switchId) showScreen('ports');
      else if (M.building) showScreen('switches');
      else showScreen('buildings');
    }
  }

  // ── API ────────────────────────────────────────────────────────
  async function fetchDevices() {
    if (M.allDevices) return M.allDevices;
    try {
      // Server-side filter via query param rather than
      // client-side filtering — the API response uses the key
      // `type` (not `device_type`) which bit us on the first
      // pass when the filter silently excluded every device.
      const r = await fetch('/api/network/devices?device_type=network', { headers: H });
      if (!r.ok) throw new Error('devices');
      const data = await r.json();
      M.allDevices = data.devices || [];
      return M.allDevices;
    } catch (e) {
      toast('Failed to load devices', 'err');
      return [];
    }
  }

  async function fetchPorts(deviceId) {
    const r = await fetch(`/api/network/devices/${deviceId}/ports`, { headers: H });
    if (!r.ok) throw new Error('ports');
    return r.json();
  }

  async function fetchPortNotes(deviceId) {
    try {
      const r = await fetch(`/api/network/port-notes/${deviceId}`, { headers: H });
      if (!r.ok) return {};
      return await r.json();
    } catch { return {}; }
  }

  async function fetchAllVlans() {
    if (M.allVlans.length) return M.allVlans;
    try {
      const r = await fetch('/api/network/vlans', { headers: H });
      if (!r.ok) throw new Error('vlans');
      const data = await r.json();
      M.allVlans = data.vlans || [];
      return M.allVlans;
    } catch { return []; }
  }

  async function fetchVlanTemplates() {
    if (M.vlanTemplates.length) return M.vlanTemplates;
    try {
      const r = await fetch('/api/network/vlan-templates', { headers: H });
      if (!r.ok) throw new Error('templates');
      M.vlanTemplates = await r.json() || [];
      return M.vlanTemplates;
    } catch { return []; }
  }

  async function fetchPortVlans(deviceId, portId) {
    try {
      const r = await fetch(`/api/network/switch/${deviceId}/port/${encodeURIComponent(portId)}/vlans`, { headers: H });
      if (!r.ok) return null;
      return await r.json();
    } catch { return null; }
  }

  // ── Buildings screen ───────────────────────────────────────────
  async function renderBuildings() {
    const list = document.getElementById('mbuildings-list');
    list.innerHTML = '<div class="mempty">Loading buildings…</div>';
    const devices = await fetchDevices();
    if (!devices.length) {
      list.innerHTML = '<div class="mempty">No switches found</div>';
      return;
    }

    // Aggregate by building
    const bmap = {};
    for (const d of devices) {
      const key = d.building || 'Unassigned';
      if (!bmap[key]) bmap[key] = { name: key, total: 0, up: 0, warn: 0, down: 0 };
      bmap[key].total++;
      // status=1 = up in LibreNMS cache; anything else we treat as down.
      // The cache doesn't carry a "warn" state at the device level, so
      // "warn" stays 0 unless we layer alert data on later.
      if (d.status === 1) bmap[key].up++;
      else bmap[key].down++;
    }

    // Load /api/buildings for nice display names (full school name
    // + brand color). Best-effort; the API returns an `aliases`
    // list per building so we can match whatever value the cache
    // happens to carry (SIS code, internal code, etc.).
    let buildingMeta = {};  // lookup: lowered-alias → {name, color}
    try {
      const bRes = await fetch('/api/buildings', { headers: H });
      if (bRes.ok) {
        const bData = await bRes.json();
        for (const b of (bData.buildings || [])) {
          const meta = { name: b.name, color: b.color, code: b.code };
          for (const alias of (b.aliases || [])) {
            buildingMeta[alias.toLowerCase()] = meta;
          }
          buildingMeta[b.code.toLowerCase()] = meta;
        }
      }
    } catch {}

    const sorted = Object.values(bmap).sort((a, b) => a.name.localeCompare(b.name));
    const header = `
      <div class="msec">
        <div class="msec-lbl">Buildings</div>
        <div class="msec-line"></div>
        <div class="msec-ct">${sorted.length}</div>
      </div>
    `;

    const cards = sorted.map(b => {
      let status = 'ok', badgeClass = 'ok', badgeText = 'ALL OK';
      if (b.down > 0) { status = 'bad'; badgeClass = 'bad'; badgeText = 'ISSUE'; }
      else if (b.warn > 0) { status = 'warn'; badgeClass = 'warn'; badgeText = 'WARNING'; }
      const upP = b.total ? (b.up / b.total) * 100 : 0;
      const wP = b.total ? (b.warn / b.total) * 100 : 0;
      const dP = 100 - upP - wP;

      const meta = buildingMeta[(b.name || '').toLowerCase()];
      const display = meta?.name || b.name;
      const statsHtml = `
        <div class="mbcard-stats">
          <div class="mbcstat"><div class="mbcstat-lbl">Switches</div><div class="mbcstat-val">${b.total}</div></div>
          <div class="mbcstat"><div class="mbcstat-lbl">Online</div><div class="mbcstat-val" style="color:var(--green)">${b.up}</div></div>
          ${b.down ? `<div class="mbcstat"><div class="mbcstat-lbl">Down</div><div class="mbcstat-val" style="color:var(--red)">${b.down}</div></div>` : ''}
        </div>
      `;
      const healthHtml = `
        <div class="mhealthbar">
          <div style="width:${upP}%;background:var(--green)"></div>
          <div style="width:${wP}%;background:var(--yellow)"></div>
          <div style="width:${dP}%;background:var(--red)"></div>
        </div>
      `;
      return `
        <div class="mbcard ${status}" data-b="${esc(b.name)}" data-display="${esc(display)}">
          <div class="mbcard-top">
            <div>
              <div class="mbcard-name">${esc(display)}</div>
              <div class="mbcard-sub">${esc(b.name)} &middot; ${b.total} device${b.total === 1 ? '' : 's'}</div>
            </div>
            <div class="mbadge ${badgeClass}">${badgeText}</div>
          </div>
          ${statsHtml}
          ${healthHtml}
        </div>
      `;
    }).join('');

    list.innerHTML = header + cards;
    list.querySelectorAll('.mbcard').forEach(card => {
      card.addEventListener('click', () => {
        M.building = card.dataset.b;
        M.buildingName = card.dataset.display;
        showScreen('switches');
        renderSwitches();
      });
    });
  }

  // ── Switches screen ────────────────────────────────────────────
  //
  // Groups switches by "closet" — the second segment of the
  // sysname, which for this district follows
  //   <building>-<closet>-<model>[-suffix]
  // e.g. pes-mc-2930 → closet "MC" (main closet)
  //      phs-tr3b-2530-4 → closet "TR3B"
  //      ac-fieldhouse-2920 → closet "FIELDHOUSE"
  // If a sysname doesn't fit the pattern we fall back to the
  // hostname's second segment, then to "OTHER". Groups stay
  // collapsible individually — the main closet ("MC") auto-opens
  // since it's the most common entry point, everything else
  // starts collapsed so a PHS view doesn't dump 50 rows at once.
  function parseCloset(sysname, hostname) {
    const src = (sysname || hostname || '').trim().toLowerCase();
    if (!src) return 'OTHER';
    const parts = src.split('-');
    if (parts.length < 2) return 'OTHER';
    return parts[1].toUpperCase();
  }

  function prettyCloset(code) {
    // Light labels for the common cases. Everything else stays
    // raw (uppercased) so the field tech can match the label on
    // the closet door.
    if (code === 'MC') return 'Main Closet';
    if (/^HC\d+[A-Z]?$/.test(code)) return `Hall Closet ${code.slice(2)}`;
    if (/^TR\d+[A-Z]?$/.test(code)) return `Telco Room ${code.slice(2)}`;
    if (/^TC\d+[A-Z]?$/.test(code)) return `Tech Closet ${code.slice(2)}`;
    // Fallback — title case the raw segment ("fieldhouse" → "Fieldhouse")
    return code.charAt(0).toUpperCase() + code.slice(1).toLowerCase();
  }

  // Persist open/closed state across renders within a session
  const closetOpen = {};

  function renderSwitches() {
    const list = document.getElementById('mswitches-list');
    const devices = (M.allDevices || []).filter(d => d.building === M.building);

    if (!devices.length) {
      list.innerHTML = '<div class="mempty">No switches in this building</div>';
      return;
    }

    // Group by closet
    const groups = {};
    for (const d of devices) {
      const key = parseCloset(d.sysname, d.hostname);
      if (!groups[key]) groups[key] = [];
      groups[key].push(d);
    }

    // Sort group keys — MC first, then alpha
    const keys = Object.keys(groups).sort((a, b) => {
      if (a === 'MC') return -1;
      if (b === 'MC') return 1;
      return a.localeCompare(b);
    });

    // Auto-open MC (main closet) and single-group buildings
    if (keys.length === 1) closetOpen[keys[0]] = true;
    else if (closetOpen['MC'] === undefined) closetOpen['MC'] = true;

    const header = `
      <div class="msec">
        <div class="msec-lbl">${esc(M.buildingName || M.building)}</div>
        <div class="msec-line"></div>
        <div class="msec-ct">${keys.length} closet${keys.length === 1 ? '' : 's'} · ${devices.length} switch${devices.length === 1 ? '' : 'es'}</div>
      </div>
    `;

    const groupsHtml = keys.map(key => {
      const gDevices = groups[key].sort(
        (a, b) => (a.ip || '').localeCompare(b.ip || '', undefined, { numeric: true })
      );
      const up = gDevices.filter(d => d.status === 1).length;
      const total = gDevices.length;
      const downCount = total - up;
      const dotColor = downCount > 0 ? 'var(--red)' : 'var(--green)';
      const isOpen = !!closetOpen[key];

      const rowsHtml = gDevices.map(d => {
        const dot = d.status === 1 ? 'up' : 'down';
        const ipLabel = `${d.ip || ''}${d.hardware ? ' · ' + d.hardware : ''}`;
        return `
          <div class="mswrow" data-id="${d.device_id}" data-name="${esc(d.hostname || d.sysname || '')}" data-ip="${esc(d.ip || '')}" data-hw="${esc(d.hardware || '')}">
            <div class="mswrow-dot ${dot}"></div>
            <div class="mswrow-info">
              <div class="mswrow-name">${esc(d.sysname || d.hostname || '(unknown)')}</div>
              <div class="mswrow-ip">${esc(ipLabel)}</div>
            </div>
            <div class="mswrow-ports">
              <div class="mswrow-ports-up">${up}</div>
              <div class="mswrow-ports-tot">/ ${total}</div>
            </div>
            <div class="mswrow-arr">&rsaquo;</div>
          </div>
        `;
      }).join('');

      return `
        <div class="mcloset-group">
          <div class="mcloset-hdr${isOpen ? ' open' : ''}" data-key="${esc(key)}">
            <div class="mcloset-icon">&#x1f5c4;&#xfe0f;</div>
            <div class="mcloset-name">${esc(prettyCloset(key))}</div>
            <div class="mcloset-dot" style="background:${dotColor}"></div>
            <div class="mcloset-meta">${up}/${total}</div>
            <span class="mcloset-chev">&rsaquo;</span>
          </div>
          <div class="mcloset-body${isOpen ? '' : ' closed'}">
            ${rowsHtml}
          </div>
        </div>
      `;
    }).join('');

    list.innerHTML = header + groupsHtml;

    list.querySelectorAll('.mcloset-hdr').forEach(hdr => {
      hdr.addEventListener('click', () => {
        const key = hdr.dataset.key;
        closetOpen[key] = !closetOpen[key];
        renderSwitches();
      });
    });
    list.querySelectorAll('.mswrow').forEach(row => {
      row.addEventListener('click', () => {
        openSwitch(parseInt(row.dataset.id, 10), row.dataset.name, row.dataset.ip, row.dataset.hw);
      });
    });
  }

  // ── Ports screen ───────────────────────────────────────────────
  async function openSwitch(deviceId, name, ip, hardware) {
    M.switchId = deviceId;
    M.switchName = name;
    M.switchIp = ip;
    M.switchHardware = hardware || '';
    M.selectedPort = null;
    M.beacon = false;
    showScreen('ports');
    const body = document.getElementById('mports-body');
    body.innerHTML = '<div class="mempty">Loading ports…</div>';
    try {
      const [portData, notes] = await Promise.all([
        fetchPorts(deviceId),
        fetchPortNotes(deviceId),
      ]);
      M.ports = portData.ports || [];
      M.portNotes = notes || {};
      renderPortScreen();
    } catch (e) {
      body.innerHTML = '<div class="mempty">Failed to load ports</div>';
    }
  }

  function renderPortScreen() {
    const body = document.getElementById('mports-body');
    const up = M.ports.filter(p => p.oper_status === 'up').length;
    const down = M.ports.length - up;
    body.innerHTML = `
      <div class="msec">
        <div class="msec-lbl">Ports</div>
        <div class="msec-line"></div>
        <div class="msec-ct">
          <span style="color:var(--green)">${up}&uarr;</span>
          &nbsp;<span style="color:var(--red)">${down}&darr;</span>
        </div>
      </div>
      <div class="mport-grid" id="mport-grid"></div>
      <div id="mpdet-wrap"></div>
    `;
    renderPortGrid();
  }

  function renderPortGrid() {
    const grid = document.getElementById('mport-grid');
    if (!grid) return;
    grid.innerHTML = M.ports.map((p, i) => {
      let cls = '';
      if (p.ifName === M.selectedPort) cls = 'sel';
      else if (p.oper_status === 'up') cls = 'up';
      else if (p.admin_status === 'down') cls = '';
      else cls = '';
      if (p.recently_down) cls += ' recently-down';
      const num = (p.ifName || '').replace(/^.*?(\d+\/?\d*\/?\d*)$/, '$1');
      const spd = p.oper_status === 'up' ? (p.speed_str || '') : '—';
      return `
        <div class="mport ${cls}" data-i="${i}">
          <div class="mport-num">${esc(num)}</div>
          <div class="mport-spd">${esc(spd)}</div>
        </div>
      `;
    }).join('');
    grid.querySelectorAll('.mport').forEach(el => {
      el.addEventListener('click', () => selectPort(parseInt(el.dataset.i, 10)));
    });
  }

  async function selectPort(idx) {
    const p = M.ports[idx];
    if (!p) return;
    M.selectedPort = p.ifName;
    renderPortGrid();
    const wrap = document.getElementById('mpdet-wrap');
    wrap.innerHTML = '<div class="mempty">Loading VLAN assignment…</div>';
    // Fetch VLAN state + global lists in parallel
    const [vlanInfo] = await Promise.all([
      fetchPortVlans(M.switchId, p.ifName),
      fetchAllVlans(),
      fetchVlanTemplates(),
    ]);
    M.portVlans = vlanInfo;
    renderPortDetail(p);
  }

  function renderPortDetail(p) {
    const wrap = document.getElementById('mpdet-wrap');
    const isUp = p.oper_status === 'up';
    const isAdminDown = p.admin_status === 'down';
    const statusText = isUp
      ? `&#9679; LINKED · ${esc(p.speed_str || '')}`
      : isAdminDown ? '&#9898; ADMIN DOWN' : '&#9898; NO LINK';
    const statusColor = isUp ? 'var(--green)' : isAdminDown ? 'var(--yellow)' : 'var(--text-muted)';

    const note = M.portNotes[p.ifName];
    const portNum = (p.ifName || '').replace(/^.*?(\d+\/?\d*\/?\d*)$/, '$1');

    // Build VLAN rows from portVlans. The endpoint returns:
    //   { untagged: [ids], tagged: [ids],
    //     vlans: [{vlan_id, name, mode}, ...] }
    // We use ``vlans`` as the source of truth — it's the
    // authoritative per-port list with names straight from the
    // switch CLI (not LibreNMS's global view), and it avoids the
    // array-vs-scalar mismatch we had when treating ``untagged``
    // as a single number.
    //
    // The ``mode`` string from _parse_port_vlans is lowercased —
    // "tagged", "untagged", etc. Match on a prefix so minor
    // vendor variations ("tag"/"untag") still classify correctly.
    let vlanRowsHtml = '';
    if (M.portVlans) {
      const rawRows = M.portVlans.vlans || [];
      // Normalise and deduplicate by vlan_id (HP sometimes lists
      // the same VLAN twice if it spans multiple modes).
      const seen = new Set();
      const rows = [];
      for (const raw of rawRows) {
        const id = raw.vlan_id;
        if (id == null || seen.has(id)) continue;
        seen.add(id);
        const modeRaw = (raw.mode || '').toLowerCase();
        const isUntag = modeRaw.startsWith('untag');
        // Fall back to the global VLAN list if the per-port parse
        // didn't capture a name (rare, but happens with custom
        // vendor output formats).
        const name = raw.name
          || M.allVlans.find(v => v.vlan_id === id)?.name
          || '';
        rows.push({
          id,
          name,
          mode: isUntag ? 'untag' : 'tag',
          canRemove: !isUntag, // only tagged VLANs are removable here
        });
      }
      // Sort: untagged first, then tagged by id
      rows.sort((a, b) => {
        if (a.mode !== b.mode) return a.mode === 'untag' ? -1 : 1;
        return a.id - b.id;
      });
      vlanRowsHtml = rows.map(r => `
        <div class="mvrow">
          <div class="mvrow-id">v${r.id}</div>
          <div class="mvrow-name">${esc(r.name || '(unnamed)')}</div>
          <div class="mvrow-mode">
            <button class="mvmb ${r.mode === 'tag' ? 'active' : ''}" disabled>TAG</button>
            <button class="mvmb ${r.mode === 'untag' ? 'active' : ''}" disabled>UNTAG</button>
          </div>
          ${r.canRemove
            ? `<button class="mvrem" data-rem="${r.id}" title="Remove tag">&times;</button>`
            : `<div style="width:24px"></div>`}
        </div>
      `).join('');
      if (!rows.length) vlanRowsHtml = '<div style="padding:12px;font-size:12px;color:var(--text-muted)">No VLANs assigned</div>';
    } else {
      vlanRowsHtml = '<div style="padding:12px;font-size:12px;color:var(--text-muted)">VLAN info unavailable</div>';
    }

    wrap.innerHTML = `
      <div class="msec">
        <div class="msec-lbl">Port ${esc(portNum)}</div>
        <div class="msec-line"></div>
      </div>
      <div class="mpdet">
        <div class="mpdet-hdr">
          <div class="mpdet-num">${esc(portNum)}</div>
          <div class="mpdet-info">
            <div class="mpdet-name">${esc(p.ifAlias || p.ifName || 'Port')}</div>
            <div class="mpdet-status" style="color:${statusColor}">${statusText}</div>
          </div>
        </div>
        <div class="mpdet-body">
          <div>
            <div class="mflbl">Description (ifAlias)</div>
            <input class="mfinput" id="mp-desc" placeholder="e.g. Room 204 Teacher Desk" value="${esc(p.ifAlias || '')}" maxlength="64">
          </div>
          <div class="mvbox">
            <div class="mvbox-hdr">
              <div class="mvbox-title">VLANs</div>
              <button class="mvadd" id="mp-vlan-add" title="Add tagged VLAN">+</button>
            </div>
            <div id="mp-vlan-list">${vlanRowsHtml}</div>
          </div>
          <div>
            <div class="mflbl">Apply VLAN Template</div>
            <button class="mabtn" id="mp-tpl-btn" ${M.vlanTemplates.length === 0 ? 'disabled' : ''}>
              ${M.vlanTemplates.length === 0
                ? 'No templates defined'
                : `&#x2630;&nbsp; Pick template (${M.vlanTemplates.length})`}
            </button>
          </div>
          <div>
            <div class="mflbl">Port note (local annotation)</div>
            <input class="mfinput" id="mp-note" placeholder="Optional — stored locally, not pushed to switch" value="${esc(note?.note || '')}" maxlength="200">
          </div>
          <div>
            <div class="mflbl">Port Controls</div>
            <div class="marow">
              ${isAdminDown
                ? `<button class="mabtn" id="mp-port-toggle" style="color:var(--success);border-color:var(--success)">&#x25B6; Enable Port</button>`
                : `<button class="mabtn" id="mp-port-toggle" style="color:var(--danger);border-color:var(--danger)">&#x23F9; Disable Port</button>`}
              ${/poe/i.test(M.switchHardware || '')
                ? `<button class="mabtn" id="mp-poe-cycle">&#x26A1; PoE Cycle</button>`
                : ''}
            </div>
          </div>
          <div class="marow">
            <button class="mabtn" id="mp-cancel">Cancel</button>
            <button class="mabtn pri" id="mp-apply">&#10003; Apply</button>
          </div>
        </div>
      </div>
    `;

    // Scroll the new detail into view
    wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    document.getElementById('mp-vlan-add').addEventListener('click', openVlanSheet);
    const tplBtn = document.getElementById('mp-tpl-btn');
    if (tplBtn && M.vlanTemplates.length > 0) {
      tplBtn.addEventListener('click', () => openTemplateSheet(p));
    }
    document.getElementById('mp-port-toggle').addEventListener('click', () => {
      const enabling = p.admin_status === 'down';
      portStateAction(p, enabling);
    });
    const poeBtn = document.getElementById('mp-poe-cycle');
    if (poeBtn) {
      poeBtn.addEventListener('click', async () => {
        if (!confirm(`Cycle PoE on ${p.ifName}? This will briefly disconnect the device.`)) return;
        toast('Cycling PoE...', 'info');
        try {
          const r = await fetch('/api/network/switch/poe', {
            method: 'POST', headers: HP,
            body: JSON.stringify({ device_id: M.switchId, port_id: p.ifName, action: 'cycle' }),
          });
          if (!r.ok) throw new Error((await r.json()).detail || 'Failed');
          toast('PoE cycled', 'ok');
        } catch (e) { toast('PoE cycle failed: ' + e.message, 'err'); }
      });
    }
    document.getElementById('mp-cancel').addEventListener('click', () => {
      M.selectedPort = null;
      renderPortGrid();
      wrap.innerHTML = '';
    });
    document.getElementById('mp-apply').addEventListener('click', () => applyPortChanges(p));
    wrap.querySelectorAll('.mvrem').forEach(btn => {
      btn.addEventListener('click', () => removeTag(parseInt(btn.dataset.rem, 10)));
    });
  }

  async function portStateAction(p, enabled) {
    const verb = enabled ? 'enable' : 'disable';
    if (!enabled && !confirm(`Shut down port ${p.ifName}? This will disconnect any device plugged into it.`)) return;
    toast(enabled ? 'Enabling port...' : 'Disabling port...', 'info');
    try {
      const r = await fetch('/api/network/switch/port-state', {
        method: 'POST', headers: HP,
        body: JSON.stringify({ device_id: M.switchId, port_id: p.ifName, enabled }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Failed');
      // Flip cached state so the toggle re-renders
      p.admin_status = enabled ? 'up' : 'down';
      renderPortGrid();
      renderPortDetail(p);
      toast(`Port ${verb}d`, 'ok');
    } catch (e) {
      toast(`Port ${verb} failed: ${e.message}`, 'err');
    }
  }

  async function applyPortChanges(p) {
    const desc = document.getElementById('mp-desc').value.trim();
    const noteInput = document.getElementById('mp-note').value.trim();
    const noteChanged = noteInput !== (M.portNotes[p.ifName]?.note || '');
    const descChanged = desc !== (p.ifAlias || '');

    if (!descChanged && !noteChanged) {
      toast('No changes', 'warn');
      return;
    }

    let anyFail = false;

    if (descChanged) {
      try {
        const r = await fetch('/api/network/switch/rename', {
          method: 'POST',
          headers: HP,
          body: JSON.stringify({ device_id: M.switchId, port_id: p.ifName, description: desc }),
        });
        if (!r.ok) throw new Error('rename failed');
        p.ifAlias = desc;
      } catch (e) {
        toast('Description save failed', 'err');
        anyFail = true;
      }
    }

    if (noteChanged) {
      try {
        const r = await fetch('/api/network/port-notes', {
          method: 'POST',
          headers: HP,
          body: JSON.stringify({ device_id: M.switchId, port_name: p.ifName, note: noteInput }),
        });
        if (!r.ok) throw new Error('note failed');
        M.portNotes[p.ifName] = { note: noteInput, updated_at: new Date().toISOString() };
      } catch (e) {
        toast('Note save failed', 'err');
        anyFail = true;
      }
    }

    if (!anyFail) toast('Port updated', 'ok');
    renderPortGrid();
    renderPortDetail(p);
  }

  async function removeTag(vlanId) {
    if (!M.portVlans) return;
    const p = M.ports.find(x => x.ifName === M.selectedPort);
    if (!p) return;
    const newTagged = (M.portVlans.tagged || []).filter(v => v !== vlanId);
    try {
      const r = await fetch('/api/network/switch/vlan-set', {
        method: 'POST',
        headers: HP,
        body: JSON.stringify({
          device_id: M.switchId,
          port_id: p.ifName,
          tagged_vlans: newTagged,
        }),
      });
      if (!r.ok) throw new Error('vlan set');
      toast(`VLAN ${vlanId} removed`, 'ok');
      // Re-fetch the authoritative port VLAN state from the
      // switch so the rendered list matches actual hardware.
      M.portVlans = await fetchPortVlans(M.switchId, p.ifName);
      renderPortDetail(p);
    } catch (e) {
      toast('VLAN remove failed', 'err');
    }
  }

  // ── VLAN picker sheet ──────────────────────────────────────────
  function openVlanSheet() {
    if (!M.portVlans) return;
    // Both untagged and tagged are lists in the endpoint response.
    // Collect every ID already assigned so the picker only offers
    // VLANs that aren't on the port yet.
    const assigned = new Set([
      ...(M.portVlans.untagged || []),
      ...(M.portVlans.tagged || []),
    ]);
    const available = M.allVlans.filter(v => !assigned.has(v.vlan_id));
    if (!available.length) {
      toast('All VLANs already assigned', 'warn');
      return;
    }
    const list = document.getElementById('mvlan-list');
    list.innerHTML = available.map(v => `
      <div class="msheet-item" data-id="${v.vlan_id}" data-name="${esc(v.name || '')}">
        <div class="msheet-item-id">v${v.vlan_id}</div>
        <div class="msheet-item-name">${esc(v.name || '(unnamed)')}</div>
      </div>
    `).join('');
    document.getElementById('mvlan-sheet').classList.add('open');
    list.querySelectorAll('.msheet-item').forEach(item => {
      item.addEventListener('click', () => addTag(parseInt(item.dataset.id, 10)));
    });
  }

  function mNetVlanSheetClose() {
    document.getElementById('mvlan-sheet').classList.remove('open');
  }

  // ── VLAN template picker (reuses the VLAN sheet DOM with
  // different content + title) ──────────────────────────────────
  function openTemplateSheet(port) {
    if (!M.vlanTemplates.length) {
      toast('No VLAN templates defined', 'warn');
      return;
    }
    const sheet = document.getElementById('mvlan-sheet');
    const title = sheet.querySelector('.msheet-title');
    const list = document.getElementById('mvlan-list');
    title.textContent = 'Apply VLAN Template';

    list.innerHTML = M.vlanTemplates.map(tpl => {
      const untag = tpl.untagged_vlan ? `U:${tpl.untagged_vlan}` : '';
      const tag = (tpl.tagged_vlans || []).length
        ? `T:${tpl.tagged_vlans.join(',')}`
        : '';
      const preview = [untag, tag].filter(Boolean).join(' · ') || '(empty)';
      const desc = tpl.description ? ` — ${esc(tpl.description)}` : '';
      return `
        <div class="msheet-item msheet-tpl" data-id="${tpl.id}" data-name="${esc(tpl.name)}">
          <div style="flex:1;min-width:0">
            <div class="msheet-item-name">${esc(tpl.name)}</div>
            <div style="font-family:ui-monospace,monospace;font-size:10px;color:var(--text-muted);margin-top:2px">
              ${esc(preview)}${desc}
            </div>
          </div>
        </div>
      `;
    }).join('');
    sheet.classList.add('open');
    list.querySelectorAll('.msheet-tpl').forEach(item => {
      item.addEventListener('click', () => {
        const id = parseInt(item.dataset.id, 10);
        const name = item.dataset.name;
        mNetVlanSheetClose();
        // Restore the sheet title so the next VLAN-add open isn't
        // stuck showing the template heading.
        title.textContent = 'Add Tagged VLAN';
        applyVlanTemplate(port, id, name);
      });
    });
  }

  async function applyVlanTemplate(port, templateId, templateName) {
    if (!confirm(`Apply template "${templateName}" to ${port.ifName}?`)) {
      return;
    }
    toast(`Applying ${templateName}…`, 'info');
    try {
      const r = await fetch('/api/network/switch/vlan-apply', {
        method: 'POST',
        headers: HP,
        body: JSON.stringify({
          device_id: M.switchId,
          port_id: port.ifName,
          template_id: templateId,
        }),
      });
      if (!r.ok) {
        let detail = 'Template apply failed';
        try { detail = (await r.json()).detail || detail; } catch {}
        throw new Error(detail);
      }
      toast(`Template "${templateName}" applied`, 'ok');
      // Refresh the port's VLAN info so the UI reflects the new
      // untagged + tagged state.
      M.portVlans = await fetchPortVlans(M.switchId, port.ifName);
      renderPortDetail(port);
    } catch (e) {
      toast(e.message || 'Template apply failed', 'err');
    }
  }

  async function addTag(vlanId) {
    const p = M.ports.find(x => x.ifName === M.selectedPort);
    if (!p || !M.portVlans) return;
    const newTagged = [...(M.portVlans.tagged || []), vlanId].sort((a, b) => a - b);
    try {
      const r = await fetch('/api/network/switch/vlan-set', {
        method: 'POST',
        headers: HP,
        body: JSON.stringify({
          device_id: M.switchId,
          port_id: p.ifName,
          tagged_vlans: newTagged,
        }),
      });
      if (!r.ok) throw new Error('vlan set');
      toast(`VLAN ${vlanId} added`, 'ok');
      mNetVlanSheetClose();
      // Re-fetch the authoritative port VLAN state so the list
      // shows the new entry with its name from the switch.
      M.portVlans = await fetchPortVlans(M.switchId, p.ifName);
      renderPortDetail(p);
    } catch (e) {
      toast('VLAN add failed', 'err');
      mNetVlanSheetClose();
    }
  }

  // ── Beacon (locator LED) ──────────────────────────────────────
  async function mNetBeaconToggle() {
    if (!M.switchId) return;
    const want = !M.beacon;
    try {
      const r = await fetch('/api/network/switch/locator', {
        method: 'POST',
        headers: HP,
        body: JSON.stringify({ device_id: M.switchId, active: want }),
      });
      if (!r.ok) throw new Error('locator');
      M.beacon = want;
      updTopbar();
      toast(`Locator ${want ? 'ON' : 'OFF'}`, want ? 'ok' : 'warn');
    } catch (e) {
      toast('Locator toggle failed', 'err');
    }
  }

  // ── Global search ──────────────────────────────────────────────
  function mNetSearchInput(value) {
    clearTimeout(M.searchTimer);
    const q = (value || '').trim();
    M.searchQuery = q;
    document.getElementById('mssearch-clear').classList.toggle('on', q.length > 0);
    if (q.length < 2) {
      // Return to whichever screen we were on
      if (M.screen === 'search') mNetBack();
      return;
    }
    M.searchTimer = setTimeout(() => doSearch(q), 240);
  }

  function mNetSearchClear() {
    document.getElementById('mssearch-input').value = '';
    document.getElementById('mssearch-clear').classList.remove('on');
    M.searchQuery = '';
    if (M.screen === 'search') mNetBack();
  }

  async function doSearch(q) {
    showScreen('search');
    const out = document.getElementById('msearch-results');
    out.innerHTML = '<div class="mempty">Searching…</div>';
    try {
      const r = await fetch('/api/network/search?q=' + encodeURIComponent(q), { headers: H });
      if (!r.ok) throw new Error('search');
      const data = await r.json();
      renderSearchResults(data);
    } catch (e) {
      out.innerHTML = '<div class="mempty">Search failed</div>';
    }
  }

  function renderSearchResults(data) {
    const out = document.getElementById('msearch-results');
    const groups = [
      { key: 'devices', label: 'Devices', items: data.devices || [] },
      { key: 'ports', label: 'Ports', items: data.ports || [] },
      { key: 'vlans', label: 'VLANs', items: data.vlans || [] },
      { key: 'notes', label: 'Port Notes', items: data.notes || [] },
    ];
    const total = groups.reduce((n, g) => n + g.items.length, 0);
    if (!total) {
      out.innerHTML = '<div class="mempty">No matches</div>';
      return;
    }
    let html = '';
    for (const g of groups) {
      if (!g.items.length) continue;
      html += `
        <div class="msec">
          <div class="msec-lbl">${g.label}</div>
          <div class="msec-line"></div>
          <div class="msec-ct">${g.items.length}</div>
        </div>
      `;
      html += g.items.map((item, i) => {
        const idx = `${g.key}-${i}`;
        return renderSearchItem(item, idx);
      }).join('');
    }
    out.innerHTML = html;
    out.querySelectorAll('.msrr').forEach(row => {
      row.addEventListener('click', () => activateSearchItem(row.dataset));
    });
  }

  function renderSearchItem(item, idx) {
    if (item.type === 'device') {
      const sub = [item.ip, item.hardware, item.building].filter(Boolean).join(' · ');
      return `
        <div class="msrr" data-kind="device" data-id="${item.device_id}" data-name="${esc(item.name)}" data-ip="${esc(item.ip || '')}" data-building="${esc(item.building || '')}">
          <div class="msrr-chip">SW</div>
          <div class="msrr-info">
            <div class="msrr-name">${esc(item.name)}</div>
            <div class="msrr-sub">${esc(sub)}</div>
          </div>
        </div>
      `;
    }
    if (item.type === 'port') {
      const sub = [item.device_name, item.device_ip].filter(Boolean).join(' · ');
      const alias = item.ifAlias ? ` — ${esc(item.ifAlias)}` : '';
      return `
        <div class="msrr" data-kind="port" data-id="${item.device_id}" data-name="${esc(item.device_name || '')}" data-ip="${esc(item.device_ip || '')}" data-port="${esc(item.ifName)}">
          <div class="msrr-chip">PT</div>
          <div class="msrr-info">
            <div class="msrr-name">${esc(item.ifName)}${alias}</div>
            <div class="msrr-sub">${esc(sub)}</div>
          </div>
        </div>
      `;
    }
    if (item.type === 'vlan') {
      const sub = [item.subnet, item.gateway_ip, item.group_name].filter(Boolean).join(' · ');
      return `
        <div class="msrr" data-kind="vlan">
          <div class="msrr-chip">V${esc(item.vlan_id)}</div>
          <div class="msrr-info">
            <div class="msrr-name">${esc(item.name || '(unnamed)')}</div>
            <div class="msrr-sub">${esc(sub)}</div>
          </div>
        </div>
      `;
    }
    if (item.type === 'note') {
      const sub = [item.device_name, item.port_name, item.device_ip].filter(Boolean).join(' · ');
      return `
        <div class="msrr" data-kind="note" data-id="${item.device_id}" data-name="${esc(item.device_name || '')}" data-ip="${esc(item.device_ip || '')}" data-port="${esc(item.port_name)}">
          <div class="msrr-chip">N</div>
          <div class="msrr-info">
            <div class="msrr-name">${esc(item.note)}</div>
            <div class="msrr-sub">${esc(sub)}</div>
          </div>
        </div>
      `;
    }
    return '';
  }

  async function activateSearchItem(ds) {
    // Clear the search input but keep us navigating to the target
    document.getElementById('mssearch-input').value = '';
    document.getElementById('mssearch-clear').classList.remove('on');
    M.searchQuery = '';
    if (ds.kind === 'device') {
      M.building = ds.building || null;
      M.buildingName = ds.building || null;
      await fetchDevices();
      openSwitch(parseInt(ds.id, 10), ds.name, ds.ip);
      return;
    }
    if (ds.kind === 'port' || ds.kind === 'note') {
      await fetchDevices();
      // Find the device in the cache so we can set building state
      const dev = (M.allDevices || []).find(d => d.device_id === parseInt(ds.id, 10));
      if (dev) {
        M.building = dev.building || null;
        M.buildingName = dev.building || null;
      }
      await openSwitch(parseInt(ds.id, 10), ds.name, ds.ip);
      // Try to auto-select the target port once ports load
      const targetPort = ds.port;
      let tries = 0;
      const picker = setInterval(() => {
        tries++;
        if (M.ports.length) {
          const idx = M.ports.findIndex(p => p.ifName === targetPort);
          if (idx >= 0) selectPort(idx);
          clearInterval(picker);
        } else if (tries > 30) {
          clearInterval(picker);
        }
      }, 120);
      return;
    }
    if (ds.kind === 'vlan') {
      toast('VLAN detail not available on mobile yet', 'warn');
      // Return to wherever we were
      mNetBack();
    }
  }

  // ── QR scanner ─────────────────────────────────────────────────
  async function mNetQrOpen() {
    const overlay = document.getElementById('mqr-overlay');
    const video = document.getElementById('mqr-video');
    const status = document.getElementById('mqr-status');
    overlay.classList.add('open');
    status.textContent = 'Starting camera…';

    // Lazy-load ZXing
    try {
      await loadZxing();
    } catch (e) {
      status.textContent = 'Scanner library failed to load';
      return;
    }

    try {
      const hints = new Map();
      const { BrowserQRCodeReader } = ZXingBrowser;
      M.scanner = new BrowserQRCodeReader();
      const devices = await BrowserQRCodeReader.listVideoInputDevices();
      // Prefer the back-facing camera if we can identify it
      const rear = devices.find(d => /back|rear|environment/i.test(d.label)) || devices[devices.length - 1];
      const deviceId = rear ? rear.deviceId : undefined;
      status.textContent = 'Point at the QR label';
      M.scanner.decodeFromVideoDevice(deviceId, video, (result, err, controls) => {
        if (result) {
          controls.stop();
          handleQrResult(result.getText());
        }
      }).then(ctrl => { M.scannerStream = ctrl; });
    } catch (e) {
      status.textContent = 'Camera access denied';
    }
  }

  function mNetQrClose() {
    const overlay = document.getElementById('mqr-overlay');
    overlay.classList.remove('open');
    if (M.scannerStream && typeof M.scannerStream.stop === 'function') {
      try { M.scannerStream.stop(); } catch {}
    }
    M.scannerStream = null;
  }

  async function loadZxing() {
    if (window.ZXingBrowser) return;
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/static/vendor/zxing-browser.min.js';
      script.onload = () => resolve();
      script.onerror = () => reject(new Error('zxing load'));
      document.head.appendChild(script);
    });
  }

  function extractDeviceIdFromText(text) {
    if (!text) return null;
    // Accepted formats:
    //   https://app.trojan-nexus.com/network?switch=123   (our QR PNG)
    //   https://app.trojan-nexus.com/network/switch/123   (legacy / manual)
    //   nexus://switch/123                                (custom scheme)
    //   123                                                (bare id)
    try {
      let m = text.match(/[?&]switch=(\d+)/);
      if (m) return parseInt(m[1], 10);
      m = text.match(/switch\/(\d+)/);
      if (m) return parseInt(m[1], 10);
      if (/^\d+$/.test(text.trim())) return parseInt(text.trim(), 10);
    } catch {}
    return null;
  }

  function handleQrResult(text) {
    mNetQrClose();
    const deviceId = extractDeviceIdFromText(text);
    if (!deviceId) {
      toast('QR unrecognized', 'err');
      return;
    }
    openDeviceById(deviceId);
  }

  async function openDeviceById(deviceId) {
    const devs = await fetchDevices();
    const d = devs.find(x => x.device_id === deviceId);
    if (!d) {
      toast(`Device ${deviceId} not in cache`, 'err');
      return;
    }
    M.building = d.building;
    M.buildingName = d.building;
    openSwitch(deviceId, d.hostname || d.sysname || '', d.ip || '');
  }

  // ── Init ───────────────────────────────────────────────────────
  async function init() {
    if (M.initialized) return;
    M.initialized = true;
    // QR deep-link support: if the URL has ?switch=<id>, pre-load
    // the device cache and jump straight to that switch.
    let deepSwitchId = null;
    try {
      const params = new URLSearchParams(window.location.search);
      const raw = params.get('switch');
      if (raw && /^\d+$/.test(raw)) deepSwitchId = parseInt(raw, 10);
    } catch {}
    if (deepSwitchId != null) {
      await fetchDevices();
      await openDeviceById(deepSwitchId);
      return;
    }
    renderBuildings();
    updTopbar();
  }

  // Wait for DOM ready then init. The mobile shell lives inside
  // network.html so by the time this script loads we're already
  // at end-of-body — run immediately.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
