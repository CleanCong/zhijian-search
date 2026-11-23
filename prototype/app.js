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

const API_BASE = (localStorage.getItem('ZHIJIAN_API') || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const TIMEOUT_MS = 8000;

let CURRENT = null;
let TECH_ON = false;

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

function render(data, degradedReason) {
  CURRENT = data;
  show('result');

  const card = (data.cards || [])[0] || {};
  const isDegraded = data.mode === 'degraded' || !!degradedReason;

  /* --- 顶栏模式标记：必须诚实 --- */
  const status = document.querySelector('.status');
  status.innerHTML = isDegraded
    ? '<i class="dot" style="background:#C9A227"></i>演示数据 · 未连接后端'
    : '<i class="dot"></i>真实检索 · 本地服务';
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

  /* --- 三段 --- */
  const blocks = [
    { key: 'consensus', title: '大家都这么说', tag: '多篇内容一致', cls: '' },
    { key: 'disputed', title: '说法不一的地方', tag: '同一个问题，结论相反', cls: '' },
    { key: 'pitfalls', title: '有人具体踩过的坑', tag: '带场景，能对上自己家', cls: 'pit' }
  ];

  $('blocks').innerHTML = blocks
    .map((b) => {
      const arr = groupOf(data, b.key);
      if (!arr.length) return '';
      const items = arr
        .map((a, i) => {
          const c = clip(a.content_text, 190);
          return `
          <div class="item ${b.cls}">
            <div class="idx">${String(i + 1).padStart(2, '0')}</div>
            <div class="body">
              <p class="q-title">${escapeHtml(a.title || '')}</p>
              <p><span class="ellip">…</span>${escapeHtml(c.text)}${c.truncated ? '…' : ''}</p>
              ${c.truncated ? `<button class="more" onclick="expand(this, ${i})" data-full="${escapeHtml(a.content_text)}">展开全文</button>` : ''}
              <div class="src">${sourceLine(a)}</div>
            </div>
          </div>`;
        })
        .join('');
      return `
        <div class="block">
          <div class="block-head"><h3>${b.title}</h3><span class="tag">${b.tag} · ${arr.length} 条</span></div>
          ${items}
        </div>`;
    })
    .join('');

  /* --- 收起区 --- */
  const hidden = groupOf(data, 'hidden');
  if (hidden.length) {
    $('fold').style.display = '';
    $('foldn').textContent = hidden.length + ' 条';
    $('foldt').textContent = `另有 ${hidden.length} 条内容，我们觉得你不该直接信`;
    $('foldbody').innerHTML =
      `<div class="fold-note">没有删掉它们。你自己看一眼，判断权在你手上。</div>` +
      hidden
        .map((h) => {
          const c = clip(h.content_text, 240);
          return `
          <div class="hid">
            <div class="why">为什么收起：${escapeHtml((h.reasons || []).join('；') || '内容可疑')}</div>
            <div class="txt"><span class="ellip">…</span>${escapeHtml(c.text)}${c.truncated ? '…' : ''}</div>
            <div class="src">${sourceLine(h)}</div>
            <button class="override" onclick="override(this)">我认为这条没问题</button>
          </div>`;
        })
        .join('');
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
}

function expand(btn, idx) {
  const full = btn.getAttribute('data-full') || '';
  const p = btn.previousElementSibling;
  p.textContent = full;
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
  $('dtoggle').textContent = TECH_ON ? '已显示技术细节' : '显示技术细节';
  if (TECH_ON && CURRENT) $('tech').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function override(btn) {
  if (btn.classList.contains('did')) {
    btn.classList.remove('did');
    btn.textContent = '我认为这条没问题';
  } else {
    btn.classList.add('did');
    btn.textContent = '已记录你的判断 · 下次不再收起';
  }
}
