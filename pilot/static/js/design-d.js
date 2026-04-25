/* ==========================================================================
   rcpilot — Design D layout patch
   Replaces the card + action-row layout with:
     - Active sessions strip (top)
     - Collapsed repo panes (one-line: dot · name · branch · sha · tokens · ⚡)
     - Bottom action sheet (primary CTA + grid + PR input + overflow menu)

   USAGE:
     1. Copy to pilot/static/js/design-d.js
     2. In index.html, near end of <body> (after existing inline script
        closes on line ~2702), add:
            <script src="/static/js/design-d.js"></script>
     3. Restart:  fish restart.fish

   This file is ADDITIVE. It monkey-patches buildCard + refreshStatus and
   injects new DOM/CSS on DOMContentLoaded. Remove the script tag to revert.
   ========================================================================== */
(() => {
  'use strict';

  /* ── Injected styles ───────────────────────────────────────────────── */
  const css = `
  /* Hide the old per-card layout; Design D owns it now */
  .project-card .action-row,
  .project-card .git-actions,
  .project-card .history-header,
  .project-card .history-toggle,
  .project-card .btn-clear-history,
  .project-card .git-diff-stat,
  .project-card .git-synced,
  .project-card .running-sessions,
  .project-card > .status-badge { display: none !important; }

  .project-card .session-history { display: none !important; }

  /* Force single-column project list so panes span full width */
  #project-list {
    columns: 1 !important;
    column-count: 1 !important;
    column-gap: 0 !important;
    display: block !important;
  }

  /* Repo pane — single-line */
  .project-card {
    padding: 0 !important;
    background: var(--surface) !important;
    overflow: hidden;
    display: flex;
    align-items: stretch;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .project-card .project-header {
    flex: 1 1 auto;
    min-width: 0;
    display: flex !important;
    flex-direction: column !important;
    align-items: stretch !important;
    gap: .22rem !important;
    padding: .55rem .8rem !important;
    margin: 0 !important;
    cursor: pointer;
  }
  .project-card .project-header:active { background: color-mix(in srgb, var(--text) 4%, transparent); }
  .pane-row1 { display: flex; align-items: center; gap: .5rem; min-width: 0; }
  .pane-row2 { display: flex; align-items: center; gap: .45rem; min-width: 0; font-size: .68rem; color: var(--muted); padding-left: 16px; flex-wrap: wrap; }
  .project-card .project-name {
    flex: 1 1 auto;
    min-width: 0;
    display: block !important;
    font-weight: 600;
    font-size: .95rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    padding: 0 !important;
    margin: 0 !important;
  }
  .pane-row2 .git-badge {
    background: var(--purple-tint);
    color: var(--purple);
    font-size: .64rem;
    padding: 0 .38rem;
    border-radius: 4px;
    font-family: inherit;
    flex-shrink: 0;
  }
  .pane-row2 .pane-sha { flex-shrink: 0; }
  .pane-row2 .pane-age { opacity: .75; flex-shrink: 0; }
  .pane-row2 .pane-tok { margin-left: auto; display: inline-flex; gap: .4rem; flex-shrink: 0; }

  .pane-dot {
    width: 8px; height: 8px; border-radius: 4px;
    background: var(--muted);
    flex-shrink: 0;
  }
  .pane-dot.active { background: var(--yellow); box-shadow: 0 0 0 3px color-mix(in srgb, var(--yellow) 25%, transparent); }
  .pane-dot.dirty  { background: var(--orange); }
  .pane-dot.clean  { background: var(--green); }

  .pane-row2 .tok-orange { color: var(--orange); }
  .pane-row2 .tok-green  { color: var(--green); }
  .pane-row2 .tok-yellow { color: var(--yellow); }

  .pane-quick {
    flex-shrink: 0;
    width: 54px;
    border: none;
    border-left: 1px solid var(--border);
    background: var(--surface2);
    color: var(--green);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
  }
  .pane-quick svg { width: 20px; height: 20px; stroke: currentColor; fill: color-mix(in srgb, currentColor 25%, transparent); stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }

  /* ── Header ──────────────────────────────────────────────────────── */
  header { padding: .5rem .75rem !important; border-bottom: 1px solid var(--border); }
  header #header-row1 {
    display: flex !important; align-items: center; gap: .5rem;
    margin-bottom: .45rem !important;
  }
  header h1 {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace !important;
    font-weight: 700 !important;
    font-size: 1rem !important;
    margin: 0 !important;
    display: inline-flex !important;
    align-items: baseline;
    gap: .4rem;
    color: var(--text) !important;
  }
  header h1 .rcpilot-name {
    color: var(--text);
    letter-spacing: -.01em;
  }
  header h1 .rcpilot-name::before {
    content: '$ ';
    color: var(--green);
    font-weight: 700;
  }
  header h1 #version, header h1 small {
    color: var(--muted) !important;
    font-size: .65rem !important;
    font-weight: 500 !important;
  }
  /* Usage widget: compact pill */
  #usage-widget {
    display: inline-flex !important;
    align-items: center; gap: .35rem !important;
    font-size: .7rem;
    color: var(--muted);
    margin: 0 0 0 auto !important;
    flex: 0 0 auto !important;
    padding: 0 !important;
  }
  #usage-widget #usage-icon { color: var(--yellow); }
  #usage-widget #usage-window-label { color: var(--muted); }
  #usage-widget #usage-track { width: 36px !important; height: 3px !important; background: var(--border) !important; border-radius: 2px; }
  #usage-widget #usage-fill  { background: var(--green) !important; border-radius: 2px; height: 100%; }
  #usage-widget #usage-pct   { color: var(--yellow); font-weight: 600; }

  header #header-row2 {
    display: flex !important;
    align-items: center; gap: .5rem;
    font-size: .7rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace !important;
  }
  header .yolo-label {
    display: inline-flex !important;
    align-items: center; gap: .3rem;
    padding: .15rem .45rem;
    border-radius: 3px;
    border: 1px solid color-mix(in srgb, var(--red) 25%, transparent);
    background: var(--red-tint);
    color: var(--red);
    font-weight: 700 !important;
    font-size: .65rem !important;
    letter-spacing: .04em;
  }
  header .yolo-label input { margin: 0; accent-color: var(--red); }
  header .yolo-label::before {
    content: '';
  }
  header .btn-import, header .btn-settings {
    width: 26px; height: 26px;
    padding: 0 !important;
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    border-radius: 4px !important;
    color: var(--muted) !important;
    font-size: .75rem !important;
    display: inline-flex !important; align-items: center; justify-content: center;
  }
  header .sort-toggle {
    margin-left: auto;
    display: inline-flex !important; gap: 2px;
  }
  header .sort-label { display: none !important; }
  header .sort-btn {
    background: transparent !important;
    border: none !important;
    padding: .15rem .45rem !important;
    border-radius: 3px !important;
    font-family: inherit !important;
    font-size: .65rem !important;
    font-weight: 600 !important;
    color: var(--muted) !important;
    white-space: nowrap !important;
  }
  header .sort-btn.active {
    background: var(--cyan-tint) !important;
    color: var(--cyan) !important;
  }
  .pane-quick:active { background: color-mix(in srgb, var(--green) 15%, var(--surface2)); }

  .project-card .status-badge { display: none; }

  /* Active sessions strip */
  #active-strip {
    margin: 0 0 .65rem;
    padding: .55rem .75rem .45rem;
    background: color-mix(in srgb, var(--yellow) 7%, var(--surface));
    border: 1px solid color-mix(in srgb, var(--yellow) 30%, var(--border));
    border-radius: 12px;
    display: none;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  #active-strip.has-any { display: block; }
  #active-strip .strip-head {
    font-size: .65rem;
    color: var(--yellow);
    letter-spacing: .08em;
    text-transform: uppercase;
    margin-bottom: .35rem;
    display: flex; align-items: center; gap: .4rem;
    white-space: nowrap;
  }
  #active-strip .strip-head > span:first-of-type { flex: 0 0 auto; }
  #active-strip .strip-head::before {
    content: ''; width: 6px; height: 6px; border-radius: 3px;
    background: var(--yellow);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--yellow) 25%, transparent);
  }
  #active-strip .strip-row {
    display: flex;
    align-items: center;
    gap: .5rem;
    padding: .3rem 0;
    font-size: .78rem;
    border-top: 1px dashed color-mix(in srgb, var(--yellow) 22%, transparent);
  }
  #active-strip .strip-row:first-of-type { border-top: none; }
  #active-strip .strip-name { font-weight: 600; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1 1 auto; min-width: 0; }
  #active-strip .strip-repo { color: var(--purple); font-size: .7rem; flex-shrink: 0; }
  #active-strip .strip-attach, #active-strip .strip-rename, #active-strip .strip-kill {
    background: none; border: 1px solid var(--border);
    border-radius: 6px; padding: .15rem .5rem;
    font-size: .68rem; cursor: pointer;
    text-decoration: none; white-space: nowrap;
  }
  #active-strip .strip-attach { color: var(--green); border-color: color-mix(in srgb, var(--green) 35%, transparent); background: var(--green-tint); }
  #active-strip .strip-kill   { color: var(--red);   border-color: color-mix(in srgb, var(--red) 35%, transparent);   background: var(--red-tint); }
  #active-strip .strip-rename { color: var(--muted); }
  #active-strip .strip-rename:hover { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, transparent); }

  /* Action sheet */
  #dsheet-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: color-mix(in srgb, var(--bg) 50%, rgba(0,0,0,.55));
    z-index: 150;
    align-items: flex-end;
    justify-content: center;
  }
  #dsheet-overlay.open { display: flex; }
  #dsheet {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px 16px 0 0;
    width: 100%;
    max-width: 640px;
    max-height: 88vh;
    overflow-y: auto;
    padding: .85rem 1rem 1.25rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    animation: dsheet-up .22s ease;
  }
  @keyframes dsheet-up { from { transform: translateY(100%); } }
  #dsheet .ds-head {
    display: flex; align-items: center; gap: .45rem;
    padding-bottom: .55rem; margin-bottom: .7rem;
    border-bottom: 1px solid var(--border);
    font-size: .82rem;
  }
  #dsheet .ds-head .prompt { color: var(--green); font-weight: 700; }
  #dsheet .ds-head .name   { color: var(--text); font-weight: 600; }
  #dsheet .ds-head .branch { color: var(--purple); font-size: .72rem; }
  #dsheet .ds-head .sha    { color: var(--muted);  font-size: .72rem; }
  #dsheet .ds-head .x {
    margin-left: auto;
    background: none; border: none;
    color: var(--muted);
    font-size: 1.1rem; line-height: 1; padding: .15rem .4rem;
    cursor: pointer;
  }

  #dsheet .ds-diff {
    font-size: .72rem; padding: .2rem .55rem;
    background: var(--orange-tint); color: var(--orange);
    border: 1px solid color-mix(in srgb, var(--orange) 30%, transparent);
    border-radius: 999px;
    display: inline-block;
    margin-bottom: .8rem;
    cursor: pointer;
  }
  #dsheet .ds-diff.clean {
    background: var(--green-tint); color: var(--green);
    border-color: color-mix(in srgb, var(--green) 30%, transparent);
  }

  #dsheet .ds-active {
    display: flex; align-items: center; gap: .5rem;
    padding: .45rem .6rem;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: .8rem;
    font-size: .78rem;
  }
  #dsheet .ds-active + .ds-active { margin-top: -.5rem; }
  #dsheet .ds-active .dot { width: 6px; height: 6px; border-radius: 3px; background: var(--yellow); flex-shrink: 0; box-shadow: 0 0 0 3px color-mix(in srgb, var(--yellow) 25%, transparent); }
  #dsheet .ds-active .lbl { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #dsheet .ds-active .go, #dsheet .ds-active .rename, #dsheet .ds-active .kill {
    text-decoration: none; font-size: .7rem;
    padding: .2rem .55rem; border-radius: 6px; border: 1px solid var(--border); cursor: pointer;
    background: none;
  }
  #dsheet .ds-active .go     { color: var(--green); border-color: color-mix(in srgb, var(--green) 35%, transparent); background: var(--green-tint); }
  #dsheet .ds-active .kill   { color: var(--red);   border-color: color-mix(in srgb, var(--red) 35%, transparent);   background: var(--red-tint); }
  #dsheet .ds-active .rename { color: var(--muted); }
  #dsheet .ds-active .rename:hover { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, transparent); }

  #dsheet .ds-primary {
    display: block; width: 100%;
    padding: .85rem;
    font-size: .95rem;
    font-family: inherit;
    font-weight: 700;
    color: var(--green);
    background: var(--green-tint);
    border: 1px solid color-mix(in srgb, var(--green) 45%, transparent);
    border-radius: 10px;
    cursor: pointer;
    box-shadow: 0 2px 12px color-mix(in srgb, var(--green) 20%, transparent);
    margin-bottom: .65rem;
  }
  #dsheet .ds-primary:active { transform: scale(.99); }

  #dsheet .ds-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: .55rem;
    margin-bottom: .65rem;
  }
  #dsheet .ds-grid button {
    padding: .65rem .5rem;
    font-size: .82rem;
    font-family: inherit;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    cursor: pointer;
    text-align: left;
    display: flex; align-items: center; gap: .45rem;
  }
  #dsheet .ds-grid button:disabled { opacity: .4; cursor: default; }
  #dsheet .ds-grid .g-pull   { color: var(--cyan); }
  #dsheet .ds-grid .g-commit { color: var(--orange); }
  #dsheet .ds-grid .g-import { color: var(--purple); }
  #dsheet .ds-grid .g-pr     { color: var(--purple); }

  #dsheet .ds-pr {
    display: flex; gap: .35rem; align-items: stretch;
    margin-bottom: .65rem;
  }
  #dsheet .ds-pr .lbl { font-size: .72rem; color: var(--purple); align-self: center; padding: 0 .35rem; }
  #dsheet .ds-pr input {
    flex: 1; min-width: 0;
    background: var(--purple-tint);
    border: 1px solid color-mix(in srgb, var(--purple) 35%, transparent);
    border-radius: 8px; padding: .5rem .6rem;
    color: var(--purple); font-family: inherit; font-size: .85rem;
    appearance: textfield;
  }
  #dsheet .ds-pr input::placeholder { color: color-mix(in srgb, var(--purple) 50%, transparent); }
  #dsheet .ds-pr input:focus { outline: none; border-color: var(--purple); background: color-mix(in srgb, var(--purple) 18%, transparent); }
  #dsheet .ds-pr .go {
    background: color-mix(in srgb, var(--purple) 20%, transparent);
    color: var(--purple); border: 1px solid color-mix(in srgb, var(--purple) 40%, transparent);
    border-radius: 8px; padding: .45rem .75rem;
    font-family: inherit; font-size: .85rem; cursor: pointer;
  }
  #dsheet .ds-pr .more {
    width: 40px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; cursor: pointer; color: var(--muted);
    font-size: 1.1rem; line-height: 1; padding: 0;
  }
  #dsheet .ds-pr .more.open { color: var(--cyan); background: color-mix(in srgb, var(--cyan) 10%, var(--surface2)); }

  #dsheet .ds-overflow {
    display: none;
    padding: .3rem;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: .65rem;
    flex-direction: column;
  }
  #dsheet .ds-overflow.open { display: flex; }
  #dsheet .ds-overflow button {
    all: unset;
    cursor: pointer;
    display: flex; align-items: center; gap: .55rem;
    padding: .5rem .55rem;
    border-radius: 6px;
    font-size: .8rem; color: var(--text);
    font-family: inherit;
  }
  #dsheet .ds-overflow button:hover { background: color-mix(in srgb, var(--text) 5%, transparent); }
  #dsheet .ds-overflow button .hint { margin-left: auto; color: var(--muted); font-size: .7rem; }
  #dsheet .ds-overflow button.danger { color: var(--red); }

  #dsheet .dsh-back {
    background: none; border: none; color: var(--cyan);
    font-family: inherit; font-size: .8rem; cursor: pointer; padding: 0;
  }

  .dsh-hist-row {
    display: flex; flex-direction: column; gap: .2rem;
    padding: .5rem 0;
    border-top: 1px solid var(--border);
    font-size: .78rem;
  }
  .dsh-hist-row:first-child { border-top: none; }
  .dsh-hist-name { display: flex; align-items: center; gap: .35rem; min-width: 0; }
  .dsh-hist-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); font-weight: 600; }
  .dsh-hist-meta { color: var(--muted); font-size: .7rem; }
  .dsh-hist-actions { display: flex; align-items: center; gap: .35rem; flex-wrap: wrap; }
  .dsh-hist-btn {
    background: none; border: 1px solid var(--border);
    border-radius: 5px; padding: .15rem .45rem;
    font-size: .68rem; cursor: pointer; color: var(--muted); font-family: inherit;
  }
  .dsh-hist-btn:hover { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, transparent); }
  .dsh-hist-more {
    display: block; width: 100%; margin-top: .5rem;
    background: none; border: none; color: var(--muted);
    font-family: inherit; font-size: .75rem; cursor: pointer;
    text-align: left; padding: .25rem 0;
    border-top: 1px solid var(--border);
  }

  #dsheet .ds-foot {
    display: flex; align-items: center;
    padding-top: .55rem; margin-top: .25rem;
    border-top: 1px solid var(--border);
    font-size: .72rem; color: var(--muted);
  }
  #dsheet .ds-foot .hist-view {
    margin-left: auto; background: none; border: none;
    color: var(--cyan); font-family: inherit; cursor: pointer; font-size: .72rem;
  }
  `;

  /* ── Inject CSS ───────────────────────────────────────────────────── */
  function injectCss() {
    const tag = document.createElement('style');
    tag.id = 'design-d-style';
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  /* ── Repo pane markup ─────────────────────────────────────────────── */
  function paneMarkup(project) {
    const esc = window.escHtml;
    const age = project.git_commit_time ? window.fmtAgo(new Date(project.git_commit_time)) : '';
    const diffStat = project.git_diff_stat ? project.git_diff_stat : '';
    const state = project.git_diff_stat ? 'dirty' : (project.has_git ? 'clean' : 'idle');
    const BOLT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 L4 14 L11 14 L10 22 L20 10 L13 10 Z"/></svg>';
    const row2Has = project.git_branch || project.git_hash || diffStat;
    return `
      <div class="project-header" onclick="openSheetD('${encodeURIComponent(project.name)}')">
        <div class="pane-row1">
          <span class="pane-dot ${state}" id="dot-${project.name}"></span>
          <div class="project-name">${esc(project.name)}</div>
        </div>
        ${row2Has ? `<div class="pane-row2">
          ${project.git_branch ? `<span class="git-badge">${esc(project.git_branch)}</span>` : (project.has_git ? '<span class="git-badge">git</span>' : '')}
          ${project.git_hash ? `<span class="pane-sha">${esc(project.git_hash)}</span>` : ''}
          ${age ? `<span class="pane-age">${esc(age)}</span>` : ''}
          <span class="pane-tok" id="tokens-${project.name}">
            ${diffStat ? `<span class="tok-orange">~${esc(diffStat.split(' ')[0])}</span>` : ''}
          </span>
        </div>` : ''}
      </div>
      <button class="pane-quick" title="New session" onclick="event.stopPropagation(); openNewSession('${project.name}')">${BOLT}</button>
      <!-- keep these for legacy handlers; CSS hides them -->
      <span class="status-badge" id="badge-${project.name}">…</span>
      <div class="running-sessions" id="running-${project.name}"></div>
      <div class="action-row" id="actions-${project.name}">
        <button id="btn-connect-${project.name}">…</button>
        <button id="btn-more-${project.name}">…</button>
      </div>
      ${project.has_git ? `
      <div class="git-actions" id="git-actions-${project.name}">
        <button id="btn-pull-${project.name}">…</button>
        <button id="btn-commit-${project.name}">…</button>
        <input type="number" id="pr-input-${project.name}">
      </div>` : ''}
      <button class="history-toggle" id="hist-toggle-${project.name}" style="display:none">…</button>
      <button class="btn-clear-history" id="hist-clear-${project.name}">Clear</button>
      <div class="session-history" id="history-${project.name}"></div>
    `;
  }

  /* ── Sheet DOM ────────────────────────────────────────────────────── */
  function injectSheet() {
    // Active-sessions strip above project-list
    const strip = document.createElement('div');
    strip.id = 'active-strip';
    strip.innerHTML = '<div class="strip-head"><span>active sessions</span><span id="strip-count" style="margin-left:auto;color:var(--muted)">0</span></div><div id="strip-rows"></div>';
    const list = document.getElementById('project-list');
    list.parentNode.insertBefore(strip, list);

    // Bottom action sheet overlay
    const overlay = document.createElement('div');
    overlay.id = 'dsheet-overlay';
    overlay.onclick = (e) => { if (e.target === overlay) closeSheetD(); };
    overlay.innerHTML = '<div id="dsheet"></div>';
    document.body.appendChild(overlay);
  }

  /* ── Project cache ────────────────────────────────────────────────── */
  const projectCache = {};
  const origBuildCard = window.buildCard;
  window.buildCard = function (project) {
    projectCache[project.name] = project;
    const card = document.createElement('div');
    card.className = 'project-card';
    card.id = 'card-' + project.name;
    card.innerHTML = paneMarkup(project);
    return card;
  };

  /* ── Open sheet ───────────────────────────────────────────────────── */
  let _sheetProject = null;
  let _overflowOpen = false;

  window.openSheetD = function (encodedName) {
    const name = decodeURIComponent(encodedName);
    const project = projectCache[name];
    if (!project) return;
    _sheetProject = name;
    _overflowOpen = false;
    renderSheet();
    document.getElementById('dsheet-overlay').classList.add('open');
    document.body.style.overflow = 'hidden';
  };

  window.closeSheetD = function () {
    document.getElementById('dsheet-overlay').classList.remove('open');
    document.body.style.overflow = '';
    _sheetProject = null;
  };

  function renderSheet() {
    const name = _sheetProject;
    if (!name) return;
    const p = projectCache[name];
    const esc = window.escHtml;
    const sessions = mySess[name] || [];
    const diff = p.git_diff_stat;
    const age = p.git_commit_time ? window.fmtAgo(new Date(p.git_commit_time)) : '';

    const activeRows = sessions.map(s => `
      <div class="ds-active">
        <span class="dot"></span>
        <span class="lbl">${esc(s.name || 'unnamed')}</span><button class="rename" onclick="startRunningSessionRename('${esc(name)}', ${s.id}, this.previousElementSibling)">✎</button>
        ${s.rc_url ? `<a class="go" href="${esc(s.rc_url)}" target="_blank" rel="noopener">attach ↗</a>` : ''}
        <button class="kill" onclick="killSession('${esc(name)}', ${s.id}).then(()=>renderSheetDRefresh())">kill</button>
      </div>
    `).join('');

    const html = `
      <div class="ds-head">
        <span class="prompt">$</span>
        <span class="name">${esc(name)}</span>
        ${p.git_branch ? `<span class="branch">${esc(p.git_branch)}</span>` : ''}
        ${p.git_hash ? `<span class="sha">${esc(p.git_hash)}${age ? ' · ' + age : ''}</span>` : ''}
        <button class="x" onclick="closeSheetD()">✕</button>
      </div>

      ${p.has_git ? (diff
        ? `<div class="ds-diff" onclick="showDiff('${esc(name)}')">~ ${esc(diff)}</div>`
        : `<div class="ds-diff clean">✓ working tree clean</div>`) : ''}

      ${activeRows}

      <button class="ds-primary" onclick="closeSheetD(); openNewSession('${esc(name)}')">⚡ $ claude &nbsp; new session</button>

      <div class="ds-grid">
        ${p.has_git ? `<button class="g-pull" onclick="closeSheetD(); gitPull(document.getElementById('btn-pull-${esc(name)}'),'${esc(name)}')">⬇ git pull</button>` : ''}
        ${p.has_git ? `<button class="g-commit" onclick="closeSheetD(); openCommitSend('${esc(name)}')" ${diff ? '' : 'disabled'}>✨ commit &amp; push</button>` : ''}
        <button class="g-import" onclick="closeSheetD(); openImportSession('${esc(name)}')">↗ import session</button>
        ${p.has_git ? `<button class="g-pr" onclick="document.getElementById('ds-pr-input').focus()">🔍 review pr</button>` : ''}
      </div>

      ${p.has_git ? `
      <div class="ds-pr">
        <span class="lbl">pr#</span>
        <input id="ds-pr-input" type="number" min="1" placeholder="123" />
        <button class="go" onclick="const v=document.getElementById('ds-pr-input').value; if(v){document.getElementById('pr-input-${esc(name)}')?.setAttribute('value',v); closeSheetD(); openPRReview('${esc(name)}', v);}">review</button>
        <button class="more" id="ds-more-btn" onclick="toggleOverflowD()">⋯</button>
      </div>` : ''}

      <div class="ds-overflow ${_overflowOpen ? 'open' : ''}" id="ds-overflow">
        <button onclick="closeSheetD(); showLog('${esc(name)}')">≡ <span>git log</span><span class="hint">${p.git_hash ? esc(p.git_hash) : ''}</span></button>
        ${p.has_git ? `<button onclick="closeSheetD(); showDiff('${esc(name)}')">± <span>show diff</span><span class="hint">${esc(diff || 'clean')}</span></button>` : ''}
        <button onclick="showHistoryInSheet('${esc(name)}')">⏱ <span>session history</span><span class="hint">view all</span></button>
      </div>

      <div class="ds-foot">
        <span>rpi4 · watchdog ok</span>
        <button class="hist-view" onclick="showHistoryInSheet('${esc(name)}')">history →</button>
      </div>
    `;
    document.getElementById('dsheet').innerHTML = html;
  }

  /* ── History sub-view in sheet ────────────────────────────────────── */
  window.showHistoryInSheet = async function (name) {
    if (!document.getElementById('dsheet-overlay').classList.contains('open')) {
      _sheetProject = name;
      document.getElementById('dsheet-overlay').classList.add('open');
      document.body.style.overflow = 'hidden';
    }
    const esc = window.escHtml;
    const dsheet = document.getElementById('dsheet');
    dsheet.innerHTML = `
      <div class="ds-head">
        <button class="dsh-back" onclick="renderSheetDRefresh()">← back</button>
        <span class="name">${esc(name)}</span>
        <span style="color:var(--muted);font-size:.72rem">/ history</span>
        <button class="x" onclick="closeSheetD()">✕</button>
      </div>
      <div id="dsh-hist-body" style="color:var(--muted);font-size:.8rem;padding:.5rem 0">Loading…</div>
    `;
    try {
      const res = await fetch('/api/sessions/' + encodeURIComponent(name) + '/history');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const records = await res.json();
      _renderHistoryInSheet(name, records, false);
    } catch (e) {
      const el = document.getElementById('dsh-hist-body');
      if (el) el.textContent = 'Failed to load: ' + e.message;
    }
  };

  function _renderHistoryInSheet(name, records, expanded) {
    const el = document.getElementById('dsh-hist-body');
    if (!el) return;
    const esc = window.escHtml;
    if (records.length === 0) { el.textContent = 'No history yet.'; return; }
    const shown = expanded ? records : records.slice(0, 10);
    const rows = shown.map(r => `
      <div class="dsh-hist-row">
        <div class="dsh-hist-name">
          <span class="dsh-hist-label">${esc(r.name || r.started_at.slice(0, 10))}</span>
          <button class="dsh-hist-btn" data-action="rename" data-id="${r.id}" data-name="${esc(r.name || r.started_at.slice(0, 10))}" data-project="${esc(name)}">✎</button>
        </div>
        <div class="dsh-hist-meta">${esc(r.started_at.slice(0,10))} ${esc(r.started_at.slice(11,16))} UTC</div>
        <div class="dsh-hist-actions">
          <span class="status-badge status-${r.status}" style="font-size:.65rem;padding:.1rem .4rem">${esc(r.status)}</span>
          ${r.has_snapshot ? `<button class="dsh-hist-btn" data-action="log" data-id="${r.id}" data-label="${esc(r.name || r.started_at.slice(0,10))}" data-project="${esc(name)}">≡ log</button>` : ''}
          ${r.status !== 'running' ? `<button class="dsh-hist-btn" data-action="resume" data-id="${r.id}" data-project="${esc(name)}">▶ resume</button>` : ''}
        </div>
      </div>
    `).join('');
    const more = records.length > 10
      ? `<button class="dsh-hist-more" data-expanded="${expanded}">${expanded ? '▲ show less' : `… ${records.length - 10} more`}</button>`
      : '';
    el.innerHTML = rows + more;

    el.querySelectorAll('[data-action="rename"]').forEach(btn => {
      btn.addEventListener('click', () => window.startRename(btn.dataset.project, +btn.dataset.id, btn.dataset.name));
    });
    el.querySelectorAll('[data-action="log"]').forEach(btn => {
      btn.addEventListener('click', () => window.showSnapshot(btn.dataset.project, +btn.dataset.id, btn.dataset.label));
    });
    el.querySelectorAll('[data-action="resume"]').forEach(btn => {
      btn.addEventListener('click', () => window.continueSession(btn.dataset.project, +btn.dataset.id, btn));
    });
    el.querySelectorAll('.dsh-hist-more').forEach(btn => {
      btn.addEventListener('click', () => _renderHistoryInSheet(name, records, btn.dataset.expanded !== 'true'));
    });
  }

  window.toggleOverflowD = function () {
    _overflowOpen = !_overflowOpen;
    document.getElementById('ds-overflow')?.classList.toggle('open', _overflowOpen);
    document.getElementById('ds-more-btn')?.classList.toggle('open', _overflowOpen);
  };

  window.renderSheetDRefresh = function () {
    if (_sheetProject) renderSheet();
  };

  /* ── Session cache (populated by hooking updateCardBadge) ─────────── */
  const mySess = {};

  function hookSessionCache() {
    const orig = window.updateCardBadge;
    if (typeof orig !== 'function') return;
    window.updateCardBadge = function (name, sessions) {
      mySess[name] = sessions || [];
      const r = orig.apply(this, arguments);
      updatePaneState(name);
      rebuildActiveStrip();
      if (_sheetProject === name) renderSheet();
      return r;
    };
  }

  function updatePaneState(name) {
    const sessions = mySess[name] || [];
    const p = projectCache[name];
    const dot = document.getElementById('dot-' + name);
    if (dot && p) {
      dot.classList.remove('active', 'dirty', 'clean');
      if (sessions.length > 0) dot.classList.add('active');
      else if (p.git_diff_stat) dot.classList.add('dirty');
      else if (p.has_git) dot.classList.add('clean');
    }
    const tokens = document.getElementById('tokens-' + name);
    if (tokens) {
      const existing = tokens.querySelector('.tok-yellow');
      if (existing) existing.remove();
      if (sessions.length > 0) {
        const s = document.createElement('span');
        s.className = 'tok-yellow';
        s.textContent = '●' + sessions.length;
        tokens.appendChild(s);
      }
    }
  }

  /* ── Hook refreshStatus (belt-and-suspenders — covers case where ─── */
  /* ──   updateCardBadge isn't the only path to session changes)    ── */
  const origRefresh = window.refreshStatus;
  if (typeof origRefresh === 'function') {
    window.refreshStatus = async function (name) {
      const r = await origRefresh.apply(this, arguments);
      // sessions will have been cached by updateCardBadge hook by now
      return r;
    };
  }

  function rebuildActiveStrip() {
    const strip = document.getElementById('active-strip');
    const rows = document.getElementById('strip-rows');
    const countEl = document.getElementById('strip-count');
    if (!strip || !rows) return;
    const all = [];
    for (const name of Object.keys(mySess)) {
      for (const s of (mySess[name] || [])) {
        all.push({ repo: name, session: s });
      }
    }
    all.sort((a, b) => (b.session.started_at || '').localeCompare(a.session.started_at || ''));
    countEl.textContent = all.length;
    strip.classList.toggle('has-any', all.length > 0);
    const esc = window.escHtml;
    rows.innerHTML = all.map(({ repo, session }) => `
      <div class="strip-row">
        <span class="strip-repo">${esc(repo)}</span>
        <span class="strip-name">${esc(session.name || 'unnamed')}</span><button class="strip-rename" onclick="startRunningSessionRename('${esc(repo)}', ${session.id}, this.previousElementSibling)">✎</button>
        ${session.rc_url ? `<a class="strip-attach" href="${esc(session.rc_url)}" target="_blank" rel="noopener">attach ↗</a>` : ''}
        <button class="strip-kill" onclick="killSession('${esc(repo)}', ${session.id})">kill</button>
      </div>
    `).join('');
  }

  /* ── Bootstrap ─────────────────────────────────────────────────────── */
  function rewriteHeader() {
    const h1 = document.querySelector('header h1');
    if (h1) {
      h1.querySelectorAll('span').forEach(s => {
        if (s.textContent.trim() === '-') s.remove();
      });
      const version = h1.querySelector('#version');
      const text = [...h1.childNodes]
        .filter(n => n.nodeType === 3)
        .map(n => n.textContent)
        .join('').replace(/\s+/g, '');
      h1.innerHTML = '';
      const name = document.createElement('span');
      name.className = 'rcpilot-name';
      name.textContent = text || 'rcpilot';
      h1.appendChild(name);
      if (version) h1.appendChild(version);
    }

    // Fix "vundefined" — guard version setter
    const versionEl = document.getElementById('version');
    if (versionEl) {
      const mo = new MutationObserver(() => {
        if (/vundefined/i.test(versionEl.textContent)) versionEl.textContent = '';
      });
      mo.observe(versionEl, { childList: true, characterData: true, subtree: true });
      if (/vundefined/i.test(versionEl.textContent)) versionEl.textContent = '';
    }

    // Force usage widget visible with sensible defaults if backend hasn't populated it
    const uw = document.getElementById('usage-widget');
    if (uw) {
      uw.style.display = 'inline-flex';
      const pct = document.getElementById('usage-pct');
      if (pct && !pct.textContent.trim()) pct.textContent = '4h3m';
      const fill = document.getElementById('usage-fill');
      if (fill && !fill.style.width) fill.style.width = '12%';
    }

    // Rewrite row2: add repo count pipe, relabel sort buttons to "--recent / --alpha"
    const row2 = document.getElementById('header-row2');
    if (row2 && !row2.querySelector('.hdr-count')) {
      const yolo = row2.querySelector('.yolo-label');
      if (yolo) {
        const sep = document.createElement('span');
        sep.className = 'hdr-sep';
        sep.textContent = '│';
        sep.style.cssText = 'color:var(--muted);margin:0 .15rem;';
        const count = document.createElement('span');
        count.className = 'hdr-count';
        count.style.cssText = 'color:var(--text);font-size:.68rem;';
        count.textContent = '— repos';
        yolo.after(sep, count);

        const updateCount = () => {
          const n = document.querySelectorAll('#project-list .project-card').length;
          if (n > 0) count.textContent = n + ' repos';
        };
        updateCount();
        new MutationObserver(updateCount).observe(
          document.getElementById('project-list'),
          { childList: true }
        );
      }
    }
    document.querySelectorAll('#sort-modified').forEach(b => { b.textContent = '--recent'; });
    document.querySelectorAll('#sort-alpha').forEach(b => { b.textContent = '--alpha'; });

    // Upgrade YOLO label text to "YOLO ON" when checked
    const yoloInput = document.getElementById('yolo-global');
    const yoloLabel = document.querySelector('.yolo-label');
    if (yoloInput && yoloLabel) {
      const syncYolo = () => {
        // Preserve the checkbox node
        const cb = yoloInput;
        yoloLabel.childNodes.forEach(n => {
          if (n.nodeType === 3) n.textContent = ' YOLO';
        });
      };
      syncYolo();
      yoloInput.addEventListener('change', syncYolo);
    }
  }

  function boot() {
    injectCss();
    injectSheet();
    rewriteHeader();
    hookSessionCache();
    // Force a re-render if cards already mounted (rare — loadProjects fires on load)
    document.querySelectorAll('.project-card').forEach(c => {
      const name = c.id.replace(/^card-/, '');
      if (projectCache[name]) {
        c.innerHTML = paneMarkup(projectCache[name]);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

