'use strict';
/* 小狐狸控制台 —— 前端逻辑（零依赖，原生 JS）
   后端接口契约见 server.py；所有写操作都由后端做校验 + 备份 + 原子落盘。 */

const TABS = [
  ['overview', '概览'],
  ['ai', 'AI 模型与密钥'],
  ['models', '模型注册表'],
  ['persona', '人格与话术'],
  ['voice', '语音 TTS'],
  ['presets', '预设与角色'],
  ['users', '用户与权限'],
  ['ops', '连接与运维'],
  ['backup', '备份还原'],
];

const S = {
  config: null, configMtime: 0, meta: null, overview: null,
  presets: {}, presetFiles: [], current: null, originalId: null,
  users: null, usersMtime: 0, prefs: { on: {}, seen: {}, voice: {} }, prefsRemove: [],
  voices: null, backups: [], logs: [], dirty: false, presetDirty: false,
  settings: null, audit: null, logTimer: null, logName: null,
};

/* 由 JS 动态创建的元素 id（tests/check_dom_refs.py 会读这一行做静态引用检查）。
   改这里的 id 时，记得同步 presetForm() / renderVoiceUI() 里的字面量。 */
const DYNAMIC_IDS = ['presetId', 'presetName', 'presetInfo', 'presetUid', 'presetPath', 'previewText'];

/* 不经通用字段渲染、由自定义编辑器维护的配置键。
   后端「字段审计」会读这一行，避免把这些键误报成"界面改不了"。
   Others. 前缀表示 Others 段下的键；不带前缀表示 config.json 顶层键。 */
const UI_EXTRA_KEYS = ['Others.models', 'Others.ROOT_User', 'Others.Auto_approval', 'Others.TTS',
                       'owner', 'black_list', 'silents'];

/* ------------------------------------------------------------------ */
/* 小工具                                                              */
/* ------------------------------------------------------------------ */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const TOKEN = new URLSearchParams(location.search).get('token') || '';

function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function setPath(obj, path, val) {
  const ks = path.split('.');
  let o = obj;
  for (let i = 0; i < ks.length - 1; i++) {
    if (typeof o[ks[i]] !== 'object' || o[ks[i]] === null) o[ks[i]] = {};
    o = o[ks[i]];
  }
  o[ks[ks.length - 1]] = val;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, kind = 'ok', ms = 3600) {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
}
function loading(on) { $('#loading').classList.toggle('hidden', !on); }
function markDirty(on = true) {
  S.dirty = on;
  $('#dirtyBadge').classList.toggle('hidden', !on);
}

