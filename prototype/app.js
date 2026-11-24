/* ===========================================================
   知鉴 Search · 前端逻辑
   ===========================================================
   数据来源（双模）：
     1) 优先请求后端 POST /api/search
     2) 8 秒内没响应 / 报错 / 返回 degraded，自动改用本地 seed.js
   界面会如实标明当前是哪一种，不假装。

   字段来源见 backend/pipeline.py 的 payload：
     query / intent / intent_label / mode / degraded_reason
     answers[]  { id,title,content_text,url,author_signature,
                  vote_up_count,published_year,age_days,
                  is_ad_suspect,reasons[],group }
     cards[0]   { core_conclusion,conclusion_confidence,key_points,
                  basis_titles,summary_source,counts }
     trace[]    { stage,label,detail }   ← 阶段是动态的，别按下标取
   =========================================================== */

/* 后端地址的推导顺序：
   1. localStorage 里手动指定的（调试用）
   2. 如果页面是从 http(s) 打开的（局域网/线上），就用同一个主机名 + 8000 端口
      —— 这样手机访问 192.168.x.x:8080 时，会自动去请求 192.168.x.x:8000
   3. 兜底：本机 127.0.0.1 */
const API_BASE = (() => {
  const manual = localStorage.getItem('ZHIJIAN_API');
  if (manual) return manual.replace(/\/+$/, '');
  if (location.protocol.startsWith('http')) {
    const port = location.port === '8000' ? '' : ':8000';
    return `${location.protocol}//${location.hostname}${port}`;
  }
  return 'http://127.0.0.1:8000';
})();
const TIMEOUT_MS = 8000;

let CURRENT = null;
let TECH_ON = false;
let selectedIds = new Set();
let selectMode = false;   // 默认不进入「挑重点」模式

const $ = (id) => document.getElementById(id);

/* ---------------- 首页话题 ---------------- */
const TOPICS = [
  { q: '全屋定制怎么避免被坑', hint: '经验避坑' },
  { q: '扫地机器人值得买吗', hint: '争议话题' },
  { q: '2000元扫地机器人 石头和追觅哪个好', hint: '对比选购' }
];

(function initTopics() {
  $('topics').innerHTML = TOPICS.map(
    (t) => `<button class="topic" onclick="pickTopic('${t.q}')">${t.q}<i>${t.hint}</i></button>`
  ).join('');
})();

/* HTML 里「开始」按钮的入口。
   之前漏了这个函数，导致点按钮报 is not defined、只有回车能用。 */
function startSearch() {
  doSearch($('q').value);
}

function pickTopic(q) {
  $('q').value = q;
  doSearch(q);
}

$('q').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') doSearch($('q').value);
});

/* ---------------- 视图切换 ---------------- */
function show(which) {
  ['home', 'loading', 'result', 'empty'].forEach((v) => {
    $(v).style.display =
      v === which ? (v === 'home' ? 'flex' : 'block') : 'none';
  });
  $('topbar').classList.toggle('on', which !== 'home');
  window.scrollTo({ top: 0, behavior: 'instant' });
}

function goHome() {
  CURRENT = null;
  $('q').value = '';
  if (TECH_ON) toggleTech();
  show('home');
  setTimeout(() => $('q').focus(), 80);
}

/* ---------------- 加载态 ---------------- */
const LOAD_STEPS = [
  '正在读你这个问题…',
  '拆成几个更具体的问题分别去找…',
  '排掉太旧的内容…',
  '看看有没有在卖东西的…',
  '把不同说法归到一起…',
  '写结论…'
];

function runSteps() {
  $('lsteps').innerHTML = LOAD_STEPS.map((s, i) => `<b id="st${i}">${s}</b>`).join('');
  LOAD_STEPS.forEach((_, i) => {
    setTimeout(() => {
      for (let k = 0; k <= i; k++) {
        const el = $('st' + k);
        if (el) el.className = k === i ? 'now' : 'done';
      }
    }, i * 620);
  });
}