async function api(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (TOKEN) headers['X-Auth-Token'] = TOKEN;
  const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts, { headers }));
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  if (!res.ok || (data && data.ok === false)) {
    const msg = (data && data.error && data.error.message) || `请求失败（HTTP ${res.status}）`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ------------------------------------------------------------------ */
/* 字段定义（schema 驱动，改配置只要在这里加一行）                       */
/* ------------------------------------------------------------------ */
const F = {};

F['f-ai-backend'] = [
  {
    path: 'Others.ai_backend', type: 'radio', label: '当前使用哪个后端', store: 'config',
    options: [['cloud', '云端（DeepSeek 官方 / 中转站）'], ['local', '本地（LM Studio 等）']],
    hint: '云端走 cloud_base_url + cloud_model；本地走 local_base_url + local_model。'
      + '机器人里用「~后端」指令也能即时切换，不必重启。'
  },
];

F['f-ai-cloud'] = [
  { path: 'Others.cloud_base_url', type: 'text', label: '云端 Base URL', placeholder: 'https://api.deepseek.com/v1', hint: '留空表示用 DeepSeek 官方地址。' },
  { path: 'Others.cloud_model', type: 'text', label: '云端模型名', placeholder: 'deepseek-chat' },
  { path: 'Others.cloud_reasoning', type: 'switch', label: '开启思考模式（思维链模型返回 reasoning_content）' },
  { path: 'Others.cloud_extra_headers', type: 'json', label: '额外请求头（JSON 对象）', placeholder: '{"sso-ak":"…"}' },
];

F['f-ai-local'] = [
  { path: 'Others.local_base_url', type: 'text', label: '本地 Base URL', placeholder: 'http://localhost:1234/v1' },
  { path: 'Others.local_model', type: 'text', label: '本地模型名', placeholder: 'google/gemma-3-4b' },
];

F['f-ai-keys'] = [
  { path: 'Others.deepseek_key', type: 'password', label: 'DeepSeek / 中转站 Key' },
  { path: 'Others.gemini_key', type: 'password', label: 'Google Gemini Key' },
  { path: 'Others.openai_key', type: 'password', label: 'OpenAI Key' },
  { path: 'Others.gemini_base_url', type: 'text', label: 'Gemini Base URL' },
  { path: 'Others.gemini_model', type: 'text', label: 'Gemini 模型名' },
];

F['f-ai-mode'] = [
  { path: 'Others.default_mode', type: 'select', label: '默认对话模式', source: 'modes', hint: '需求⑤（多模型切换）落地后，这一项会被「模型注册表」里的 models.default 取代；在那之前它仍决定用哪条代码链路。' },
  { path: 'Others.asr_enabled', type: 'switch', label: '启用语音转文字（ASR，收到语音消息时转成文字再交给 AI）' },
  { path: 'Others.asr_model', type: 'text', label: 'ASR 模型大小', placeholder: 'base' , hint: 'faster-whisper 的模型尺寸：tiny / base / small / medium。' },
  { path: 'Others.asr_language', type: 'text', label: 'ASR 语言', placeholder: 'zh' },
  { path: 'Others.asr_timeout_seconds', type: 'number', label: 'ASR 下载语音的超时（秒）', min: 5, max: 300, hint: '默认 30。网络慢就调大；这两项已经接到 Tools/asr.py 上，真会生效。' },
  { path: 'Others.asr_max_seconds', type: 'number', label: 'ASR 最长处理时长（秒，0=不限制）', min: 0, max: 3600, hint: '超过这个长度的语音只转写前 N 秒，避免一条长语音把线程占住。' },
];

F['f-persona-bubbles'] = [
  { path: 'Others.private_reply_quote', type: 'switch', label: '私聊引用回复（开着时机器人引用你的话再回）' },
  { path: 'Others.quote_only_first_chunk', type: 'switch', label: '流式回复只引用第一条' },
  { path: 'Others.private_typing_bubble', type: 'switch', label: '私聊显示"正在输入"气泡' },
  { path: 'Others.thinking_bubble', type: 'switch', label: '思考气泡（等模型出结果时先发个气泡占位）' },
  { path: 'Others.thinking_bubble_delay', type: 'number', label: '思考气泡延迟（秒）', min: 0, max: 30, hint: '超过这个时间还没出结果才发气泡，避免秒回时多余。' },
  { path: 'Others.thinking_bubble_text', type: 'text', label: '思考气泡文字', placeholder: '...' },
  { path: 'Others.thinking_bubble_max', type: 'number', label: '思考气泡最长存在（秒）', min: 5, max: 600, hint: '到了就撤回/替换，防止气泡卡住。' },
];

F['f-persona-id'] = [
  { path: 'Others.bot_name', type: 'text', label: '机器人中文名' },
  { path: 'Others.bot_name_en', type: 'text', label: '机器人英文名' },
  { path: 'Others.reminder', type: 'text', label: '指令触发前缀', hint: '默认是 ~ ，即「~帮助」「~语音」这种。' },
  { path: 'Others.slogan', type: 'text', label: '口号（~关于 等处显示）' },
];

F['f-persona-misc'] = [
  {
    path: 'Others.confused_words', type: 'textarea', rows: 3, store: 'config',
    label: '听不懂/越界时的回话（{bot_name} 会被替换成机器人名）',
    hint: '机器人遇到做不到的指令时回这句。留空代码里还有默认值兜底。',
  },
  {
    path: 'Others.affinity_daily_cap', type: 'number', label: '好感度每日上限（每人每天最多加多少）',
    min: 0, max: 1000, hint: 'userstore.add_affinity 的 daily_cap，默认 30；设 0 等于不加好感度。',
  },
];

F['f-persona-compliment'] = [
  { path: 'Others.compliment', type: 'list', label: '夸奖语列表', placeholder: '啊！别这么夸我啦…', addLabel: '添加一条夸奖语' },
];

F['f-persona-poke'] = [
  { path: 'Others.poke_rejection_phrases', type: 'list', label: '戳一戳拒绝语列表', placeholder: '哎呀别戳啦…', addLabel: '添加一条拒绝语' },
];

F['f-users-lists'] = [
  { path: 'owner', type: 'tags', label: '框架 owner（最高权限，可管理整个框架）', store: 'users', kind: 'int' },
  { path: 'root_user', type: 'tags', label: 'ROOT_User（管理员通知接收者）', store: 'users', kind: 'int' },
  { path: 'super_user', type: 'tags', label: 'Super_User（Super_User.ini）', store: 'users', kind: 'str' },
  { path: 'manage_user', type: 'tags', label: 'Manage_User（Manage_User.ini）', store: 'users', kind: 'str' },
  { path: 'auto_approval', type: 'tags', label: 'Auto_approval（这些 QQ 申请入群时自动同意，留空=谁的都不自动同意）', store: 'users', kind: 'int' },
  { path: 'black_list', type: 'tags', label: 'black_list（框架级黑名单）', store: 'users', kind: 'int' },
  { path: 'silents', type: 'tags', label: 'silents（框架级静默名单）', store: 'users', kind: 'int' },
];

F['f-conn'] = [
  { path: 'protocol', type: 'radio', label: '协议', options: [['OneBot', 'OneBot v11'], ['Satori', 'Satori']] },
  { path: 'Connection.mode', type: 'radio', label: '连接模式', options: [['FWS', '正向 WebSocket（机器人连 NapCat）'], ['HTTP', 'HTTP + 反向上报']] },
  { path: 'Connection.host', type: 'text', label: 'NapCat Host' },
  { path: 'Connection.port', type: 'number', label: 'NapCat Port', min: 1, max: 65535 },
  { path: 'Connection.listener_host', type: 'text', label: 'HTTP 上报监听 Host' },
  { path: 'Connection.listener_port', type: 'number', label: 'HTTP 上报监听 Port', min: 1, max: 65535 },
  { path: 'Connection.retries', type: 'number', label: '断线重试次数', min: 0, max: 100 },
  { path: 'Connection.satori_token', type: 'text', label: 'Satori Token（用不上就留空）' },
  { path: 'Log_level', type: 'select', label: '日志等级', source: 'log_levels' },
  { path: 'uin', type: 'number', label: '登录 QQ 号（记录用，可留 0）', min: 0 },
];

/* ------------------------------------------------------------------ */
/* 字段渲染                                                            */
/* ------------------------------------------------------------------ */
function storeOf(sp) {
  if (sp.store === 'users') return S.users;
  return S.config;
}
function readField(sp) { return getPath(storeOf(sp), sp.path); }
function writeField(sp, val) {
  setPath(storeOf(sp), sp.path, val);
  if (sp.store === 'users') return;   // 用户名单改动由 users 面板统一保存
  markDirty(true);
}

function fieldWrap(sp, control, extra) {
  const wrap = document.createElement('label');
  wrap.className = 'field';
  const lab = document.createElement('span');
  lab.className = 'label';
  lab.textContent = sp.label;
  wrap.appendChild(lab);
  wrap.appendChild(control);
  if (sp.hint) {
    const h = document.createElement('span');
    h.className = 'hint';
    h.textContent = sp.hint;
    wrap.appendChild(h);
  }
  if (extra) wrap.appendChild(extra);
  return wrap;
}

function optionsFor(sp) {
  if (sp.source === 'modes') return (S.meta.modes || []).map(m => [m.value, m.label]);
  if (sp.source === 'log_levels') return (S.meta.log_levels || []).map(v => [v, v]);
  return sp.options || [];
}

function buildField(sp) {
  const cur = readField(sp);

  if (sp.type === 'radio') {
    const box = document.createElement('div');
    box.className = 'radios';
    optionsFor(sp).forEach(([val, label]) => {
      const l = document.createElement('label');
      if (String(cur) === String(val)) l.classList.add('on');
      const r = document.createElement('input');
      r.type = 'radio'; r.name = 'r-' + sp.path; r.value = val;
      r.checked = String(cur) === String(val);
      r.addEventListener('change', () => {
        $$('label', box).forEach(x => x.classList.remove('on'));
        l.classList.add('on');
        writeField(sp, val);
      });
      l.appendChild(r);
      l.appendChild(document.createTextNode(label));
      box.appendChild(l);
    });
    return fieldWrap(sp, box);
  }

  if (sp.type === 'switch') {
    const l = document.createElement('label');
    l.className = 'inline';
    const c = document.createElement('input');
    c.type = 'checkbox';
    c.checked = !!cur;
    c.addEventListener('change', () => writeField(sp, c.checked));
    l.appendChild(c);
    l.appendChild(document.createTextNode('开启'));
    return fieldWrap(sp, l);
  }

  if (sp.type === 'select') {
    const sel = document.createElement('select');
    optionsFor(sp).forEach(([val, label]) => {
      const o = document.createElement('option');
      o.value = val; o.textContent = label;
      if (String(cur) === String(val)) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener('change', () => writeField(sp, sel.value));
    return fieldWrap(sp, sel);
  }

  if (sp.type === 'json') {
    const ta = document.createElement('textarea');
    ta.rows = 3; ta.className = 'mono';
    ta.value = JSON.stringify(cur || {}, null, 2);
    let ok = true;
    ta.addEventListener('input', () => {
      try {
        const v = JSON.parse(ta.value || '{}');
        if (typeof v !== 'object' || Array.isArray(v)) throw new Error('必须是对象');
        ta.style.borderColor = '';
        writeField(sp, v);
      } catch (e) {
        ta.style.borderColor = 'var(--err)';
      }
    });
    return fieldWrap(sp, ta);
  }

  if (sp.type === 'list') {
    const box = document.createElement('div');
    const render = () => {
      box.innerHTML = '';
      const arr = readField(sp) || [];
      arr.forEach((item, i) => {
        const row = document.createElement('div');
        row.className = 'listrow';
        const inp = document.createElement('input');
        inp.className = 'input'; inp.value = item;
        inp.placeholder = sp.placeholder || '';
        inp.addEventListener('input', () => {
          const a = readField(sp) || [];
          a[i] = inp.value;
          writeField(sp, a);
        });
        const del = document.createElement('button');
        del.className = 'btn small danger'; del.textContent = '删除';
        del.addEventListener('click', () => {
          const a = readField(sp) || [];
          a.splice(i, 1);
          writeField(sp, a);
          render();
        });
        row.appendChild(inp); row.appendChild(del);
        box.appendChild(row);
      });
      const add = document.createElement('button');
      add.className = 'btn small'; add.textContent = sp.addLabel || '添加一条';
      add.addEventListener('click', () => {
        const a = readField(sp) || [];
        a.push('');
        writeField(sp, a);
        render();
      });
      box.appendChild(add);
    };
    render();
    return fieldWrap(sp, box);
  }

  if (sp.type === 'tags') {
    const inp = document.createElement('input');
    inp.className = 'input mono';
    const arr = cur || [];
    inp.value = arr.join(' ');
    inp.placeholder = sp.kind === 'int' ? '123456 234567' : '每行/空格分隔';
    inp.addEventListener('input', () => {
      const parts = inp.value.split(/[\s,，、]+/).map(x => x.trim()).filter(Boolean);
      writeField(sp, sp.kind === 'int' ? parts.filter(x => /^\d+$/.test(x)).map(Number) : parts);
    });
    return fieldWrap(sp, inp);
  }

  if (sp.type === 'password') {
    const box = document.createElement('div');
    box.className = 'row';
    box.style.marginTop = '0';
    const inp = document.createElement('input');
    inp.className = 'input mono'; inp.type = 'password';
    inp.value = cur || '';
    inp.placeholder = '留空表示不用这个 Key';
    inp.style.flex = '1';
    inp.addEventListener('input', () => writeField(sp, inp.value));
    const eye = document.createElement('button');
    eye.className = 'btn small ghost'; eye.textContent = '显示';
    eye.addEventListener('click', () => {
      inp.type = inp.type === 'password' ? 'text' : 'password';
      eye.textContent = inp.type === 'password' ? '显示' : '隐藏';
    });
    box.appendChild(inp); box.appendChild(eye);
    return fieldWrap(sp, box);
  }

  if (sp.type === 'textarea') {
    const ta = document.createElement('textarea');
    ta.rows = sp.rows || 4;
    ta.value = cur || '';
    ta.addEventListener('input', () => writeField(sp, ta.value));
    return fieldWrap(sp, ta);
  }

  // text / number
  const inp = document.createElement('input');
  inp.className = 'input';
  inp.type = sp.type === 'number' ? 'number' : 'text';
  if (sp.type === 'number') {
    if (sp.min != null) inp.min = sp.min;
    if (sp.max != null) inp.max = sp.max;
  }
  inp.value = cur == null ? '' : cur;
  if (sp.placeholder) inp.placeholder = sp.placeholder;
  inp.addEventListener('input', () => {
    writeField(sp, sp.type === 'number' ? Number(inp.value || 0) : inp.value);
  });
  return fieldWrap(sp, inp);
}

function renderFields() {
  for (const id of Object.keys(F)) {
    const box = document.getElementById(id);
    if (!box) continue;
    const saved = box.__extra; // 保留动态追加的内容容器
    box.innerHTML = '';
    F[id].forEach(sp => box.appendChild(buildField(sp)));
  }
}

/* ------------------------------------------------------------------ */
/* 各面板                                                              */
/* ------------------------------------------------------------------ */
function renderTabs() {
  const nav = $('#tabs');
  nav.innerHTML = '';
  TABS.forEach(([id, label], i) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.dataset.tab = id;
    if (i === 0) b.classList.add('active');
    b.addEventListener('click', () => showTab(id));
    nav.appendChild(b);
  });
}
function showTab(id) {
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
  $$('.panel').forEach(p => p.classList.toggle('active', p.dataset.panel === id));
  if (id === 'ops') { refreshLogs(); }
}

function kv(label, value, cls) {
  return `<div class="kv"><b>${esc(label)}</b><span class="${cls || ''}">${value}</span></div>`;
}

function renderOverview() {
  const d = S.overview;
  if (!d) return;
  $('#botDir').textContent = d.bot_dir;
  $('#verTag').textContent = 'v' + d.version;

  const bot = d.bot || {};
  const pill = $('#botPill');
  if (bot.confirmed) {
    pill.textContent = `机器人运行中（PID ${(bot.pids || []).join(', ')}）`;
    pill.className = 'pill ok';
  } else if (bot.probable) {
    pill.textContent = `机器人很可能在运行（按锁文件推断）`;
    pill.className = 'pill warn';
  } else if (!bot.scan_available) {
    pill.textContent = '无法确认（进程扫描被拦）';
    pill.className = 'pill warn';
  } else {
    pill.textContent = '机器人未运行';
    pill.className = 'pill bad';
  }

  const be = d.backend || {};
  const keys = be.keys || {};
  const keyLine = ['deepseek', 'gemini', 'openai']
    .map(k => `${k}: ${keys[k] ? esc(keys[k].source) : '—'}`).join('　');

  const h = d.config_health || {};
  const lock = bot.lock;
  const cards = [
    ['机器人进程',
      bot.confirmed ? '运行中' : (bot.probable ? '很可能在运行' : (bot.scan_available ? '未运行' : '无法确认')),
      bot.confirmed ? 'ok' : (bot.probable || !bot.scan_available ? 'warn' : 'bad'),
      esc(bot.detection || '')],
    ['OneBot 端口', `${bot.onebot_port} ${bot.onebot_open ? '可连接' : '未监听'}`, bot.onebot_open ? 'ok' : 'warn',
      `反向上报端口 5003：${bot.listener_port_open ? '已监听' : '未监听'}`],
    ['锁文件 jianer.lock', bot.stale_lock ? '残留，需清理'
      : (lock && lock.alive ? (bot.probable ? '与运行中进程吻合' : '有活进程') : '无 / 已释放'),
      bot.stale_lock ? 'bad' : (bot.probable ? 'ok' : ''), lock && lock.alive
        ? `PID ${lock.pid} · ${esc(lock.name || '')} · 启动于 ${esc(lock.started_str || '')}`
        : '未发现被占用的锁'],
    ['AI 后端', esc(be.ai_backend === 'cloud' ? '云端' : '本地'), '',
      esc(be.ai_backend === 'cloud' ? (be.cloud_model || '') : (be.local_model || ''))],
    ['默认模式', esc(be.default_mode || '—'), '', esc(be.bot_name || '')],
    ['API Key', keys.deepseek && keys.deepseek.ok ? '已配置' : '未配置',
      keys.deepseek && keys.deepseek.ok ? 'ok' : 'bad', keyLine],
    ['配置文件', h.ok ? '正常' : '有问题', h.ok ? 'ok' : 'bad',
      `${((h.size || 0) / 1024).toFixed(1)} KB · ${esc(h.mtime_str || '')}`],
    ['预设 / 备份', `${d.counts.presets} 个预设`, '', `${d.counts.backups} 份备份 · ${d.counts.logs} 个日志`],
  ];
  $('#ovCards').innerHTML = cards.map(([t, v, cls, sub]) => `
    <div class="card">
      <h2>${esc(t)}</h2>
      <div class="big ${cls === 'bad' ? '' : ''}" style="${cls === 'bad' ? 'color:var(--err)' : cls === 'warn' ? 'color:var(--warn)' : cls === 'ok' ? 'color:var(--ok)' : ''}">${v}</div>
      <p class="hint">${sub || ''}</p>
    </div>`).join('');

  const checks = (h.checks || []);
  $('#ovHealth').innerHTML = '<div class="alerts">' + (checks.length ? checks.map(c =>
    `<div class="alert ${c.ok ? 'ok' : 'bad'}">${c.ok ? '✅' : '❌'} ${esc(c.name)}：${esc(c.detail || '')}</div>`
  ).join('') : '<div class="alert warn">尚未体检</div>') +
    (h.suspects && h.suspects.length ? `<div class="alert warn">疑似被 '?' 破坏的行：${h.suspects.slice(0, 5).map(s => '第 ' + s.line + ' 行').join('、')}</div>` : '') +
    '</div>';

  const warnBox = $('#ovLockWarn');
  if (bot.stale_lock || bot.probable) {
    const cls = bot.stale_lock ? 'bad' : 'warn';
    const title = bot.stale_lock ? '⚠ 残留锁文件：' : 'ℹ 状态说明：';
    warnBox.innerHTML = `<div class="alerts" style="margin-top:10px"><div class="alert ${cls}">
      ${title}${esc(bot.stale_lock_hint)}</div>
      <div class="row"><button class="btn ${bot.stale_lock ? 'danger' : 'ghost small'}"
        data-act="bot-clear-lock" data-force="${bot.probable ? '1' : '0'}">
        ${bot.stale_lock ? '清理残留锁文件' : '万一判断错了：强行清理锁文件'}</button>
      <span class="hint">清理只会删掉 <code>jianer.lock</code>（先备份），不影响任何进程。</span></div></div>`;
    const b = warnBox.querySelector('[data-act]');
    if (b) b.addEventListener('click', (e) => botAction('bot-clear-lock', e.target));
  } else {
    warnBox.innerHTML = '';
  }

  $('#ovFiles').innerHTML = Object.entries(d.files || {}).map(([name, m]) =>
    kv(name, m.exists ? `${(m.size / 1024).toFixed(2)} KB · ${esc(m.mtime_str)}` : '<span class="tag bad">不存在</span>')
  ).join('');
}

function renderKeyStatus() {
  const box = $('#f-ai-keys');
  if (!box || !S.overview || !S.overview.backend || !S.overview.backend.keys) return;
  const keys = S.overview.backend.keys;
  const bar = document.createElement('div');
  bar.className = 'row';
  bar.innerHTML = Object.entries(keys).map(([name, k]) =>
    `<span class="tag ${k.ok ? 'ok' : 'bad'}">${esc(name)}：${esc(k.source)}</span>`).join('');
  box.appendChild(bar);
}

function renderPresets() {
  const box = $('#presetList');
  const ids = Object.keys(S.presets || {});
  if (!ids.length) { box.innerHTML = '<div class="hint" style="padding:10px">还没有预设</div>'; return; }
  box.innerHTML = '';
  ids.forEach(id => {
    const p = S.presets[id] || {};
    const el = document.createElement('div');
    el.className = 'item' + (id === S.current ? ' on' : '');
    el.innerHTML = `<div class="grow"><div class="name">${esc(p.name || id)}</div>
      <div class="sub">${esc(id)} · ${esc(p.path || '')} · 适用 ${(p.uid || []).length} 人</div></div>`;
    el.addEventListener('click', () => loadPreset(id));
    box.appendChild(el);
  });
}

function presetForm() {
  const box = $('#f-preset-form');
  if (!box) return;
  box.innerHTML = '';
  const mk = (label, id, ph) => {
    const l = document.createElement('label');
    l.className = 'field';
    l.innerHTML = `<span class="label">${label}</span>`;
    const i = document.createElement('input');
    i.className = 'input'; i.id = id; i.placeholder = ph || '';
    i.addEventListener('input', () => { S.presetDirty = true; $('#presetDirty').textContent = '未保存'; });
    l.appendChild(i);
    box.appendChild(l);
    return i;
  };
  mk('预设 ID（英文/数字/中文，不含路径符）', 'presetId', '如 p12345678 或 猫娘');
  mk('预设名称（在 QQ 里用 ~角色扮演 名称 切换）', 'presetName', '如 小猫娘');
  mk('简介 / 切换时的回话', 'presetInfo', '如 喵～主人～');
  mk('适用 QQ 号（空格分隔，可留空）', 'presetUid', '123456 234567');
  mk('正文文件名（默认 <ID>.txt）', 'presetPath', 'p12345678.txt');
}

async function loadPreset(id) {
  try {
    loading(true);
    const r = await api('/api/presets/' + encodeURIComponent(id));
    const d = r.data;
    S.current = id; S.originalId = id; S.presetDirty = false;
    $('#presetId').value = d.id;
    $('#presetName').value = d.name || '';
    $('#presetInfo').value = d.info || '';
    $('#presetUid').value = (d.uid || []).join(' ');
    $('#presetPath').value = d.path || '';
    $('#presetContent').value = d.content || '';
    $('#presetEditorTitle').textContent = '编辑预设：' + (d.name || d.id);
    $('#presetFileTag').textContent = d.path + ' · ' + (d.file.exists ? (d.file.size + ' 字节') : '文件不存在');
    $('#presetFileTag').className = 'tag ' + (d.file.exists ? 'ok' : 'bad');
    $('#presetDirty').textContent = '';
    renderPresets();
  } catch (e) { toast(e.message, 'err'); }
  finally { loading(false); }
}

async function savePreset() {
  const payload = {
    original_id: S.originalId || undefined,
    id: $('#presetId').value.trim(),
    name: $('#presetName').value.trim(),
    info: $('#presetInfo').value.trim(),
    uid: $('#presetUid').value.split(/[\s,，、]+/).filter(Boolean),
    path: $('#presetPath').value.trim(),
    content: $('#presetContent').value,
  };
  if (!payload.id) { toast('预设 ID 不能为空', 'warn'); return; }
  try {
    loading(true);
    const r = await api('/api/presets', { method: 'POST', body: JSON.stringify(payload) });
    toast('预设已保存' + (r.warnings && r.warnings.length ? '（' + r.warnings.join('；') + '）' : ''), 'ok');
    S.current = payload.id; S.originalId = payload.id; S.presetDirty = false;
    $('#presetDirty').textContent = '';
    await loadPresetList();
  } catch (e) { toast(e.message, 'err', 6000); }
  finally { loading(false); }
}

async function deletePreset() {
  if (!S.current) { toast('先选一个预设', 'warn'); return; }
  const delFile = confirm(`删除预设「${S.current}」？\n\n点「确定」= 同时删除正文文件（会先备份）\n点「取消」= 什么都不做`);
  if (!delFile) return;
  if (!confirm('再确认一次：确定要删除这个预设及其正文文件吗？')) return;
  try {
    loading(true);
    await api('/api/presets/' + encodeURIComponent(S.current) + '?delete_file=1', { method: 'DELETE' });
    toast('预设已删除', 'ok');
    S.current = null; S.originalId = null;
    $('#presetId').value = ''; $('#presetName').value = ''; $('#presetInfo').value = '';
    $('#presetUid').value = ''; $('#presetPath').value = ''; $('#presetContent').value = '';
    $('#presetEditorTitle').textContent = '预设内容';
    await loadPresetList();
  } catch (e) { toast(e.message, 'err', 6000); }
  finally { loading(false); }
}

async function loadPresetList() {
  const r = await api('/api/presets');
  S.presets = r.presets || {};
  S.presetFiles = r.files || [];
  renderPresets();
}

/* ---------------- 语音 ---------------- */
function voiceOptions(forSession) {
  const out = [];
  if (forSession) out.push(['', '不覆盖（用全局默认）']);
  (S.voices.builtin || []).forEach(v => out.push([v.name, `${v.name}（${v.desc}）`]));
  Object.entries(S.voices.gender || {}).forEach(([g, id]) => out.push([g, `${g}（快捷）`]));
  (S.voices.edge || []).forEach(v => out.push([v.name, `${v.name} · ${v.friendly || ''}`]));
  return out;
}

function renderVoiceUI() {
  const box = $('#f-voice-main');
  box.innerHTML = '';
  const cur = S.config.Others.TTS || {};
  const opts = [];
  (S.voices.builtin || []).forEach(v => opts.push([v.id, `${v.name}（${v.desc}）· ${v.id}`]));
  Object.entries(S.voices.gender || {}).forEach(([g, id]) => opts.push([id, `${g}默认 · ${id}`]));
  (S.voices.edge || []).forEach(v => { if (!opts.some(o => o[0] === v.name)) opts.push([v.name, `${v.name} · ${v.friendly || ''}`]); });

  const mk = (label, id, node, hint) => {
    const l = document.createElement('label');
    l.className = 'field';
    l.innerHTML = `<span class="label">${label}</span>`;
    l.appendChild(node);
    if (hint) { const h = document.createElement('span'); h.className = 'hint'; h.textContent = hint; l.appendChild(h); }
    box.appendChild(l);
    return l;
  };

  const sel = document.createElement('select');
  opts.forEach(([v, t]) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = t;
    if (String(cur.voiceColor) === String(v)) o.selected = true;
    sel.appendChild(o);
  });
  if (cur.voiceColor && !opts.some(o => String(o[0]) === String(cur.voiceColor))) {
    const o = document.createElement('option');
    o.value = cur.voiceColor; o.textContent = cur.voiceColor + '（当前值，不在清单里）';
    o.selected = true;
    sel.appendChild(o);
  }
  sel.addEventListener('change', () => { S.config.Others.TTS.voiceColor = sel.value; markDirty(); if (S.previewVoice) S.previewVoice.value = sel.value; });
  mk('全局默认声线', 'ttsVoice', sel, '在 QQ 里用「~音色 名称」可以按会话临时切换，保存在 voice_prefs.json。');

  [['rate', '语速', -100, 100, '+0%'], ['volume', '音量', -100, 100, '+0%'], ['pitch', '音调', -100, 100, '+0Hz']].forEach(([k, label, min, max, def]) => {
    const row = document.createElement('div');
    row.className = 'row';
    row.style.marginTop = '0';
    const rng = document.createElement('input');
    rng.type = 'range'; rng.min = min; rng.max = max; rng.step = 1;
    const initVal = parseInt(String(cur[k] || def).replace(/[^\-\d]/g, ''), 10) || 0;
    rng.value = initVal;
    const txt = document.createElement('input');
    txt.className = 'input mono'; txt.style.maxWidth = '110px';
    txt.value = cur[k] || def;
    const sync = (v) => { S.config.Others.TTS[k] = v; markDirty(); };
    rng.addEventListener('input', () => { txt.value = (rng.value >= 0 ? '+' : '') + rng.value + (k === 'pitch' ? 'Hz' : '%'); sync(txt.value); });
    txt.addEventListener('change', () => { sync(txt.value); });
    row.appendChild(rng); row.appendChild(txt);
    mk(label + '（' + (k === 'pitch' ? 'Hz' : '%') + '）', 'tts-' + k, row);
  });

  const pv = document.createElement('div');
  pv.className = 'field';
  pv.innerHTML = '<span class="label">试听用的声线</span>';
  const pvSel = document.createElement('select');
  [['', '（跟上面的全局默认一致）']].concat(opts).forEach(([v, t]) => {
    const o = document.createElement('option'); o.value = v; o.textContent = t; pvSel.appendChild(o);
  });
  S.previewVoice = pvSel;
  pv.appendChild(pvSel);
  const pt = document.createElement('textarea');
  pt.id = 'previewText'; pt.rows = 2;
  pt.value = '你好呀，我是' + ((S.config.Others.bot_name) || '小狐狸') + '，这是一句试听。';
  const ptWrap = document.createElement('label');
  ptWrap.className = 'field';
  ptWrap.innerHTML = '<span class="label">试听文本</span>';
  ptWrap.appendChild(pt);
  const box2 = $('#f-voice-preview');
  box2.innerHTML = '';
  box2.appendChild(pv);
  box2.appendChild(ptWrap);

  const lb = $('#voiceListBox');
  const edge = S.voices.edge || [];
  lb.innerHTML = edge.length
    ? edge.map(v => `<div class="voice"><span>${esc(v.name)}</span><span class="tag">${esc(v.gender || '')} ${esc(v.local_name || '')}</span></div>`).join('')
    : `<div class="alert warn">在线清单没拿到：${esc(S.voices.error || '未知原因')}<br>内置的 ${(S.voices.builtin || []).length} 个音色照样可用。</div>`;
}

/* ---------------- 用户与权限 ---------------- */
function renderPrefsTable() {
  const box = $('#voicePrefsTable');
  const on = S.prefs.on || {};
  const voice = S.prefs.voice || {};
  const keys = Array.from(new Set(Object.keys(on).concat(Object.keys(voice)))).sort();
  box.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'row';
  head.innerHTML = '<span class="tag">会话</span><span class="tag">语音开关</span><span class="tag">声线覆盖</span>';
  if (!keys.length) {
    box.innerHTML = '<div class="hint" style="padding:8px 0">还没有任何会话记录（机器人跟人聊过语音后会自动出现）。</div>';
  }
  keys.forEach(k => {
    const row = document.createElement('div');
    row.className = 'listrow';
    const name = document.createElement('input');
    name.className = 'input mono'; name.value = k; name.style.maxWidth = '140px'; name.disabled = true;
    const sw = document.createElement('label');
    sw.className = 'inline';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = !!on[k];
    cb.addEventListener('change', () => { S.prefs.on[k] = cb.checked; });
    sw.appendChild(cb);
    sw.appendChild(document.createTextNode(on[k] ? '开' : '关'));
    cb.addEventListener('change', () => { sw.lastChild.textContent = cb.checked ? '开' : '关'; });
    const sel = document.createElement('select');
    sel.style.maxWidth = '260px';
    voiceOptions(true).forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if (String(voice[k] || '') === String(v)) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener('change', () => { S.prefs.voice[k] = sel.value; });
    const del = document.createElement('button');
    del.className = 'btn small danger'; del.textContent = '移除';
    del.addEventListener('click', () => {
      delete S.prefs.on[k]; delete S.prefs.voice[k];
      S.prefsRemove.push(k);
      renderPrefsTable();
    });
    row.appendChild(name); row.appendChild(sw); row.appendChild(sel); row.appendChild(del);
    box.appendChild(row);
  });
}

/* ---------------- 运维 ---------------- */
function renderOps() {
  const d = S.overview || {};
  const bot = d.bot || {};
  const lock = bot.lock;
  $('#opsStatus').innerHTML = [
    kv('机器人进程', bot.confirmed
      ? `<span class="tag ok">已确认运行 PID ${(bot.pids || []).join(', ')}</span>`
      : (bot.probable
        ? '<span class="tag warn">很可能在运行（扫描被拦，按锁文件推断）</span>'
        : (bot.scan_available ? '<span class="tag bad">未运行</span>' : '<span class="tag warn">无法确认（扫描被拦）</span>'))),
    kv('判定依据', esc(bot.detection || '')),
    kv('jianer.lock', bot.stale_lock
      ? `<span class="tag bad">残留（PID ${esc(String(lock && lock.pid || '?'))}，需清理）</span>`
      : (lock && lock.alive
        ? `PID ${esc(String(lock.pid))} · ${esc(lock.name || '')} · ${esc(lock.started_str || '')}`
        : '无占用')),
    kv('锁文件时间', esc(bot.lock_mtime_str || '—')),
    kv('OneBot 端口', `${bot.onebot_port} · ${bot.onebot_open ? '<span class="tag ok">可连接</span>' : '<span class="tag bad">未监听</span>'}`),
    kv('上报端口 5003', bot.listener_port_open ? '<span class="tag ok">已监听</span>' : '<span class="tag warn">未监听</span>'),
    kv('控制台使用的 Python', esc(d.python_for_bot || '')),
  ].join('');
}

async function refreshLogs() {
  try {
    const r = await api('/api/logs');
    S.logs = r.logs || [];
    const sel = $('#logSelect');
    const cur = S.logName || (S.logs[0] && S.logs[0].name);
    sel.innerHTML = S.logs.map(l => `<option value="${esc(l.name)}"${l.name === cur ? ' selected' : ''}>${esc(l.name)} · ${esc(l.mtime_str)}</option>`).join('');
    if (!S.logs.length) { $('#logBox').textContent = '（还没有日志。用上面的「启动机器人」启动后，输出会写进这里）'; S.logName = null; return; }
    S.logName = cur;
    const log = await api('/api/logs/' + encodeURIComponent(cur) + '?tail=400');
    const box = $('#logBox');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
    box.textContent = (log.data.lines || []).join('\n') || '（空文件）';
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) { /* 静默 */ }
}

function renderBackups() {
  const box = $('#backupList');
  if (!S.backups.length) { box.innerHTML = '<div class="hint" style="padding:10px">还没有备份（第一次保存后就会出现）</div>'; return; }
  box.innerHTML = '';
  S.backups.forEach(b => {
    const el = document.createElement('div');
    el.className = 'item';
    el.innerHTML = `<div class="grow"><div class="name">${esc(b.target)}</div>
      <div class="sub">${esc(b.name)} · ${(b.size / 1024).toFixed(2)} KB · ${esc(b.mtime_str)}</div></div>`;
    const btn = document.createElement('button');
    btn.className = 'btn small'; btn.textContent = '还原';
    btn.addEventListener('click', async () => {
      if (!confirm(`把 ${b.target} 还原成 ${b.mtime_str} 的版本？\n当前文件会先被备份。`)) return;
      try {
        loading(true);
        const r = await api('/api/backups/restore', { method: 'POST', body: JSON.stringify({ name: b.name }) });
        toast('已还原 ' + r.target, 'ok');
        await reloadAll();
      } catch (e) { toast(e.message, 'err', 6000); }
      finally { loading(false); }
    });
    el.appendChild(btn);
    box.appendChild(el);
  });
}

/* ------------------------------------------------------------------ */
/* 保存                                                                */
/* ------------------------------------------------------------------ */
async function saveConfig(restartMode) {
  if (!S.config) return;
  try {
    loading(true);
    const body = { config: S.config, expected_mtime: S.configMtime };
    if (restartMode) body.restart = restartMode;   // 不传则由后端按「保存行为」设置决定
    let r;
    try {
      r = await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
    } catch (e) {
      if (e.status === 409 && confirm(e.message + '\n\n要用当前页面的内容强制覆盖吗？')) {
        r = await api('/api/config', { method: 'POST', body: JSON.stringify(Object.assign({}, body, { force: true })) });
      } else throw e;
    }
    S.config = r.config;
    S.configMtime = r.meta.mtime;
    markDirty(false);
    toast('已保存到 config.json' + (r.warnings && r.warnings.length ? `（${r.warnings.length} 处已自动规整）` : ''), 'ok');
    if (r.warnings && r.warnings.length) console.warn('规整提示', r.warnings);

    const out = $('#ovSaveOut');
    if (r.restart) {
      const rs = r.restart;
      if (out) out.textContent = '保存结果：\n' + JSON.stringify({ warnings: r.warnings, restart: rs }, null, 2);
      if (rs.skipped) toast(rs.note || '未重启（机器人没在运行）', 'warn', 5000);
      else if (rs.ok) toast('机器人已重启，配置已生效', 'ok', 5000);
      else toast('重启没成功：' + (rs.note || '请看概览里的输出'), 'err', 8000);
    } else if (out) {
      out.textContent = '已保存（未重启）。若机器人正在运行，改动要重启后才生效。';
    }
    await reloadAll();
  } catch (e) { toast(e.message, 'err', 6000); }
  finally { loading(false); }
}

async function saveUsers() {
  try {
    loading(true);
    const r = await api('/api/users', {
      method: 'POST',
      body: JSON.stringify({ data: S.users, expected_mtime: S.usersMtime }),
    });
    toast('名单已保存' + (r.warnings && r.warnings.length ? `（${r.warnings.join('；')}）` : ''), 'ok');
    await loadUsers();
    await loadOverview();
  } catch (e) { toast(e.message, 'err', 6000); }
  finally { loading(false); }
}

async function savePrefs() {
  try {
    loading(true);
    await api('/api/voiceprefs', {
      method: 'POST',
      body: JSON.stringify({ data: { on: S.prefs.on, voice: S.prefs.voice, remove: S.prefsRemove } }),
    });
    S.prefsRemove = [];
    toast('语音开关已保存', 'ok');
    await loadPrefs();
    renderPrefsTable();
  } catch (e) { toast(e.message, 'err', 6000); }
  finally { loading(false); }
}

/* ------------------------------------------------------------------ */
/* 加载                                                                */
/* ------------------------------------------------------------------ */
async function loadOverview() {
  const r = await api('/api/overview');
  S.overview = r.data;
  renderOverview();
  renderOps();
}
async function loadConfig() {
  const r = await api('/api/config');
  S.config = r.config;
  S.configMtime = r.meta.mtime;
  if (!S.config.Others) S.config.Others = {};
  if (!S.config.Others.TTS) S.config.Others.TTS = { voiceColor: '', rate: '+0%', volume: '+0%', pitch: '+0Hz' };
  markDirty(false);
}
async function loadUsers() {
  const r = await api('/api/users');
  S.users = r.data;
  S.usersMtime = r.meta.mtime;
}
async function loadPrefs() {
  const r = await api('/api/voiceprefs');
  S.prefs = r.data;
  S.prefsRemove = [];
}
async function loadVoices(force) {
  const r = await api('/api/voices' + (force ? '?force=1' : ''));
  S.voices = r.data;
}
async function loadBackups() {
  const r = await api('/api/backups');
  S.backups = r.backups || [];
  renderBackups();
}

async function loadSettings() {
  const r = await api('/api/settings');
  S.settings = r.settings || {};
}

/* ---------------- 常驻与开机自启 ---------------- */
async function loadAutostart() {
  const r = await api('/api/autostart');
  S.autostart = r.data;
  renderAutostart();
}

function renderAutostart() {
  const box = $('#f-autostart');
  if (!box || !S.autostart) return;
  const a = S.autostart;
  box.innerHTML = '';

  const sw = document.createElement('label');
  sw.className = 'inline';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!a.enabled;
  cb.addEventListener('change', async () => {
    try {
      loading(true);
      const r = await api('/api/autostart', { method: 'POST', body: JSON.stringify({ enabled: cb.checked }) });
      S.autostart = r.data;
      toast(cb.checked ? '已开启开机自启（开机后静默进托盘）' : '已关闭开机自启', 'ok');
      renderAutostart();
    } catch (e) { toast(e.message, 'err', 7000); cb.checked = !cb.checked; }
    finally { loading(false); }
  });
  sw.appendChild(cb);
  sw.appendChild(document.createTextNode('开机自动启动控制台（托盘常驻、无窗口）'));
  box.appendChild(sw);

  const info = document.createElement('div');
  info.className = 'hint';
  info.innerHTML = a.enabled
    ? `已写入启动项：<code>${esc(a.path)}</code><br>开机后它会调用 <code>${esc(a.launcher)}</code> 静默启动托盘模式。`
    : `开启后会在启动文件夹里放一个 bat：<code>${esc(a.path)}</code><br>
       想彻底移除就关掉这个开关（也可以直接删那个文件）。`;
  box.appendChild(info);

  const row = document.createElement('div');
  row.className = 'row';
  const btnTray = document.createElement('button');
  btnTray.className = 'btn';
  btnTray.textContent = '现在就启动托盘模式';
  btnTray.title = '后台起一份托盘实例；若已经在跑会因端口占用而自动换端口';
  btnTray.addEventListener('click', async () => {
    try {
      loading(true);
      const r = await api('/api/bot/tray', { method: 'POST', body: JSON.stringify({ open: false }) });
      toast(r.note || '已启动托盘实例', 'ok', 6000);
    } catch (e) { toast(e.message, 'err', 7000); }
    finally { loading(false); }
  });
  row.appendChild(btnTray);

  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.textContent = '当前这份前台实例不会被关掉；想只留托盘，先关掉这个窗口即可。';
  row.appendChild(hint);
  box.appendChild(row);
}

/* ---------------- 字段审计 ---------------- */
async function loadAudit() {
  const r = await api('/api/field-audit');
  S.audit = r.data;
  renderAudit();
}

function renderAudit() {
  const a = S.audit;
  const box = $('#ovAudit');
  if (!a || !box) return;
  const chips = (arr, cls) => (arr && arr.length)
    ? arr.map(k => `<span class="tag ${cls}">${esc(k)}</span>`).join(' ')
    : '<span class="hint">无</span>';
  const missing = (a.others.missing_in_ui || []).concat(a.top_level.missing_in_ui || []);
  const evidence = missing.map(k => {
    const hits = (a.others.read_by_code[k] || a.top_level.read_by_code[k] || []).slice(0, 2);
    return hits.map(h => `<div class="hint mono">${esc(k)} ← ${esc(h.file)}:${h.line}</div>`).join('');
  }).join('');

  const modelsRead = ('models' in (a.top_level.read_by_code || {})) || ('models' in (a.others.read_by_code || {}));

  box.innerHTML = `
    <div class="kv"><b>代码在用但界面没有 🆕</b><span>${chips(missing, 'bad')}</span></div>
    ${evidence ? `<div style="margin:4px 0 8px">${evidence}</div>` : ''}
    <div class="kv"><b>界面有但代码没读 ⚠️</b><span>${chips(a.others.ui_but_unread, 'warn')}</span></div>
    <div class="kv"><b>配置里有但代码没读</b><span>${chips(a.others.in_config_but_unread, 'warn')}</span></div>
    <div class="kv"><b>扫描范围</b><span>${a.scanned_files} 个 .py · ${esc(a.generated_at)}</span></div>
    <div class="kv"><b>顶层键说明</b><span class="hint" style="text-align:right">config.json 的顶层键（owner / protocol / Log_level …）
      由 Hyper 框架读取，框架代码在 venv 里不在扫描范围内，所以"顶层 ui_but_unread"属正常现象。</span></div>
    <div class="kv"><b>models 段是否已被代码读取</b><span>${modelsRead
      ? '<span class="tag ok">是</span>' : '<span class="tag warn">还没有</span>'}</span></div>
    <p class="hint">${esc(a.models_note)}</p>
    <details style="margin-top:8px"><summary class="hint">展开：代码读取的全部配置键（含出处）</summary>
      <div class="scroll-box" style="max-height:260px;margin-top:6px">${
        Object.keys(a.others.read_by_code).sort().map(k => {
          const hits = a.others.read_by_code[k];
          return `<div class="voice"><span class="mono">Others.${esc(k)}</span>
            <span class="tag">${esc(hits[0] ? hits[0].file + ':' + hits[0].line : '')}</span></div>`;
        }).join('')}</div></details>`;
}

/* ---------------- 模型注册表 ---------------- */
function seedModels() {
  const o = S.config.Others || {};
  return {
    providers: {
      relay: { type: 'openai', base_url: o.cloud_base_url || 'https://api.deepseek.com/v1', key_env: 'JIANER_API_KEY', extra_headers: o.cloud_extra_headers || {} },
      local: { type: 'openai', base_url: o.local_base_url || 'http://localhost:1234/v1', key_env: '', extra_headers: {} },
      gemini: { type: 'gemini', base_url: o.gemini_base_url || 'https://generativelanguage.googleapis.com/', key_env: 'GEMINI_API_KEY', extra_headers: {} },
    },
    registry: [
      { id: 'ds-chat', provider: 'relay', model: o.cloud_model || 'deepseek-chat', label: '云端默认', tags: ['通用'], aliases: ['ds', 'deepseek'], reasoning: false },
      { id: 'local-default', provider: 'local', model: o.local_model || 'google/gemma-3-4b', label: '本地默认', tags: ['离线', '免费'], aliases: ['local', '本地'], reasoning: false },
      { id: 'gemini-flash', provider: 'gemini', model: o.gemini_model || 'gemini-2.0-flash-exp', label: 'Gemini 多模态', tags: ['看图'], aliases: ['gemini'], reasoning: false },
    ],
    default: 'ds-chat',
    fallback_chain: ['ds-chat', 'local-default'],
  };
}

function renderModels() {
  const banner = $('#modelsBanner');
  const m = (S.config.Others || {}).models;
  const audit = S.audit;
  const readByCode = !!audit && (('models' in (audit.top_level.read_by_code || {}))
    || ('models' in (audit.others.read_by_code || {})));

  if (!m) {
    banner.innerHTML = `<div class="alerts"><div class="alert warn">
      还没有 <code>models</code> 段。点「从当前配置生成初始注册表」会按你现在的
      cloud/local/gemini 设置生成一份（不会改动上面的旧字段，两者并存）。</div></div>`;
    $('#modelsProviders').innerHTML = '<div class="hint">（未创建）</div>';
    $('#modelsRegistry').innerHTML = '<div class="hint">（未创建）</div>';
    $('#modelsDefaults').innerHTML = '<div class="hint">（未创建）</div>';
    return;
  }

  banner.innerHTML = `<div class="alerts"><div class="alert ${readByCode ? 'ok' : 'warn'}">
    ${readByCode
      ? '✅ 机器人代码已经在读 <code>models</code> 段了，这里改完保存 + 重启即生效。'
      : '⚠️ 机器人代码目前<b>还没有</b>读取 <code>models</code> 段（需求⑤开发中）。现在配置不会报错，但也不会有行为变化；等代码接上就直接生效。'}
    </div></div>`;

  const provIds = Object.keys(m.providers || {});

  /* 供应商 */
  const pbox = $('#modelsProviders');
  pbox.innerHTML = '';
  const pgrid = document.createElement('div');
  pgrid.className = 'field';
  provIds.forEach(pid => {
    const p = m.providers[pid] || {};
    const wrap = document.createElement('div');
    wrap.className = 'listrow';
    const mk = (val, ph, w) => {
      const i = document.createElement('input');
      i.className = 'input mono'; i.value = val == null ? '' : val; i.placeholder = ph || '';
      if (w) i.style.maxWidth = w;
      return i;
    };
    const idIn = mk(pid, 'id', '150px'); idIn.disabled = true;
    const typeSel = document.createElement('select');
    typeSel.style.maxWidth = '130px';
    ['openai', 'gemini', 'anthropic'].forEach(t => {
      const o = document.createElement('option'); o.value = t; o.textContent = t;
      if ((p.type || 'openai') === t) o.selected = true;
      typeSel.appendChild(o);
    });
    typeSel.addEventListener('change', () => { p.type = typeSel.value; markDirty(); });
    const baseIn = mk(p.base_url, 'base_url');
    baseIn.addEventListener('input', () => { p.base_url = baseIn.value; markDirty(); });
    const envIn = mk(p.key_env, 'key 环境变量名', '190px');
    envIn.addEventListener('input', () => { p.key_env = envIn.value; markDirty(); });
    const del = document.createElement('button');
    del.className = 'btn small danger'; del.textContent = '删除';
    del.addEventListener('click', () => {
      if (!confirm(`删除供应商 ${pid}？引用它的模型也会一并删掉。`)) return;
      delete m.providers[pid];
      m.registry = (m.registry || []).filter(x => x.provider !== pid);
      m.fallback_chain = (m.fallback_chain || []).filter(x => m.registry.some(r => r.id === x));
      if (!m.registry.some(r => r.id === m.default)) m.default = m.registry[0] ? m.registry[0].id : '';
      markDirty(); renderModels();
    });
    wrap.appendChild(idIn); wrap.appendChild(typeSel); wrap.appendChild(baseIn);
    wrap.appendChild(envIn); wrap.appendChild(del);
    pgrid.appendChild(wrap);
  });
  if (!provIds.length) pgrid.innerHTML = '<div class="hint">还没有供应商</div>';
  pbox.appendChild(pgrid);

  /* 模型 */
  const rbox = $('#modelsRegistry');
  rbox.innerHTML = '';
  const rgrid = document.createElement('div');
  rgrid.className = 'field';
  (m.registry || []).forEach(item => {
    const wrap = document.createElement('div');
    wrap.className = 'listrow';
    const mk = (val, ph, w) => {
      const i = document.createElement('input');
      i.className = 'input mono'; i.value = val == null ? '' : val; i.placeholder = ph || '';
      if (w) i.style.maxWidth = w;
      return i;
    };
    const idIn = mk(item.id, 'id', '150px');
    idIn.addEventListener('input', () => {
      const old = item.id; item.id = idIn.value.trim();
      (m.fallback_chain || []).forEach((x, i) => { if (x === old) m.fallback_chain[i] = item.id; });
      if (m.default === old) m.default = item.id;
      markDirty();
    });
    const provSel = document.createElement('select');
    provSel.style.maxWidth = '150px';
    provIds.forEach(pid => {
      const o = document.createElement('option'); o.value = pid; o.textContent = pid;
      if (item.provider === pid) o.selected = true;
      provSel.appendChild(o);
    });
    provSel.addEventListener('change', () => { item.provider = provSel.value; markDirty(); });
    const modelIn = mk(item.model, '模型名');
    modelIn.addEventListener('input', () => { item.model = modelIn.value; markDirty(); });
    const labelIn = mk(item.label, '显示名', '150px');
    labelIn.addEventListener('input', () => { item.label = labelIn.value; markDirty(); });
    const tagIn = mk((item.tags || []).join(' '), '标签', '130px');
    tagIn.addEventListener('input', () => { item.tags = tagIn.value.split(/[\s,，]+/).filter(Boolean); markDirty(); });
    const aliasIn = mk((item.aliases || []).join(' '), '别名', '130px');
    aliasIn.addEventListener('input', () => { item.aliases = aliasIn.value.split(/[\s,，]+/).filter(Boolean); markDirty(); });
    const reason = document.createElement('label');
    reason.className = 'inline';
    const rc = document.createElement('input');
    rc.type = 'checkbox'; rc.checked = !!item.reasoning;
    rc.addEventListener('change', () => { item.reasoning = rc.checked; markDirty(); });
    reason.appendChild(rc); reason.appendChild(document.createTextNode('推理'));
    const del = document.createElement('button');
    del.className = 'btn small danger'; del.textContent = '删除';
    del.addEventListener('click', () => {
      m.registry = m.registry.filter(x => x !== item);
      m.fallback_chain = (m.fallback_chain || []).filter(x => x !== item.id);
      if (m.default === item.id) m.default = m.registry[0] ? m.registry[0].id : '';
      markDirty(); renderModels();
    });
    [idIn, provSel, modelIn, labelIn, tagIn, aliasIn, reason, del].forEach(el => wrap.appendChild(el));
    rgrid.appendChild(wrap);
  });
  if (!(m.registry || []).length) rgrid.innerHTML = '<div class="hint">还没有模型</div>';
  rbox.appendChild(rgrid);

  /* 默认与降级 */
  const dbox = $('#modelsDefaults');
  dbox.innerHTML = '';
  const d1 = document.createElement('div');
  d1.className = 'field';
  d1.innerHTML = '<span class="label">全局默认模型 models.default</span>';
  const dsel = document.createElement('select');
  ['', ...(m.registry || []).map(x => x.id)].forEach(id => {
    const o = document.createElement('option'); o.value = id; o.textContent = id || '（未设置）';
    if ((m.default || '') === id) o.selected = true;
    dsel.appendChild(o);
  });
  dsel.addEventListener('change', () => { m.default = dsel.value; markDirty(); });
  d1.appendChild(dsel);
  dbox.appendChild(d1);

  const d2 = document.createElement('div');
  d2.className = 'field';
  d2.innerHTML = '<span class="label">自动降级顺序 fallback_chain（从上到下依次尝试）</span>';
  const chain = m.fallback_chain || (m.fallback_chain = []);
  chain.forEach((id, idx) => {
    const row = document.createElement('div');
    row.className = 'listrow';
    const span = document.createElement('span');
    span.className = 'tag'; span.textContent = `${idx + 1}. ${id || '(空)'}`;
    const up = document.createElement('button');
    up.className = 'btn small ghost'; up.textContent = '↑';
    up.addEventListener('click', () => {
      if (idx === 0) return;
      [chain[idx - 1], chain[idx]] = [chain[idx], chain[idx - 1]];
      markDirty(); renderModels();
    });
    const down = document.createElement('button');
    down.className = 'btn small ghost'; down.textContent = '↓';
    down.addEventListener('click', () => {
      if (idx >= chain.length - 1) return;
      [chain[idx + 1], chain[idx]] = [chain[idx], chain[idx + 1]];
      markDirty(); renderModels();
    });
    const del = document.createElement('button');
    del.className = 'btn small danger'; del.textContent = '移除';
    del.addEventListener('click', () => { chain.splice(idx, 1); markDirty(); renderModels(); });
    row.appendChild(span); row.appendChild(up); row.appendChild(down); row.appendChild(del);
    d2.appendChild(row);
  });
  const addRow = document.createElement('div');
  addRow.className = 'row';
  const addSel = document.createElement('select');
  addSel.style.maxWidth = '260px';
  (m.registry || []).forEach(x => {
    const o = document.createElement('option'); o.value = x.id; o.textContent = x.id;
    addSel.appendChild(o);
  });
  const addBtn = document.createElement('button');
  addBtn.className = 'btn small'; addBtn.textContent = '加入降级链';
  addBtn.addEventListener('click', () => {
    if (addSel.value && !chain.includes(addSel.value)) { chain.push(addSel.value); markDirty(); renderModels(); }
  });
  addRow.appendChild(addSel); addRow.appendChild(addBtn);
  d2.appendChild(addRow);
  dbox.appendChild(d2);
}

/* ---------------- 控制台自身设置（保存行为） ---------------- */
function renderConsoleSettings() {
  const box = $('#f-console-settings');
  if (!box || !S.settings) return;
  box.innerHTML = '';

  const save = async (patch) => {
    try {
      const r = await api('/api/settings', { method: 'POST', body: JSON.stringify({ settings: patch }) });
      S.settings = r.settings;
      toast('保存行为已更新', 'ok');
      renderConsoleSettings();
    } catch (e) { toast(e.message, 'err'); }
  };

  const sw = document.createElement('label');
  sw.className = 'inline';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!S.settings.auto_restart;
  cb.addEventListener('change', () => save({ auto_restart: cb.checked }));
  sw.appendChild(cb);
  sw.appendChild(document.createTextNode('保存配置后自动重启机器人（一键生效）'));
  const w1 = document.createElement('div');
  w1.className = 'field';
  w1.appendChild(sw);
  const h1 = document.createElement('span');
  h1.className = 'hint';
  h1.textContent = '只在机器人本来就在运行时重启；它没在跑的话就只保存。重启期间群里会短暂无响应（约 5~15 秒）。';
  w1.appendChild(h1);
  box.appendChild(w1);

  const w2 = document.createElement('div');
  w2.className = 'field';
  const lab = document.createElement('span');
  lab.className = 'label';
  lab.textContent = '自动重启的触发方式';
  w2.appendChild(lab);
  const sel = document.createElement('select');
  [['auto', '仅当机器人在运行时重启（推荐，安全）'],
   ['force', '总是重启（没在跑就启动它）']].forEach(([v, t]) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = t;
    if ((S.settings.restart_mode || 'auto') === v) o.selected = true;
    sel.appendChild(o);
  });
  sel.addEventListener('change', () => save({ restart_mode: sel.value }));
  w2.appendChild(sel);
  box.appendChild(w2);

  const row = document.createElement('div');
  row.className = 'row';
  const btn = document.createElement('button');
  btn.className = 'btn';
  btn.textContent = '立即重启机器人';
  btn.addEventListener('click', () => botAction('bot-restart', btn));
  row.appendChild(btn);
  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.textContent = '等价于「停止 main.py 再启动」，NapCat 不动。正在聊天的用户上下文会重置。';
  row.appendChild(hint);
  box.appendChild(row);
}

async function reloadAll() {
  await Promise.all([loadOverview(), loadConfig(), loadUsers(), loadPrefs(), loadSettings()]);
  renderFields();
  renderKeyStatus();
  renderConsoleSettings();
  renderModels();
  loadAutostart().catch(() => { const b = $('#f-autostart'); if (b) b.textContent = '自启状态读取失败'; });
  loadAudit().catch(e => { const b = $('#ovAudit'); if (b) b.textContent = '审计失败：' + e.message; });
  await Promise.all([loadPresetList(), loadBackups(), loadVoices(false)]);
  renderVoiceUI();
  renderPrefsTable();
  renderOps();
}

/* ------------------------------------------------------------------ */
/* 事件绑定                                                            */
/* ------------------------------------------------------------------ */
function bindGlobal() {
  $('#btnReload').addEventListener('click', async () => {
    if (S.dirty && !confirm('有未保存的修改，确定要放弃并重新读取吗？')) return;
    try { loading(true); await reloadAll(); toast('已重新读取', 'ok'); }
    catch (e) { toast(e.message, 'err'); }
    finally { loading(false); }
  });
  $('#btnSaveAll').addEventListener('click', () => saveConfig());
  $$('[data-save]').forEach(b => b.addEventListener('click', () => saveConfig()));
  $$('[data-save-restart]').forEach(b => b.addEventListener('click', () => saveConfig('force')));
  $$('[data-save-users]').forEach(b => b.addEventListener('click', saveUsers));
  $$('[data-save-prefs]').forEach(b => b.addEventListener('click', savePrefs));

  $$('[data-act]').forEach(b => b.addEventListener('click', () => botAction(b.dataset.act, b)));
  $$('[data-test]').forEach(b => b.addEventListener('click', async () => {
    const kind = b.dataset.test;
    const out = $('#aiTestOut');
    out.textContent = `正在测试 ${kind} …`;
    try {
      const r = await api('/api/model/test', { method: 'POST', body: JSON.stringify({ kind }) });
      const d = r.data;
      out.textContent = `${d.ok ? '✅ 连通' : '❌ 不通'}\nURL: ${d.url}\nHTTP: ${d.status}\n${d.detail || ''}`;
    } catch (e) { out.textContent = '❌ ' + e.message; }
  }));

  $('#btnPreview').addEventListener('click', async () => {
    const tts = S.config.Others.TTS || {};
    const pt = $('#previewText');
    const body = {
      text: pt ? pt.value : '',
      voice: (S.previewVoice && S.previewVoice.value) || tts.voiceColor || '',
      rate: tts.rate || '+0%', volume: tts.volume || '+0%', pitch: tts.pitch || '+0Hz',
    };
    const box = $('#voicePreviewBox');
    box.innerHTML = '<p class="hint">正在合成…（首次调用 edge-tts 可能慢几秒）</p>';
    try {
      const r = await api('/api/tts/preview', { method: 'POST', body: JSON.stringify(body) });
      box.innerHTML = `<audio controls autoplay src="${esc(r.url)}" style="width:100%;margin-top:8px"></audio>`;
    } catch (e) { box.innerHTML = `<p class="hint" style="color:var(--err)">合成失败：${esc(e.message)}</p>`; }
  });
  $('#btnVoicesRefresh').addEventListener('click', async () => {
    try { loading(true); await loadVoices(true); renderVoiceUI(); renderPrefsTable(); toast('声线清单已刷新', 'ok'); }
    catch (e) { toast(e.message, 'err'); } finally { loading(false); }
  });

  $('#btnNewPreset').addEventListener('click', () => {
    S.current = null; S.originalId = null;
    $('#presetId').value = 'p' + Date.now().toString().slice(-8);
    $('#presetName').value = ''; $('#presetInfo').value = ''; $('#presetUid').value = '';
    $('#presetPath').value = ''; $('#presetContent').value = '';
    $('#presetEditorTitle').textContent = '新建预设';
    $('#presetFileTag').textContent = ''; $('#presetDirty').textContent = '未保存';
    renderPresets();
  });
  $('#btnPresetSave').addEventListener('click', savePreset);
  $('#btnPresetDelete').addEventListener('click', deletePreset);
  $('#presetContent').addEventListener('input', () => { S.presetDirty = true; $('#presetDirty').textContent = '未保存'; });

  $('#btnAddPref').addEventListener('click', () => {
    const k = $('#newPrefKey').value.trim();
    if (!/^[ug]\d+$/.test(k)) { toast('格式应为 u123 或 g456', 'warn'); return; }
    S.prefs.on[k] = S.prefs.on[k] || false;
    if (!(k in S.prefs.voice)) S.prefs.voice[k] = '';
    $('#newPrefKey').value = '';
    renderPrefsTable();
  });
  $('#btnCleanPref').addEventListener('click', async () => {
    const keys = Array.from(new Set(Object.keys(S.prefs.on).concat(Object.keys(S.prefs.voice))));
    if (!keys.length) return;
    if (!confirm(`清掉全部 ${keys.length} 条会话记录？\n（只删 voice_prefs.json 里的会话键，全局设置不动）`)) return;
    S.prefsRemove = S.prefsRemove.concat(keys);
    S.prefs.on = {}; S.prefs.voice = {};
    renderPrefsTable();
    toast('已清空（记得点保存）', 'warn');
  });

  $('#btnStatusRefresh').addEventListener('click', async () => {
    try { loading(true); await loadOverview(); toast('状态已刷新', 'ok'); } catch (e) { toast(e.message, 'err'); } finally { loading(false); }
  });
  $('#btnBackupRefresh').addEventListener('click', async () => {
    try { loading(true); await loadBackups(); } catch (e) { toast(e.message, 'err'); } finally { loading(false); }
  });

  // ---- 模型注册表 ----
  $('#btnModelsSeed').addEventListener('click', () => {
    if (S.config.Others.models && !confirm('已经有 models 段了，覆盖成按当前配置生成的版本？')) return;
    S.config.Others.models = seedModels();
    markDirty();
    renderModels();
    toast('已生成初始注册表（记得点保存）', 'ok');
  });
  $('#btnModelsAddProvider').addEventListener('click', () => {
    if (!S.config.Others.models) { S.config.Others.models = seedModels(); }
    const id = prompt('新供应商 id（英文，例如 silicon）');
    if (!id) return;
    S.config.Others.models.providers[id.trim()] = { type: 'openai', base_url: '', key_env: '', extra_headers: {} };
    markDirty(); renderModels();
  });
  $('#btnModelsAddModel').addEventListener('click', () => {
    if (!S.config.Others.models) { S.config.Others.models = seedModels(); }
    const prov = Object.keys(S.config.Others.models.providers)[0];
    if (!prov) { toast('先加一个供应商', 'warn'); return; }
    const id = prompt('新模型 id（英文，例如 sf-qwen）');
    if (!id) return;
    S.config.Others.models.registry.push({ id: id.trim(), provider: prov, model: '', label: id.trim(), tags: [], aliases: [], reasoning: false });
    markDirty(); renderModels();
  });
  $('#btnModelsClear').addEventListener('click', () => {
    if (!S.config.Others.models) return;
    if (!confirm('从 config.json 里删除整个 models 段？（保存后生效，可回滚）')) return;
    delete S.config.Others.models;
    markDirty(); renderModels();
    toast('已移除（记得点保存）', 'warn');
  });
  $('#btnAuditRefresh').addEventListener('click', async () => {
    try { loading(true); await loadAudit(); toast('已重新扫描', 'ok'); }
    catch (e) { toast(e.message, 'err'); } finally { loading(false); }
  });
  $('#logSelect').addEventListener('change', () => { S.logName = $('#logSelect').value; refreshLogs(); });
  $('#btnLogRefresh').addEventListener('click', refreshLogs);
  $('#logAuto').addEventListener('change', () => {
    if ($('#logAuto').checked) startLogTimer(); else stopLogTimer();
  });

  window.addEventListener('beforeunload', (e) => {
    if (S.dirty) { e.preventDefault(); e.returnValue = ''; }
  });
}