/* ---------------- 检索 ---------------- */
async function doSearch(rawQuery) {
  const query = (rawQuery || '').trim();
  if (!query) {
    $('q').focus();
    return;
  }
  $('miniq-text').textContent = query;
  show('loading');
  runSteps();

  const started = Date.now();
  try {
    const res = await fetch(`${API_BASE}/api/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, options: {} }),
      signal: AbortSignal.timeout(TIMEOUT_MS)
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (!data || !Array.isArray(data.answers)) throw new Error('返回结构不对');

    // 后端自己说降级了，就按降级展示
    if (data.mode === 'degraded') {
      render(data, data.degraded_reason || '后端暂时没给出结果');
      return;
    }
    render(data);
  } catch (err) {
    // 连不上就退回本地 seed，并如实说明
    const wait = Math.max(0, 900 - (Date.now() - started));
    setTimeout(() => {
      const fallback = JSON.parse(JSON.stringify(SEED));
      render(fallback, '没连上检索服务，这是提前存好的一份结果');
    }, wait);
  }
}

/* ---------------- 渲染 ---------------- */
function groupOf(d, name) {
  return (d.answers || []).filter((a) => a.group === name);
}

function clip(text, limit) {
  const t = (text || '').replace(/\s+/g, ' ').trim();
  if (t.length <= limit) return { text: t, truncated: false };
  return { text: t.slice(0, limit), truncated: true };
}

function sourceLine(a) {
  const parts = [];
  parts.push(`<span class="who">知乎 · ${escapeHtml(a.author_signature || '匿名')}</span>`);
  if (a.published_year) parts.push(`<span class="yr">${a.published_year}</span>`);
  if (a.vote_up_count) parts.push(`<span class="votes">${a.vote_up_count} 赞</span>`);
  return parts.join('');
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

/* 收起区的条目渲染。render 和 renderEmpty 都用它，避免两处逻辑分叉。 */
function hiddenHTML(list) {
  return `<div class="fold-note">没有删掉它们。你自己看一眼，判断权在你手上。</div>` +
    list.map((h) => {
      const c = clip(h.content_text, 240);
      return `
      <div class="hid">
        <div class="why">为什么收起：${escapeHtml((h.reasons || []).join('；') || '内容可疑')}</div>
        <div class="txt"><span class="ellip">…</span>${escapeHtml(c.text)}${c.truncated ? '…' : ''}</div>
        <div class="src">${sourceLine(h)}</div>
        <button class="override" onclick="override(this)">我认为这条没问题</button>
      </div>`;
    }).join('');
}

function render(data, degradedReason) {
  CURRENT = data;
  selectedIds = new Set();
  if (selectMode) exitSelectMode(true);
  syncPickBtn();

  // 先判断有没有东西可展示。没东西时走空态，不要渲染一个空壳。
  const all = data.answers || [];
  const usable = all.filter((a) => a.group !== 'hidden');
  const forced = new URLSearchParams(location.search).get('state');
  if (forced === 'empty' || (!usable.length && !forced)) {
    renderEmpty(data, degradedReason);
    return;
  }
  show('result');

  const card = (data.cards || [])[0] || {};
  const isDegraded = data.mode === 'degraded' || !!degradedReason;

  /* --- 顶栏模式标记：必须诚实 --- */
  const status = document.querySelector('.status');
  status.innerHTML = isDegraded
    ? '<i class="dot" style="background:#C9A227"></i><span>演示数据 · 未连接后端</span>'
    : '<i class="dot"></i><span>真实检索 · 本地服务</span>';
  if (degradedReason) status.title = degradedReason;

  /* --- 头部 --- */
  $('rq').textContent = data.query || '';
  $('rmeta').textContent = data.intent_label || '综合探索';
  const st = data.stats || {};
  $('rcount').textContent =
    st.recalled != null
      ? `读了 ${st.recalled} 篇，留下 ${(st.after_freshness || 0) - (st.hidden || 0)} 篇`
      : '';

  /* --- 结论 --- */
  const confidence = card.conclusion_confidence || 'none';
  // 三档：medium 正常给结论；low 只能说到这；none 干脆没有结论
  const weak = confidence === 'low' || confidence === 'none';
  const noConclusion = confidence === 'none';
  $('rkicker').textContent =
    confidence === 'none' ? '说实话' : confidence === 'low' ? '只能说到这' : '先说结论';
  $('rhead').textContent = card.core_conclusion || '这次没有找到能形成结论的内容。';
  $('rhead').style.fontSize = weak ? '20px' : '';
  $('rhead').style.color = weak ? 'var(--ink-2)' : '';

  const points = (card.key_points || []).filter(Boolean);
  const basisCount = (card.basis_titles || []).length;
  $('rsub').textContent = noConclusion
    ? '下面这几条是别人提到最多的，你自己判断。'
    : confidence === 'low'
    ? `材料只够说到这个程度，这条结论的把握不大。`
    : basisCount
    ? `这句是从 ${basisCount} 条回答里归纳出来的。`
    : '';
  $('rsub').style.display = '';
  $('rquick').innerHTML = points.map((p) => `<li>${escapeHtml(p)}</li>`).join('');
  if (!points.length) {
    // 没有要点时那条分隔线会悬空，去掉
    $('rsub').style.cssText = 'padding-bottom:0;border-bottom:none;margin-bottom:0;';
  }

  /* --- 三段 --- */
  const blocks = [
    { key: 'consensus', title: '大家都这么说', tag: '多篇内容一致', cls: '' },
    { key: 'disputed', title: '说法不一的地方', tag: '同一个问题，结论相反', cls: '' },
    { key: 'pitfalls', title: '有人具体踩过的坑', tag: '带场景，能对上自己家', cls: 'pit' }
  ];

  // 每组先给两条，其余折起来。
  // 为什么不全部折叠：全收起来时首屏只剩三个标题，一个字都读不到，
  // 「30 秒看懂」就废了。留两条既有实质内容，页面也不至于太长。
  const PER_GROUP = 2;

  const renderItem = (a, cls) => {
    const c = clip(a.content_text, 190);
    const picked = selectedIds.has(a.id);
    return `
      <div class="item ${cls}${picked ? ' picked' : ''}" data-full="${escapeHtml(a.content_text || '')}">
        <label class="pickbox"><input type="checkbox" data-pick="${escapeHtml(a.id)}" ${picked ? 'checked' : ''} aria-label="加入精选"></label>
        <div class="body">
          <p class="q-title">${escapeHtml(a.title || '')}</p>
          <p><span class="ellip">…</span>${escapeHtml(c.text)}${c.truncated ? '…' : ''}</p>
          ${c.truncated ? `<button class="more" onclick="expand(this)">展开全文</button>` : ''}
          <div class="src">${sourceLine(a)}</div>
        </div>
      </div>`;
  };

  $('blocks').innerHTML = blocks
    .map((b, bi) => {
      const arr = groupOf(data, b.key);
      if (!arr.length) return '';
      const head = arr.slice(0, PER_GROUP).map((a) => renderItem(a, b.cls)).join('');
      const rest = arr.slice(PER_GROUP);
      const restHtml = rest.length
        ? `<div class="rest" id="rest${bi}" hidden>${rest.map((a) => renderItem(a, b.cls)).join('')}</div>
           <button class="more-group" id="moreBtn${bi}" onclick="toggleGroup(${bi})">
             还有 ${rest.length} 条 · 展开
           </button>`
        : '';
      return `
        <div class="block">
          <div class="block-head"><h3>${b.title}</h3><span class="tag">${b.tag} · ${arr.length} 条</span></div>
          ${head}${restHtml}
        </div>`;
    })
    .join('');

  /* --- 收起区 --- */
  const hidden = groupOf(data, 'hidden');
  if (hidden.length) {
    $('fold').style.display = '';
    $('foldn').textContent = hidden.length + ' 条';
    $('foldt').textContent = `另有 ${hidden.length} 条内容，我们觉得你不该直接信`;
    $('foldbody').innerHTML = hiddenHTML(hidden);
  } else {
    $('fold').style.display = 'none';
  }

  /* --- 技术抽屉：按 label 渲染，不假设阶段数 --- */
  $('tech').innerHTML =
    `<h4>技术细节 · 供评委查看，普通用户默认看不到</h4>
     <div class="trace">
       ${(data.trace || [])
         .map(
           (t) =>
             `<div class="tr"><span class="lb">${escapeHtml(t.label || t.stage)}</span><span class="dt">${escapeHtml(t.detail || '')}</span></div>`
         )
         .join('')}
     </div>
     <div class="tstats">
       <div class="tstat"><div class="v">${st.recalled || 0}</div><div class="k">初始召回</div></div>
       <div class="tstat"><div class="v">${st.after_freshness || 0}</div><div class="k">时效过滤后</div></div>
       <div class="tstat"><div class="v">${st.hidden || 0}</div><div class="k">降权保留</div></div>
       <div class="tstat"><div class="v">${st.kept || 0}</div><div class="k">可用内容</div></div>
     </div>
     ${degradedReason ? `<div class="fold-note" style="margin-top:12px">降级原因：${escapeHtml(degradedReason)}</div>` : ''}`;
  $('tech').classList.toggle('on', TECH_ON);

  $('fold').classList.remove('open');

  const pickable = (data.answers || []).filter((a) => a.group !== 'hidden').length;
  const pk = $('pickBtn');
  if (pk) pk.style.display = pickable ? '' : 'none';
}

function toggleGroup(i) {
  const rest = $('rest' + i);
  const btn = $('moreBtn' + i);
  if (!rest || !btn) return;
  const open = !rest.hidden;
  rest.hidden = open;
  const n = rest.querySelectorAll('.item').length;
  btn.textContent = open ? `还有 ${n} 条 · 展开` : '收起';
  if (!open) rest.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function expand(btn) {
  const item = btn.closest('.item');
  const full = item ? item.getAttribute('data-full') || '' : '';
  const p = btn.previousElementSibling;
  if (p && full) p.textContent = full;
  btn.remove();
}

/* ---------------- 交互 ---------------- */
function toggleFold() {
  $('fold').classList.toggle('open');
}

function toggleTech() {
  TECH_ON = !TECH_ON;
  $('tech').classList.toggle('on', TECH_ON);
  $('dtoggle').classList.toggle('on', TECH_ON);
  syncTechLabel();
  if (TECH_ON && CURRENT) $('tech').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/* 手机顶栏放不下六个字，缩成「细节 / 收起细节」 */
function syncTechLabel() {
  const btn = $('dtoggle');
  if (!btn) return;
  const narrow = window.innerWidth <= 680;
  btn.textContent = TECH_ON
    ? (narrow ? '收起细节' : '已显示技术细节')
    : (narrow ? '细节' : '显示技术细节');
}
window.addEventListener('resize', syncTechLabel);
document.addEventListener('DOMContentLoaded', syncTechLabel);

function override(btn) {
  if (btn.classList.contains('did')) {
    btn.classList.remove('did');
    btn.textContent = '我认为这条没问题';
  } else {
    btn.classList.add('did');
    btn.textContent = '已记录你的判断 · 下次不再收起';
  }
}


/* ===========================================================
   勾选与重组
   -----------------------------------------------------------
   设计说明：
   · 勾选的粒度是「整条内容」，不是整篇回答——用户要的是
     「这几条我要留着」，而不是「这篇我要收藏」
   · 导出的是本地拼接的 Markdown，不调模型、不烧额度
   · 文件里保留作者、年份、原文链接，让人能回去核对
   =========================================================== */

function updateSelbar() {
  const bar = $('selbar');
  if (!bar) return;
  $('selcount').textContent = selectedIds.size;
  bar.classList.toggle('on', selectMode);
  const go = $('goSel');
  if (go) go.disabled = selectedIds.size === 0;
}

/* 进入 / 退出「挑重点」模式
   默认不显示勾选框：多数人只想要答案，不想做选择。
   只有用户主动点「自己挑重点重组」才进入选择态。 */
function toggleSelectMode() {
  if (selectMode) exitSelectMode();
  else enterSelectMode();
}

function enterSelectMode() {
  selectMode = true;
  document.body.classList.add('select-mode');
  updateSelbar();
  syncPickBtn();
  const first = document.querySelector('[data-pick]');
  if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function exitSelectMode(silent) {
  // 只退出模式，不清空已选——用户可能只是先看一眼答案，
  // 回头还要接着挑。真要清空有「清空」按钮。
  selectMode = false;
  document.body.classList.remove('select-mode');
  updateSelbar();
  if (!silent) syncPickBtn();
}

/* 按钮上带数量，退出后也知道自己选过东西 */
function syncPickBtn() {
  const btn = $('pickBtn');
  if (!btn) return;
  const n = selectedIds.size;
  if (selectMode) {
    btn.textContent = '退出选择';
    btn.classList.add('primary');
  } else {
    btn.textContent = n ? `继续挑重点（已选 ${n} 条）` : '自己挑重点重组';
    btn.classList.remove('primary');
  }
}

function syncPicked() {
  document.querySelectorAll('[data-pick]').forEach((cb) => {
    const item = cb.closest('.item');
    if (item) item.classList.toggle('picked', cb.checked);
  });
}

// 事件委托：渲染是重建 innerHTML，挂在父级才不会失效
document.addEventListener('change', (e) => {
  const cb = e.target.closest && e.target.closest('[data-pick]');
  if (!cb) return;
  const id = cb.getAttribute('data-pick');
  if (cb.checked) selectedIds.add(id);
  else selectedIds.delete(id);
  syncPicked();
  updateSelbar();
  syncPickBtn();
});

function clearSel() {
  selectedIds = new Set();
  document.querySelectorAll('[data-pick]').forEach((cb) => { cb.checked = false; });
  syncPicked();
  updateSelbar();
  syncPickBtn();
}

const GROUP_TITLE = {
  consensus: '大家都这么说',
  disputed: '说法不一的地方',
  pitfalls: '有人具体踩过的坑',
  hidden: '被收起的内容'
};

function selectedItems() {
  if (!CURRENT) return [];
  return (CURRENT.answers || []).filter((a) => selectedIds.has(a.id));
}

function openCompose() {
  const rows = selectedItems();
  if (!rows.length) return;

  // 按分组聚合，保持与结果页一致的顺序
  const order = ['consensus', 'disputed', 'pitfalls'];
  const groups = {};
  rows.forEach((a) => {
    const g = a.group || 'consensus';
    (groups[g] = groups[g] || []).push(a);
  });

  const body = order
    .filter((g) => groups[g])
    .map((g) => {
      const items = groups[g]
        .map((a) => {
          const link = a.url
            ? `<a href="${a.url}" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>`
            : '<span>来源链接不可用</span>';
          const c = clip(a.content_text, 260);
          return `<div class="cmp-entry" data-full="${escapeHtml(a.content_text || '')}">
            <p>${escapeHtml(c.text)}${c.truncated ? '…' : ''}</p>
            ${c.truncated ? '<button class="more" onclick="expandCmp(this)">展开这条</button>' : ''}
            <small>
              <span>知乎 · ${escapeHtml(a.author_signature || '匿名')}</span>
              ${a.published_year ? `<span>${a.published_year}</span>` : ''}
              ${a.vote_up_count ? `<span>${a.vote_up_count} 赞</span>` : ''}
              ${link}
            </small>
          </div>`;
        })
        .join('');
      return `<div class="cmp-group"><div class="gt">${GROUP_TITLE[g] || g}</div>${items}</div>`;
    })
    .join('');

  $('modalbody').innerHTML = `
    <p class="modal-intro">按你选的顺序拼在一起，保留出处与时间。不带任何改写。</p>
    <div class="compose-doc">
      <h4>${escapeHtml(CURRENT.query || '')}</h4>
      <div class="meta">${rows.length} 条 · 来自知乎公开内容 · 整理于 ${new Date().toLocaleDateString('zh-CN')}</div>
      ${body}
    </div>
    <div class="modal-actions">
      <button class="act" onclick="closeModal()">返回</button>
      <button class="act primary" onclick="exportMarkdown()">导出 Markdown</button>
    </div>`;
  $('modal').classList.add('on');
}

function expandCmp(btn) {
  const entry = btn.closest('.cmp-entry');
  const full = entry ? entry.getAttribute('data-full') || '' : '';
  const p = entry ? entry.querySelector('p') : null;
  if (p && full) p.textContent = full;
  btn.remove();
}

function closeModal() {
  $('modal').classList.remove('on');
  syncPickBtn();
}

function exportMarkdown() {
  const rows = selectedItems();
  if (!rows.length) return;

  const order = ['consensus', 'disputed', 'pitfalls'];
  const lines = [
    `# ${CURRENT.query || '精选资料'}`,
    '',
    `> ${rows.length} 条内容，拼自知乎公开回答与文章。`,
    '> 这里的判断只作参考，可信度与时效是系统辅助评估，请结合原文自行核对。',
    ''
  ];

  order.forEach((g) => {
    const items = rows.filter((a) => (a.group || 'consensus') === g);
    if (!items.length) return;
    lines.push(`## ${GROUP_TITLE[g]}`, '');
    items.forEach((a) => {
      lines.push('…' + (a.content_text || ''));
      lines.push('');
      const meta = [`知乎 · ${a.author_signature || '匿名'}`];
      if (a.published_year) meta.push(String(a.published_year));
      if (a.vote_up_count) meta.push(`${a.vote_up_count} 赞`);
      lines.push(`— ${meta.join(' · ')}`);
      if (a.url) lines.push(`原文：${a.url}`);
      lines.push('');
    });
  });

  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const name = (CURRENT.query || '精选').replace(/[\\/:*?"<>|]/g, '_').slice(0, 40);
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${name}-精选.md`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}


/* ===========================================================
   空状态
   -----------------------------------------------------------
   三种情况要分开说，因为用户接下来该做的事不一样：
   1. 完全没找到 —— 换个问法
   2. 找到了但都不可信 —— 可以自己看，或者放宽条件
   3. 服务出问题 —— 等一会儿或看缓存
   =========================================================== */
function renderEmpty(data, degradedReason) {
  show('result');   // 仍然用结果页的外壳（保留顶栏和搜索框）
  // 但把主体换成空态
  $('blocks').innerHTML = '';

  const all = data.answers || [];
  const isDeg = data.mode === 'degraded' || !!degradedReason;
  const status = document.querySelector('.status');
  status.innerHTML = isDeg
    ? '<i class="dot" style="background:#C9A227"></i><span>演示数据 · 未连接后端</span>'
    : '<i class="dot"></i><span>真实检索 · 本地服务</span>';
  if (degradedReason) status.title = degradedReason;
  const hidden = all.filter((a) => a.group === 'hidden');
  const isDegraded = data.mode === 'degraded' || !!degradedReason;
  const forced = new URLSearchParams(location.search).get('state');

  let kicker = '说实话';
  let title = '';
  let reason = '';
  const opts = [];

  if (forced === 'empty' || (!all.length && !isDegraded)) {
    title = '这次一条都没找到';
    reason = '不是所有问题都能在社区里找到答案。有些太新、有些太窄，或者问法跟别人用的不一样。';
    opts.push({ label: '换个说法试试', hint: '比如把「哪个好」换成「有什么缺点」', action: 'home' });
  } else if (!all.filter((a) => a.group !== 'hidden').length && hidden.length) {
    title = `找到 ${all.length} 条，但都不太可信`;
    reason = `它们要么在卖东西，要么信息太旧。我没有删，放在下面了，你可以自己判断。`;
    opts.push({ label: `看看这 ${all.length} 条`, hint: '它们可能对你仍有参考价值', action: 'showHidden' });
    opts.push({ label: '换个问法', hint: '换个角度可能找到更实在的经验', action: 'home' });
  } else {
    kicker = '暂时不行';
    title = '现在拿不到内容';
    reason = degradedReason || '检索服务没有响应。可能是网络问题，也可能是今天的调用次数用完了。';
    opts.push({ label: '再试一次', hint: '如果是网络抖动，重试通常就好了', action: 'retry' });
    opts.push({ label: '换个问法', hint: '', action: 'home' });
  }

  $('rq').textContent = data.query || '';
  $('rmeta').textContent = data.intent_label || '';
  $('rcount').textContent = all.length ? `找到 ${all.length} 条` : '';

  $('rkicker').textContent = kicker;
  $('rhead').textContent = title;
  $('rhead').style.fontSize = '21px';
  $('rhead').style.color = 'var(--ink)';
  $('rsub').textContent = reason;
  $('rsub').style.cssText = 'padding-bottom:0;border-bottom:none;margin-bottom:0;';
  $('rquick').innerHTML = '';

  // 建议卡片塞进第一段的位置
  $('blocks').innerHTML = `
    <div class="block">
      <div class="block-head"><h3>接下来可以</h3><span class="tag">${opts.length} 个建议</span></div>
      <div class="empty-opts">
        ${opts.map((o, i) => `
          <button class="opt" onclick="emptyAction('${o.action}')">
            <div><div class="lab">${escapeHtml(o.label)}</div>${o.hint ? `<div class="hint">${escapeHtml(o.hint)}</div>` : ''}</div>
            <span class="go2">→</span>
          </button>`).join('')}
      </div>
    </div>`;

  // 即使当前不显示，也要把收起区的内容填好——
  // 用户点「看看这 N 条」时要能立刻展开，不能是空的
  if (hidden.length) {
    $('foldn').textContent = hidden.length + ' 条';
    $('foldt').textContent = `另有 ${hidden.length} 条内容，我们觉得你不该直接信`;
    $('foldbody').innerHTML = hiddenHTML(hidden);
    $('fold').style.display = 'none';
  } else {
    $('fold').style.display = 'none';
  }
  $('pickBtn').style.display = 'none';
  $('tech').innerHTML = '';
  $('tech').classList.remove('on');
  syncPickBtn();
}

function emptyAction(action) {
  if (action === 'home') { goHome(); return; }
  if (action === 'retry') {
    if (CURRENT && CURRENT.query) doSearch(CURRENT.query);
    return;
  }
  if (action === 'showHidden') {
    // 把被收起的内容摊开：三段清空，只留收起区并展开
    $('blocks').innerHTML = `<div class="block"><div class="block-head">
      <h3>这些都被判为不可信</h3><span class="tag">没有删除，只是收起</span></div></div>`;
    $('fold').style.display = '';
    $('fold').classList.add('open');
    $('rhead').textContent = '你自己看一眼';
    $('rkicker').textContent = '判断权在你';
    $('rsub').textContent = '下面这些是系统觉得不该直接信的。可能判错，你可以点「我认为这条没问题」推翻它。';
    return;
  }
}