function startLogTimer() {
  stopLogTimer();
  S.logTimer = setInterval(() => {
    const panel = $('.panel[data-panel="ops"]');
    if (panel && panel.classList.contains('active') && $('#logAuto').checked) refreshLogs();
  }, 3000);
}
function stopLogTimer() { if (S.logTimer) clearInterval(S.logTimer); S.logTimer = null; }

async function botAction(act, btn) {
  const out = $('#ovOpOut');
  const outs = $('#opsOut');
  const show = (t) => { out.textContent = t; outs.textContent = t; };
  try {
    if (act === 'bot-start' || act === 'bot-all') {
      if (!confirm(act === 'bot-all'
        ? '启动一键脚本（会拉起 NapCat，可能弹出扫码窗口）？'
        : '启动机器人主程序（main.py）？')) return;
      loading(true);
      const r = await api('/api/bot/start', { method: 'POST', body: JSON.stringify({ mode: act === 'bot-all' ? 'all' : 'bot' }) });
      show(JSON.stringify(r, null, 2));
      toast('启动指令已发出', 'ok');
    } else if (act === 'bot-stop') {
      if (!confirm('确定要停止机器人进程吗？（不影响 NapCat）')) return;
      loading(true);
      const r = await api('/api/bot/stop', { method: 'POST', body: JSON.stringify({ force: false }) });
      show(JSON.stringify(r, null, 2));
      toast(r.stopped && r.stopped.length ? '已停止' : (r.ok ? '没有需要停止的进程' : '未执行停止'),
        r.stopped && r.stopped.length ? 'ok' : 'warn', 5000);
    } else if (act === 'bot-clear-lock') {
      const bot = (S.overview && S.overview.bot) || {};
      const fromBtn = btn && btn.dataset ? btn.dataset.force === '1' : false;
      const force = fromBtn || !bot.scan_available;
      const msg = '清理 jianer.lock 锁文件？\n\n'
        + '• 只有在你确认机器人「没有」在运行时才应该清理；\n'
        + '• 锁文件会先备份到 fox-console/backups/；\n'
        + '• 不会结束任何进程。\n\n'
        + (force ? '（当前判断为「很可能在运行」或扫描不可用，将使用「强行清理」）' : '');
      if (!confirm(msg)) return;
      loading(true);
      const r = await api('/api/bot/clear-lock', { method: 'POST', body: JSON.stringify({ force }) });
      show(JSON.stringify(r, null, 2));
      toast(r.note || '已清理', 'ok');
    } else if (act === 'bot-restart') {
      if (!confirm('重启 = 先停止 main.py，再重新启动。NapCat 不动。继续？')) return;
      loading(true);
      const rs = await api('/api/bot/restart', { method: 'POST', body: JSON.stringify({ mode: 'bot' }) });
      show(JSON.stringify(rs, null, 2));
      if (rs.ok) toast('机器人已重启' + (rs.started && rs.started.waited ? `（${rs.started.waited}s）` : ''), 'ok', 5000);
      else toast('重启未完成：' + (rs.note || '请看输出'), 'err', 8000);
    }
    await loadOverview();
  } catch (e) {
    show('❌ ' + e.message);
    toast(e.message, 'err', 6000);
  } finally { loading(false); }
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
async function boot() {
  renderTabs();
  presetForm();
  bindGlobal();
  try {
    const h = await api('/api/health');
    if (h.auth_required && !h.authorized) {
      toast('未授权：请用控制台窗口里打印的带 token 地址打开本页面', 'err', 8000);
      return;
    }
    const m = await api('/api/meta');
    S.meta = m;
    await reloadAll();
    startLogTimer();
    toast('已连接控制台', 'ok');
  } catch (e) {
    toast('初始化失败：' + e.message, 'err', 8000);
    console.error(e);
  }
}

document.addEventListener('DOMContentLoaded', boot);
