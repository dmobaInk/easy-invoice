'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const money = value => Number(value || 0).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});
const dateText = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '—';
const state = {user:null, csrf:'', invoicePage:1, printPage:1, filters:{}, recommendation:null, adminTab:'users', adminPage:1};
const labels = {active:'可用',pending:'待确认',reimbursed:'已报销',excluded:'已排除'};
const paths = {invoice:'M5 3h14v18l-3-2-4 2-4-2-3 2V3zm4 5h6m-6 4h6m-6 4h3',titles:'M4 4h16v16H4z M8 8h8m-8 4h8m-8 4h4',assist:'M12 3v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6 5.6 18.4',print:'M7 8V3h10v5M7 17H4V8h16v9h-3M7 14h10v7H7z',settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2',admin:'M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4zm-4 9 3 3 5-6',logout:'M9 4H4v16h5m4-12 4 4-4 4m-5-4h13',plus:'M12 5v14M5 12h14',check:'m5 12 4 4L19 6'};
const icon = (name='invoice') => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.invoice}"/></svg>`;
const badge = status => `<span class="badge ${escapeHTML(status)}">${escapeHTML(labels[status] || status)}</span>`;
const brand = `<div class="brand"><span class="brand-mark">${icon('invoice')}</span><div><strong>发票小助手</strong><small>INVOICE DESK</small></div></div>`;
let renderVersion = 0, toastTimer;

function resetWorkspace() {
  Object.assign(state,{invoicePage:1,printPage:1,filters:{},recommendation:null,adminTab:'users',adminPage:1,adminFilter:'',logSource:'web'});
}

function toast(message, error=false) {
  clearTimeout(toastTimer);
  const target = $('#toast');
  target.textContent = message;
  target.className = `toast${error ? ' error' : ''}`;
  target.hidden = false;
  toastTimer = setTimeout(() => {target.hidden = true;}, error ? 6500 : 4000);
}

function errorText(detail) {
  if (!Array.isArray(detail)) return typeof detail === 'string' ? detail : '操作未完成，请重试';
  const names = {password:'密码',username:'用户名',name:'显示名称',new_password:'新密码',target_amount:'目标金额',amount:'金额',invoice_ids:'所选发票',title_name:'抬头'};
  return detail.map(item => `${names[item.loc?.at(-1)] || '输入内容'}：${item.msg}`).join('；');
}

async function api(path, options={}) {
  const headers = {'X-CSRF-Token':state.csrf, ...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {...options, headers, credentials:'same-origin'});
  if (response.ok && response.headers.get('Content-Type')?.includes('application/x-ndjson')) return response;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && state.user) {
      state.user = null;
      resetWorkspace();
      const session = await fetch('/api/auth/session', {credentials:'same-origin'}).then(r => r.json());
      state.csrf = session.csrf_token;
      renderAuth();
    }
    throw new Error(errorText(data.detail) || `请求失败（${response.status}）`);
  }
  return data;
}

const post = (path, body) => api(path, {method:'POST', body:JSON.stringify(body)});
const heading = (title, subtitle, action='') => `<div class="page-heading"><div><div class="eyebrow">YOUR INVOICE WORKSPACE</div><h1>${title}</h1><p>${subtitle}</p></div><div class="actions">${action}</div></div>`;
const empty = (title, subtitle, name='invoice') => `<div class="empty"><div class="empty-symbol">${icon(name)}</div><h3>${title}</h3><p>${subtitle}</p></div>`;

function openForm(title, content, submitLabel, callback) {
  const modal = $('#modal');
  modal.className = '';
  modal.onclose = null;
  modal.innerHTML = `<div class="modal-head"><h2 id="modal-title">${title}</h2><button type="button" class="subtle" data-close aria-label="关闭">✕</button></div><form class="modal-body">${content}<div class="form-error" hidden></div><div class="modal-footer"><button type="button" data-close>取消</button><button class="primary" type="submit">${submitLabel}</button></div></form>`;
  $$('[data-close]', modal).forEach(button => button.onclick = () => modal.close());
  $('form', modal).onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget, submit = $('[type=submit]', form), error = $('.form-error', form);
    submit.disabled = true; error.hidden = true;
    try {await callback(new FormData(form), form); modal.close();}
    catch (exception) {error.textContent = exception.message; error.hidden = false;}
    finally {submit.disabled = false;}
  };
  modal.showModal();
}

function bindPreviews(rows, admin=false) {
  $$('[data-preview]').forEach(button => {
    button.onclick = () => previewInvoice(rows.find(row => row.id === Number(button.dataset.preview)), admin);
  });
}

function previewInvoice(row, admin=false) {
  const modal = $('#modal');
  const base = `/api/${admin ? 'admin/' : ''}invoices/${row.id}`;
  modal.className = 'preview-modal';
  modal.innerHTML = `<div class="modal-head"><div><h2 id="modal-title">原件预览</h2><p class="muted preview-caption">发票号码：${escapeHTML(row.invoice_number || '待补录')}</p></div><div class="actions"><a class="button primary" href="${base}/file" download>原件下载</a><button type="button" class="subtle" data-preview-close aria-label="关闭预览">✕</button></div></div><div class="preview-toolbar"><button type="button" class="small" data-preview-prev disabled>上一页</button><span class="muted" data-preview-page aria-live="polite">正在读取原件…</span><button type="button" class="small" data-preview-next disabled>下一页</button></div><div class="preview-stage"><p class="preview-message" data-preview-message role="status">正在加载预览…</p><img data-preview-image alt="发票原件" hidden></div><div class="preview-footnote">这里可以翻看完整原件；OCR 识别和 A4 双拼仍只使用第一页。</div>`;
  const image = $('[data-preview-image]', modal), message = $('[data-preview-message]', modal);
  const previous = $('[data-preview-prev]', modal), next = $('[data-preview-next]', modal);
  let currentPage = 1, totalPages = 1, imageUrl = null, controller = null;
  const setControls = loading => {previous.disabled = loading || currentPage <= 1; next.disabled = loading || currentPage >= totalPages;};
  async function loadPage(page) {
    controller?.abort(); controller = new AbortController();
    const activeController = controller;
    setControls(true); message.hidden = false; message.textContent = '正在加载预览…'; image.hidden = true;
    try {
      const response = await fetch(`${base}/preview?page=${page}`, {credentials:'same-origin', signal:activeController.signal});
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(errorText(data.detail));
      }
      const blob = await response.blob();
      if (activeController.signal.aborted || !modal.open) return;
      if (imageUrl) URL.revokeObjectURL(imageUrl);
      imageUrl = URL.createObjectURL(blob); image.src = imageUrl;
      currentPage = page; totalPages = Number(response.headers.get('X-PDF-Page-Count')) || 1;
      image.alt = `发票原件，第 ${currentPage} 页，共 ${totalPages} 页`;
      $('[data-preview-page]', modal).textContent = `第 ${currentPage} / ${totalPages} 页`;
      message.hidden = true; image.hidden = false;
      $('.preview-stage', modal).scrollTop = 0;
    } catch (error) {
      if (error.name === 'AbortError') return;
      message.textContent = `${error.message}，可点击“原件下载”查看完整 PDF。`;
      $('[data-preview-page]', modal).textContent = '预览暂不可用';
    } finally {
      if (controller === activeController && modal.open) setControls(false);
    }
  }
  previous.onclick = () => loadPage(currentPage - 1);
  next.onclick = () => loadPage(currentPage + 1);
  $('[data-preview-close]', modal).onclick = () => modal.close();
  modal.onclose = () => {controller?.abort(); if (imageUrl) URL.revokeObjectURL(imageUrl);};
  modal.showModal(); loadPage(1);
}

function confirmAction(title, message, callback) {
  openForm(title, `<p class="muted">${escapeHTML(message)}</p>`, '确认', async () => {await callback();});
}

function download(url) {
  const link = document.createElement('a'); link.href = url; link.download = ''; document.body.append(link); link.click(); link.remove();
}

async function performLogout() {
  try {
    const result=await post('/api/auth/logout',{});
    state.csrf=result.csrf_token; state.user=null; resetWorkspace();
    history.replaceState({},'','/login'); renderAuth();
  } catch(error) {toast(error.message,true);}
}

function renderAuth(register=false) {
  ++renderVersion;
  $('#app').innerHTML = `<div class="auth-screen"><section class="auth-art">${brand}<div><div class="eyebrow">LESS PAPERWORK. MORE CLARITY.</div><h1>每一张发票，<br>都安排妥当。</h1><p class="auth-desc">把 PDF 发给飞书机器人，其余的交给小助手。<br>收集、归类、报销、打印，在一处有序完成。</p><div class="auth-steps"><span><b>01</b>飞书收集</span><span><b>02</b>智能整理</span><span><b>03</b>轻松报销</span></div></div><footer>A SMALL HELPER FOR EVERY RECEIPT.</footer></section><section class="auth-form-side"><div class="auth-form"><div class="eyebrow">WELCOME TO INVOICE DESK</div><h2>${register ? '创建你的工作台' : '欢迎回来'}</h2><p class="intro">${register ? '注册后，在飞书单聊发送用户名即可关联账号。' : '登录后，继续整理你的发票与报销。'}</p><form id="auth-form"><label>用户名<input name="username" autocomplete="username" maxlength="64" required placeholder="输入你的用户名"></label>${register ? '<label>显示名称<input name="name" autocomplete="nickname" maxlength="100" placeholder="你希望如何被称呼"></label>' : ''}<label>密码<input type="password" name="password" autocomplete="${register ? 'new-password' : 'current-password'}" minlength="${register ? 8 : 1}" maxlength="128" required placeholder="${register ? '至少 8 位字符' : '输入密码'}"></label><div class="form-error" hidden></div><button class="primary" type="submit">${register ? '注册并进入工作台' : '登 录'}</button></form><div class="auth-switch">${register ? '已经有账号？' : '还没有账号？'}<button class="subtle" id="auth-switch">${register ? '立即登录' : '创建账号'}</button></div><p class="auth-note">你的发票、抬头和打印记录按账号独立保存。<br>请使用自己的账号关联飞书机器人。</p></div></section></div>`;
  $('#auth-switch').onclick = () => renderAuth(!register);
  $('#auth-form').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget, button = $('[type=submit]', form), error = $('.form-error', form);
    button.disabled = true; error.hidden = true;
    try {
      const result = await post(`/api/auth/${register ? 'register' : 'login'}`, Object.fromEntries(new FormData(form)));
      resetWorkspace();
      state.user = result.user; state.csrf = result.csrf_token;
      navigate(location.pathname === '/login' || location.pathname === '/' ? '/invoices' : location.pathname, true);
    } catch (exception) {error.textContent = exception.message; error.hidden = false;}
    finally {button.disabled = false;}
  };
}

const navItems = [['/invoices','invoice','我的发票'],['/titles','titles','发票抬头'],['/reimbursements','assist','报销助手'],['/prints','print','打印记录'],['/settings','settings','账户设置']];

function shell() {
  const user = state.user;
  const navigation = [...navItems, ...(user.role === 'admin' ? [['/admin','admin','管理后台']] : [])];
  $('#app').innerHTML = `<div class="shell"><aside class="sidebar">${brand}<p class="nav-label">工作空间</p><nav class="nav">${navigation.map(([url,name,label]) => `<a href="${url}" data-route>${icon(name)}<span>${label}</span></a>`).join('')}</nav><div class="sidebar-note">少一点繁琐，<br>多一点井井有条。</div><div class="user-panel"><span class="avatar">${escapeHTML((user.name || user.username).slice(0,1))}</span><div><strong>${escapeHTML(user.name || user.username)}</strong><small>${user.role === 'admin' ? '管理员' : '个人工作台'}</small></div><button class="subtle" id="logout" title="退出登录" aria-label="退出登录">${icon('logout')}</button></div></aside><div class="workspace"><header class="topbar"><span>工作台&nbsp; / &nbsp;<strong id="breadcrumb"></strong></span><span><span class="dot"></span>${escapeHTML(new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric'}))}</span></header><main id="main"></main></div></div>`;
  $$('[data-route]').forEach(link => link.onclick = event => {event.preventDefault(); navigate(link.getAttribute('href'));});
  $('#logout').onclick = performLogout;
}

function navigate(path, replace=false) {
  if (replace) history.replaceState({},'',path); else if (location.pathname !== path) history.pushState({},'',path);
  renderRoute();
}

async function renderRoute() {
  if (!state.user) return renderAuth();
  if (!$('#main')) shell();
  const path = location.pathname === '/' || location.pathname === '/login' ? '/invoices' : location.pathname;
  const version = ++renderVersion;
  $$('[data-route]').forEach(link => link.classList.toggle('active', link.getAttribute('href') === path));
  $('#breadcrumb').textContent = [...navItems, ['/admin','admin','管理后台']].find(item => item[0] === path)?.[2] || '工作台';
  $('#main').innerHTML = '<div class="loading">正在整理数据…</div>';
  try {
    const pages = {'/invoices':invoicePage,'/titles':titlePage,'/reimbursements':reimbursementPage,'/prints':printPage,'/settings':settingsPage,'/admin':adminPage};
    const page = await (pages[path] || invoicePage)();
    if (version !== renderVersion || !state.user) return;
    $('#main').innerHTML = page.html;
    page.bind?.();
  } catch (error) {
    if (version !== renderVersion) return;
    $('#main').innerHTML = empty('暂时无法打开', escapeHTML(error.message));
    toast(error.message, true);
  }
}

function pagination(total, page, size) {
  return `<div class="table-footer"><span>共 ${total} 条记录 · 第 ${page} / ${Math.max(1, Math.ceil(total/size))} 页</span><div class="actions"><button class="small" id="prev" ${page <= 1 ? 'disabled' : ''}>上一页</button><button class="small" id="next" ${page*size >= total ? 'disabled' : ''}>下一页</button></div></div>`;
}

function tagField(tags, value = '', listId = 'invoice-tag-choices') {
  const options = tags.map(tag => `<option value="${escapeHTML(tag)}">${escapeHTML(tag)}</option>`).join('');
  return `<div class="tag-field"><label>标签<input name="tag" list="${listId}" maxlength="64" autocomplete="off" placeholder="选择或输入" value="${escapeHTML(value)}"></label><datalist id="${listId}">${options}</datalist><span class="tag-picker">▾<select aria-label="选择常用标签" data-tag-choice><option value="">常用标签</option>${options}</select></span></div>`;
}

document.addEventListener('change', event => {
  const choice = event.target;
  if (!choice.matches('[data-tag-choice]') || !choice.value) return;
  const input = choice.closest('.tag-field').querySelector('input');
  input.value = choice.value;
  choice.selectedIndex = 0;
  input.dispatchEvent(new Event('input', {bubbles:true}));
});

function invoiceSearchFields(filters = {}) {
  const fields = [['seller_name','销售方名称','text'],['amount_min','最低金额','number'],
                  ['amount_max','最高金额','number'],['date_from','起始日期','date'],['date_to','截止日期','date']];
  return fields.map(([name,label,type]) => `<label>${label}<input name="${name}" type="${type}" ${type === 'number' ? 'min="0" step="0.01"' : ''} value="${escapeHTML(filters[name] || '')}"></label>`).join('');
}

async function invoicePage() {
  const parameters = new URLSearchParams({...state.filters, page:state.invoicePage});
  const [data, summary, titles, tags] = await Promise.all([api(`/api/invoices?${parameters}`), api('/api/invoices/summary'), api('/api/titles'), api('/api/invoices/tags')]);
  const options = titles.items.map(item => `<option value="${item.id}" ${String(state.filters.title_id) === String(item.id) ? 'selected' : ''}>${escapeHTML(item.title_name)}</option>`).join('');
  const metrics = `<div class="metrics"><div class="metric primary"><div class="metric-label">可用发票金额 ${icon('invoice')}</div><strong><small>¥</small>${money(summary.active?.amount)}</strong><span class="caption">${summary.active?.count || 0} 张发票，等待下一次报销</span></div><div class="metric"><div class="metric-label">待确认 ${icon('titles')}</div><strong>${summary.pending?.count || 0}<small> 张</small></strong><span class="caption">补全信息后即可参与报销</span></div><div class="metric"><div class="metric-label">已报销金额 ${icon('check')}</div><strong><small>¥</small>${money(summary.reimbursed?.amount)}</strong><span class="caption">${summary.reimbursed?.count || 0} 张发票，已完成报销</span></div><div class="metric"><div class="metric-label">收录发票 ${icon('invoice')}</div><strong>${Object.values(summary).reduce((n,item) => n+item.count,0)}<small> 张</small></strong><span class="caption">所有状态的发票记录</span></div></div>`;
  const rows = data.items.map(row => `<tr><td class="strong">${escapeHTML(row.invoice_number || '等待补录')}<span class="subline">${escapeHTML(row.invoice_type || '待识别发票')}</span></td><td>${escapeHTML(row.invoice_date || '—')}</td><td class="title-cell" title="${escapeHTML(row.title_name || row.buyer_name || '')}">${escapeHTML(row.title_name || row.buyer_name || '未归类')}</td><td>${escapeHTML(row.seller_name || '--')}</td><td class="amount">${row.amount == null ? '—' : '¥ '+money(row.amount)}</td><td>${row.reimbursed_at ? new Date(row.reimbursed_at).toLocaleDateString('zh-CN') : row.status === 'reimbursed' ? '日期未记录' : '--'}</td><td><button class="small" type="button" data-tag="${row.id}">${escapeHTML(row.tags.join('、') || '未分类')}</button></td><td>${badge(row.status)}</td><td><div class="row-actions"><button class="subtle small" data-edit="${row.id}">编辑</button><button class="subtle small" data-preview="${row.id}">预览</button><a class="button small" href="/api/invoices/${row.id}/file" download>原件下载</a><button class="subtle small danger" data-delete-invoice="${row.id}">删除</button></div></td></tr>`).join('');
  return {html:heading('我的发票','从飞书收集，在这里整理。每一张发票，都有清晰的去处。','<button id="upload-button" class="primary">＋ 上传 PDF（可多选）</button><input type="file" id="upload-input" accept="application/pdf,.pdf" multiple hidden>')+metrics+`<section class="panel invoice-list"><div class="panel-head"><h2>发票清单</h2><span class="muted">可将多个 PDF 拖到页面上传</span></div><form class="filters" id="filters"><label>发票抬头<select name="title_id"><option value="">全部抬头</option>${options}</select></label><label>状态<select name="status"><option value="">全部状态</option>${Object.entries(labels).map(([key,value]) => `<option value="${key}" ${state.filters.status === key ? 'selected' : ''}>${value}</option>`).join('')}</select></label>${tagField(tags.items,state.filters.tag)}${invoiceSearchFields(state.filters)}<button class="primary" type="submit">筛选</button><button id="reset-filters" type="button">重置</button></form>${rows ? `<div class="table-wrap"><table><thead><tr><th>发票号码 / 类型</th><th>开票日期</th><th>发票抬头</th><th>销售方名称</th><th>价税合计</th><th>报销日期</th><th>标签</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty('这里还没有发票','在飞书向机器人发送 PDF，或上传 PDF 自动识别并归类。')}${pagination(data.total,state.invoicePage,data.page_size)}</section><div class="helper">${icon('assist')}<span><strong>让飞书帮你收集。</strong> 先在「账户设置」查看绑定方式，再向机器人发送 PDF，识别结果会出现在这里。</span></div>`, bind:() => {
    $$('[data-tag]').forEach(button => button.onclick = () => {
      const row = data.items.find(item => item.id === Number(button.dataset.tag));
      const selected = new Set(row.tags);
      const choices = new Set([...selected, ...tags.items.filter(name => name !== '未分类')]);
      openForm('修改发票标签', `<label>搜索或新建标签<input name="newTag" maxlength="64" autocomplete="off"></label>
        <div class="tag-choices"></div><button type="button" data-add-tag hidden></button>`, '保存', async () => {
        await api(`/api/invoices/${row.id}/tag`, {method:'PUT',body:JSON.stringify({names:[...selected]})});
        if (data.items.length === 1 && state.invoicePage > 1 && state.filters.tag) state.invoicePage--;
        await renderRoute();
      });
      const input = $('#modal [name=newTag]'), list = $('#modal .tag-choices'), add = $('#modal [data-add-tag]');
      const refresh = () => {
        const query = input.value.trim();
        list.innerHTML = [...choices].filter(name => name.toLowerCase().includes(query.toLowerCase())).map(name =>
          `<label class="check"><input type="checkbox" value="${escapeHTML(name)}" ${selected.has(name) ? 'checked' : ''}>${escapeHTML(name)}</label>`).join('');
        add.hidden = !query || choices.has(query) || query === '未分类';
        add.textContent = `添加「${query}」`;
      };
      list.onchange = event => {
        if (event.target.checked) selected.add(event.target.value);
        else selected.delete(event.target.value);
      };
      add.onclick = () => {
        const name = input.value.trim();
        choices.add(name); selected.add(name);
        input.value = ''; refresh(); input.focus();
      };
      input.oninput = refresh;
      input.onkeydown = event => {
        if (event.key === 'Enter' && !event.isComposing) {
          event.preventDefault();
          if (!add.hidden) add.click();
        }
      };
      refresh();
      input.focus();
    });
    $('#filters').onsubmit = event => {event.preventDefault(); state.filters = Object.fromEntries([...new FormData(event.currentTarget)].filter(([,value]) => value !== '')); state.invoicePage = 1; renderRoute();};
    $('#reset-filters').onclick = () => {state.filters = {}; state.invoicePage = 1; renderRoute();};
    $('#prev').onclick = () => {state.invoicePage--; renderRoute();}; $('#next').onclick = () => {state.invoicePage++; renderRoute();};
    bindPreviews(data.items);
    $$('[data-delete-invoice]').forEach(button => button.onclick = () => confirmAction('删除发票','删除后，该发票将从清单中移除，不再参与报销推荐。此操作无法撤销，已导出的 PDF 保留。',async () => {
      await api(`/api/invoices/${button.dataset.deleteInvoice}`,{method:'DELETE'});
      state.recommendation = null;
      if (data.items.length === 1 && state.invoicePage > 1) state.invoicePage--;
      toast('发票已删除'); await renderRoute();
    }));
    $$('[data-edit]').forEach(button => button.onclick = () => editInvoice(data.items.find(row => row.id === Number(button.dataset.edit)), titles.items));
    $('#upload-button').onclick = () => $('#upload-input').click();
    $('#upload-input').onchange = event => {
      const files = [...event.target.files];
      event.target.value = '';
      if (files.length) uploadInvoices(files);
    };
  }};
}

function invoiceFileDrag(event) {
  return $('#upload-input') && event.dataTransfer?.types.includes('Files');
}
document.addEventListener('dragover', event => {
  if (invoiceFileDrag(event)) event.preventDefault();
});
document.addEventListener('drop', event => {
  if (!invoiceFileDrag(event)) return;
  event.preventDefault();
  const files = Array.from(event.dataTransfer.files);
  if (files.length && !$('#modal').open) uploadInvoices(files);
});

async function uploadInvoices(files) {
  const modal = $('#modal');
  modal.className = 'upload-modal';
  modal.onclose = null;
  // Keep the queue visible and prevent starting a second batch while it runs.
  modal.oncancel = event => event.preventDefault();
  modal.innerHTML = `<div class="modal-head"><h2 id="modal-title">批量上传 PDF</h2></div><div class="modal-body"><p class="muted">支持多选，每份不超过 10 MiB。识别不完整的原件会保留，可在清单中修正。</p><p data-upload-progress role="status" aria-live="polite"></p><div class="upload-results">${files.map((file,index) => `<div class="upload-result"><strong>${escapeHTML(file.name)}</strong><span class="muted" data-upload-result="${index}">等待上传</span></div>`).join('')}</div><div class="modal-footer"><button class="primary" data-upload-close disabled>上传中…</button></div></div>`;
  const progress = $('[data-upload-progress]', modal);
  const close = $('[data-upload-close]', modal);
  close.onclick = () => modal.close();
  modal.showModal();
  let success = 0, pending = 0, failed = 0;
  const user = state.user;
  try {
    for (const [index, file] of files.entries()) {
      const result = $(`[data-upload-result="${index}"]`, modal);
      progress.textContent = `正在处理 ${index + 1} / ${files.length}`;
      result.textContent = '上传并识别中…';
      try {
        if (!state.user || state.user !== user) throw new Error('登录状态已变化，请重新登录后上传');
        if (!/\.pdf$/i.test(file.name)) throw new Error('仅支持 PDF 文件');
        if (file.size > 10*1024*1024) throw new Error('PDF 文件不能超过 10 MiB');
        const body = new FormData(); body.append('file', file);
        const row = await api('/api/invoices/upload', {method:'POST', body});
        if (row.status === 'pending') {
          pending++;
          result.textContent = '待确认：原件已保留，请在清单中修正信息';
        } else {
          success++;
          result.textContent = '成功：已识别并归类';
        }
      } catch (error) {
        failed++;
        result.textContent = `失败：${error.message}`;
      }
    }
    progress.textContent = `处理完成，共 ${files.length} 份：成功 ${success} 份，待确认 ${pending} 份，失败 ${failed} 份。`;
    if ((success || pending) && state.user === user) {
      state.invoicePage = 1;
      state.recommendation = null;
      await renderRoute();
    }
  } finally {
    modal.oncancel = null;
    close.disabled = false;
    close.textContent = '完成';
  }
}

function editInvoice(row, titles) {
  const fields = [['invoice_code','发票代码','text'],['invoice_number','发票号码','text'],['invoice_date','开票日期','date'],['amount','价税合计（元）','number'],['buyer_name','购买方名称','text'],['seller_name','销售方名称','text'],['invoice_type','发票类型','text']];
  openForm('修正发票信息',`<div class="form-grid">${fields.map(([name,label,type]) => `<label>${label}<input name="${name}" type="${type}" ${type === 'number' ? 'step="0.01" min="0"' : ''} value="${escapeHTML(row[name] ?? '')}"></label>`).join('')}<label>状态<select name="status">${Object.entries(labels).map(([key,value]) => `<option value="${key}" ${row.status===key?'selected':''}>${value}</option>`).join('')}</select></label><label class="wide">归类抬头<select name="title_id"><option value="auto">按购买方名称归类</option><option value="">暂不归类</option>${titles.map(title => `<option value="${title.id}" ${row.title_id===title.id?'selected':''}>${escapeHTML(title.title_name)}</option>`).join('')}</select></label></div><p class="muted">补全所有发票信息后，将状态改为「可用」。也可以在这里手动调整是否已报销。</p>`, '保存修改',async form => {
    const values=Object.fromEntries(form); for (const key of Object.keys(values)) if(values[key]==='') values[key]=null;
    if(values.title_id==='auto') delete values.title_id; else if(values.title_id!==null) values.title_id=Number(values.title_id);
    await api(`/api/invoices/${row.id}`,{method:'PUT',body:JSON.stringify(values)}); toast('发票已更新'); await renderRoute();
  });
}

async function titlePage() {
  const data = await api('/api/titles');
  const html = data.items.map(item => `<article class="title-card"><span class="badge">常用抬头</span><h2>${escapeHTML(item.title_name)}</h2><p class="tax">税号：${escapeHTML(item.tax_number || '尚未填写')}</p><div class="title-totals"><div><strong>${item.active_count}</strong><small>可用发票 / 张</small></div><div><strong>¥ ${money(item.active_amount)}</strong><small>可用金额 / 元</small></div></div><div class="actions"><button class="subtle small" data-title="${item.id}">编辑抬头</button><button class="subtle small danger" data-delete="${item.id}">删除</button></div></article>`).join('');
  return {html:heading('发票抬头','为常用的报销抬头留一个位置。','<button class="primary" id="new-title">＋ 新增抬头</button>')+(html?`<div class="cards">${html}</div>`:`<section class="panel">${empty('先添加一个常用抬头','识别成功的发票也会自动按购买方名称归类。','titles')}</section>`), bind:() => {
    $('#new-title').onclick = () => editTitle();
    $$('[data-title]').forEach(button => button.onclick = () => editTitle(data.items.find(item=>item.id===Number(button.dataset.title))));
    $$('[data-delete]').forEach(button => button.onclick = () => confirmAction('删除抬头','已有发票或报销方案关联的抬头不能删除。', async () => {await api(`/api/titles/${button.dataset.delete}`,{method:'DELETE'}); toast('抬头已删除'); await renderRoute();}));
  }};
}

function editTitle(item) {
  openForm(item?'编辑抬头':'新增抬头',`<div class="stack"><label>抬头名称<input name="title_name" maxlength="200" required value="${escapeHTML(item?.title_name || '')}" placeholder="填写公司或个人名称"></label><label>纳税人识别号<input name="tax_number" maxlength="64" value="${escapeHTML(item?.tax_number || '')}" placeholder="选填"></label></div>`, '保存抬头', async form => {
    await api(`/api/titles${item?'/'+item.id:''}`,{method:item?'PUT':'POST',body:JSON.stringify(Object.fromEntries(form))}); toast('抬头已保存'); await renderRoute();
  });
}

async function matchInvoices(payload, panel) {
  const response = await post('/api/reimbursements/optimize?stream=true', payload);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) throw new Error('匹配连接已中断，请重试');
      buffer += decoder.decode(value, {stream:true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        const event = JSON.parse(line);
        if (event.error) throw new Error(event.error);
        if (event.result) return event.result;
        $('progress', panel).max = event.total;
        $('progress', panel).value = event.done;
        $('.match-amount', panel).textContent = '¥ ' + money(event.best_cents / 100);
        $('.match-work', panel).textContent = '剩余 ' + (event.total - event.done) + ' / ' + event.total + ' 张待处理';
        $('.match-eta', panel).textContent = event.eta == null ? '正在估时' : event.eta < 1 ? '预计不足 1 秒' : '预计剩余 ' + Math.ceil(event.eta) + ' 秒';
      }
    }
  } finally {
    reader.releaseLock();
  }
}

async function reimbursementPage() {
  const titles = await api('/api/titles');
  const result = state.recommendation;
  let fixed = new Set(result?.fixed_invoice_ids || []);
  return {html:heading('报销助手','给一个目标金额，找到最合适的发票组合。')+`<section class="panel"><div class="panel-head"><h2>这次，准备报销多少？</h2><span class="muted">仅从所选抬头的可用发票中推荐</span></div><form id="optimize" class="section-body reimburse-form"><label>选择报销抬头<select name="title_id" required><option value="">请选择抬头</option>${titles.items.map(item=>`<option value="${item.id}" ${(result?.title_id ?? titles.items[0]?.id)===item.id?'selected':''}>${escapeHTML(item.title_name)} · ${item.active_count} 张可用</option>`).join('')}</select></label><label>目标金额（元）<input name="target_amount" type="number" min="0.01" max="999999999999.99" step="0.01" required placeholder="例如 1000.00" value="${escapeHTML(result?.target_amount || '')}"></label><div class="actions"><button class="primary" type="submit">推荐发票组合</button><button type="button" id="pick-fixed">选择固定发票</button></div><label class="check"><input name="allow_exceed" type="checkbox" ${(result?.allow_exceed ?? true)?'checked':''}>允许超过目标金额，选择最接近的组合</label></form><div id="match-progress" hidden><div class="match-heading"><i aria-hidden="true"></i><span role="status">正在匹配发票</span></div><small>当前最佳金额</small><strong class="match-amount">计算中</strong><progress aria-label="发票匹配进度"></progress><div class="match-details"><span class="match-work">正在读取可用发票</span><span class="match-eta">正在估时</span></div></div></section><section class="panel"><div class="panel-head"><h2>推荐组合</h2><span class="muted">可取消勾选，按你的实际需要导出</span></div>${result?.invoices?.length ? `${result.message ? `<div class="helper" role="status">${escapeHTML(result.message)}</div>` : ''}<div class="table-wrap"><table><thead><tr><th>选择</th><th>发票号码</th><th>开票日期</th><th>抬头</th><th>金额</th></tr></thead><tbody>${result.invoices.map(row=>`<tr><td><input type="checkbox" data-select="${row.id}" checked ${fixed.has(row.id) ? 'disabled' : ''} aria-label="选择发票 ${escapeHTML(row.invoice_number)}"></td><td class="strong">${escapeHTML(row.invoice_number)}${fixed.has(row.id) ? '（固定）' : ''}</td><td>${escapeHTML(row.invoice_date)}</td><td>${escapeHTML(row.buyer_name)}</td><td class="amount">¥ ${money(row.amount)}</td></tr>`).join('')}</tbody></table></div><div class="result-summary"><div><small id="selection-count"></small><strong id="selection-total"></strong><small id="selection-difference"></small></div><div class="stack"><label class="check"><input id="auto-reimburse" type="checkbox" checked>PDF 导出成功后自动标记为已报销</label><label class="check"><input id="merge-pdf" type="checkbox" checked>合并导出</label><button id="generate-pdf" class="primary">生成A4双拼PDF和清单</button></div></div>` : empty(result?'暂时没有合适的组合':'把凑金额的事交给小助手',result?.message ? escapeHTML(result.message) : '选择抬头和目标金额，推荐结果会显示在这里。','assist')}</section><div class="helper">${icon('print')}<span>合并导出时，每张 A4 上下放置两张发票，只使用原文件第一页；奇数张时，最后一页保留一张。取消合并后，ZIP 内每张发票保留完整原件。</span></div>`,bind:()=>{
    const form = $('#optimize');
    const picker = $('#pick-fixed');
    const title = form.elements.title_id;
    const labelFixed = () => {
      picker.textContent = fixed.size ? `选择固定发票（${fixed.size}）` : '选择固定发票';
    };
    labelFixed();
    const hidePrevious = () => {
      const previous = form.closest('section').nextElementSibling;
      if (result && previous) previous.hidden = true;
    };
    title.onchange = () => { fixed.clear(); labelFixed(); hidePrevious(); };
    picker.onclick = async () => {
      if (!title.reportValidity()) return;
      const selectedTitle = title.value;
      picker.disabled = true;
      try {
        const [{items}, tags] = await Promise.all([api(`/api/reimbursements/fixed-options?title_id=${encodeURIComponent(selectedTitle)}`), api('/api/invoices/tags')]);
        if (!form.isConnected || title.value !== selectedTitle) return;
        const rows = items.map(row => `<tr data-fixed-row><td><input type="checkbox" name="fixed" value="${row.id}" ${fixed.has(row.id) ? 'checked' : ''} aria-label="选择发票 ${escapeHTML(row.invoice_number)}"></td><td>${escapeHTML(row.invoice_number)}</td><td>${escapeHTML(row.seller_name)}</td><td>${escapeHTML(row.invoice_date)}</td><td class="amount">¥ ${money(row.amount)}</td><td>${escapeHTML(row.remarks || '—')}</td><td>${escapeHTML(row.tags.join('、') || '未分类')}</td></tr>`).join('');
        const content = rows ? `<div class="table-wrap"><table><thead><tr><th>选择</th><th>发票号码</th><th>销售方名称</th><th>开票日期</th><th>价税合计</th><th>备注</th><th>标签</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p>该抬头下暂无可用发票。</p>';
        const filters = `<div class="fixed-filters">${tagField(tags.items)}${invoiceSearchFields()}<button type="button" data-clear>重置</button></div>`;
        openForm('选择固定发票', filters + content + '<p data-no-match hidden>没有符合筛选条件的发票。</p>', '确定', async values => {
          const ids = values.getAll('fixed').map(Number);
          if (ids.length > 200) throw new Error('每次最多可导出 200 张发票');
          const chosen = new Set(ids);
          state.recommendation = {
            id:null, title_id:Number(selectedTitle), target_amount:form.elements.target_amount.value,
            allow_exceed:form.elements.allow_exceed.checked, fixed_invoice_ids:ids,
            invoices:items.filter(row => chosen.has(row.id))
          };
          await renderRoute();
        });
        const controls = $('.fixed-filters');
        const visibleRows = $$('[data-fixed-row]', $('#modal'));
        const filterRows = () => {
          const value = name => controls.querySelector(`[name="${name}"]`).value.trim().toLowerCase();
          const tag = value('tag'), seller = value('seller_name');
          const min = value('amount_min'), max = value('amount_max');
          const from = value('date_from'), to = value('date_to');
          let count = 0;
          items.forEach((row, index) => {
            const show = (row.tags.length ? row.tags : ['未分类']).some(name => name.toLowerCase().includes(tag)) && (row.seller_name || '').toLowerCase().includes(seller)
              && (!min || row.amount >= Number(min)) && (!max || row.amount <= Number(max))
              && (!from || row.invoice_date >= from) && (!to || row.invoice_date <= to);
            visibleRows[index].hidden = !show;
            if (show) count++;
          });
          $('[data-no-match]').hidden = !items.length || count > 0;
        };
        controls.oninput = filterRows;
        $('[data-clear]', controls).onclick = () => {
          $$('input', controls).forEach(input => input.value = '');
          filterRows();
        };
        $('#modal').classList.add('fixed-invoices');
      } catch (error) { toast(error.message, true); }
      finally { picker.disabled = false; }
    };
    $('#optimize').onsubmit = async event => {
      event.preventDefault(); const form=event.currentTarget, values=new FormData(form),button=$('[type=submit]',form);button.disabled=true;
      const panel = $('#match-progress');
      panel.hidden = false;
      $('progress',panel).removeAttribute('value');
      $('.match-amount',panel).textContent = '计算中';
      $('.match-work',panel).textContent = '正在读取可用发票';
      $('.match-eta',panel).textContent = '正在估时';
      const controls = [...form.elements];
      controls.forEach(input => input.disabled = true);
      try {
        const data = await matchInvoices({title_id:Number(values.get('title_id')),target_amount:values.get('target_amount'),allow_exceed:values.has('allow_exceed'),fixed_invoice_ids:[...fixed]},panel);
        if (form.isConnected) {state.recommendation=data; await renderRoute();}
      } catch(error) {
        if (form.isConnected) toast(error.message,true);
      } finally {
        panel.hidden = true;
        controls.forEach(input => input.disabled = false);
      }
    };
    if(!result?.invoices?.length)return;
    const selections=()=>$$('[data-select]:checked').map(input=>Number(input.dataset.select));
    const update=()=>{const selected=new Set(selections()),total=result.invoices.filter(row=>selected.has(row.id)).reduce((sum,row)=>sum+Math.round(Number(row.amount)*100),0)/100;$('#selection-count').textContent=`已选择 ${selected.size} 张发票${result.target_amount ? ` · 目标 ¥ ${money(result.target_amount)}` : ''}`;$('#selection-total').textContent='¥ '+money(total);const delta=Math.round((total-Number(result.target_amount))*100)/100;$('#selection-difference').textContent=!result.target_amount?'':delta===0?'刚好达到目标金额':`${delta>0?'超出':'距离'}目标 ¥ ${money(Math.abs(delta))}`;$('#generate-pdf').disabled=!selected.size;};
    $$('[data-select]').forEach(input=>input.onchange=update);update();
    $('#merge-pdf').onchange = event => {
      $('#generate-pdf').textContent = event.target.checked ? '生成A4双拼PDF和清单' : '导出PDF和清单';
    };
    $('#generate-pdf').onclick=async()=>{const button=$('#generate-pdf');button.disabled=true;try{const job=await post('/api/print/generate',{reimbursement_plan_id:result.id,title_id:result.title_id,merge_pdf:$('#merge-pdf').checked,invoice_ids:selections(),auto_reimburse:$('#auto-reimburse').checked});state.recommendation=null;download(job.bundle_url);toast(job.auto_reimburse?'PDF 和清单已生成，所选发票已标记为已报销':'PDF 和清单已生成，发票报销状态未变');navigate('/prints');}catch(error){toast(error.message,true);button.disabled=false;}};
  }};
}

async function printPage() {
  const data=await api(`/api/print?page=${state.printPage}`);
  const rows=data.items.map(row=>`<tr><td class="strong">${row.merge_pdf ? 'A4 双拼' : '独立 PDF'} · #${row.id}</td><td>${escapeHTML(row.title_name)}</td><td>${row.invoice_count} 张</td><td class="amount">¥ ${money(row.total_amount)}</td><td>${row.auto_reimburse?'导出时自动报销':'未自动报销'}</td><td>${escapeHTML(dateText(row.created_at))}</td><td><a href="${row.download_url}" download>${row.merge_pdf ? '下载 PDF' : '下载 ZIP'}</a> ${row.manifest_url ? `<a href="${row.manifest_url}" download>下载清单</a>` : ''}</td></tr>`).join('');
  return {html:heading('打印记录','每一份整理好的发票，都可以在这里重新下载。')+`<section class="panel"><div class="panel-head"><h2>已生成的打印文件</h2><span class="muted">A4 · 上下双拼</span></div>${rows?`<div class="table-wrap"><table><thead><tr><th>文件</th><th>报销抬头</th><th>发票数量</th><th>合计金额</th><th>报销选项</th><th>生成时间</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`:empty('还没有打印记录','在报销助手中选择发票，即可生成 A4 双拼文件。','print')}${pagination(data.total,state.printPage,data.page_size)}</section>`,bind:()=>{$('#prev').onclick=()=>{state.printPage--;renderRoute();};$('#next').onclick=()=>{state.printPage++;renderRoute();};}};
}

async function settingsPage() {
  const user=await api('/api/users/me');state.user=user;
  return {html:heading('账户设置','连接你的飞书，让发票收集再简单一点。')+`<div class="settings-grid"><section class="panel"><div class="panel-head"><h2>飞书账号关联</h2><span class="badge ${user.feishu_open_id?'':'pending'}">${user.feishu_open_id?'已绑定':'尚未绑定'}</span></div><div class="section-body"><p>当前用户名：<strong>${escapeHTML(user.username)}</strong></p><div class="binding">在飞书与机器人单聊，发送完整用户名：<br><code>${escapeHTML(user.username)}</code><br><span class="muted">绑定完成后，直接发送 PDF 发票即可。</span></div><div class="actions"><button id="show-feishu-qr" class="primary" type="button">扫码添加机器人</button><button id="copy-username">复制用户名</button>${user.feishu_open_id?'<button id="unbind" class="danger">解绑飞书</button>':'<button id="refresh-binding">刷新绑定状态</button>'}</div></div></section><section class="panel"><div class="panel-head"><h2>修改密码</h2></div><form class="section-body stack" id="password-form"><label>原密码<input type="password" name="old_password" autocomplete="current-password" required maxlength="128"></label><label>新密码<input type="password" name="new_password" autocomplete="new-password" minlength="8" maxlength="128" required placeholder="至少 8 位字符"></label><label>确认新密码<input type="password" name="confirm_password" autocomplete="new-password" minlength="8" maxlength="128" required></label><button class="primary" type="submit">保存新密码</button></form></section></div><button id="settings-logout" class="danger">退出登录</button>`,bind:()=>{
    $('#show-feishu-qr').onclick=()=>{
      const modal=$('#modal');
      modal.className='feishu-qr-modal';
      modal.onclose=null;
      modal.innerHTML=`<div class="modal-head"><h2 id="modal-title">扫码添加飞书机器人</h2><button type="button" class="subtle" data-close aria-label="关闭">✕</button></div><div class="modal-body feishu-qr-body"><p class="muted">打开飞书扫一扫，扫描下方二维码添加机器人。</p><img src="/resource/invoice-qr.jpg" alt="飞书发票机器人添加二维码" width="320" height="376" class="feishu-qr-image"><p data-qr-error role="status" hidden>二维码加载失败，请关闭弹窗后重试。</p><p>添加后，在机器人单聊中发送用户名 <strong>${escapeHTML(user.username)}</strong>，即可关联账号。</p><button type="button" data-close>关闭</button></div>`;
      $$('[data-close]',modal).forEach(button=>button.onclick=()=>modal.close());
      $('.feishu-qr-image',modal).onerror=event=>{event.currentTarget.hidden=true;$('[data-qr-error]',modal).hidden=false;};
      modal.showModal();
    };
    $('#settings-logout').onclick=performLogout;
    $('#copy-username').onclick=async()=>{try{await navigator.clipboard.writeText(user.username);toast('用户名已复制');}catch{toast('请选中上方用户名后复制');}};
    if($('#refresh-binding'))$('#refresh-binding').onclick=()=>renderRoute();
    if($('#unbind'))$('#unbind').onclick=()=>confirmAction('解绑飞书账号','解绑后，新发送的发票将不再收录，已有发票仍保留。',async()=>{await api('/api/auth/feishu/bind',{method:'DELETE'});toast('飞书已解绑');await renderRoute();});
    $('#password-form').onsubmit=async event=>{event.preventDefault();const values=Object.fromEntries(new FormData(event.currentTarget));if(values.new_password!==values.confirm_password)return toast('两次输入的新密码不一致',true);delete values.confirm_password;const button=$('[type=submit]',event.currentTarget);button.disabled=true;try{const result=await api('/api/users/me/password',{method:'PUT',body:JSON.stringify(values)});state.user=null;resetWorkspace();state.csrf=result.csrf_token;history.replaceState({},'','/login');renderAuth();toast('密码已更新，请重新登录');}catch(error){toast(error.message,true);button.disabled=false;}};
  }};
}

async function adminPage() {
  if(state.user.role!=='admin')throw new Error('需要管理员权限');
  const tabs=`<div class="tabs">${[['users','用户管理'],['invoices','全部发票'],['logs','系统日志']].map(([key,name])=>`<button data-tab="${key}" class="${state.adminTab===key?'active':''}">${name}</button>`).join('')}</div>`;
  let content='',afterBind=()=>{};
  if(state.adminTab==='users'){
    const data=await api(`/api/admin/users?page=${state.adminPage}`);
    content=`<section class="panel"><div class="panel-head"><h2>用户管理</h2><span class="muted">停用账号、调整角色、重置密码和解绑飞书</span></div><div class="table-wrap"><table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>飞书绑定</th><th>注册时间</th><th>操作</th></tr></thead><tbody>${data.items.map(row=>`<tr><td class="strong">${escapeHTML(row.username)}<span class="subline">${escapeHTML(row.name || '未设置显示名称')}</span></td><td>${row.role==='admin'?'管理员':'普通用户'}</td><td>${row.is_active?'启用':'停用'}</td><td>${row.feishu_open_id?'已绑定':'未绑定'}</td><td>${escapeHTML(dateText(row.created_at))}</td><td><button class="subtle" data-admin-user="${row.id}">管理</button></td></tr>`).join('')}</tbody></table></div>${pagination(data.total,state.adminPage,data.page_size)}</section>`;
    afterBind=()=>{$$('[data-admin-user]').forEach(button=>button.onclick=()=>editUser(data.items.find(row=>row.id===Number(button.dataset.adminUser))));$('#prev').onclick=()=>{state.adminPage--;renderRoute();};$('#next').onclick=()=>{state.adminPage++;renderRoute();};};
  }else if(state.adminTab==='invoices'){
    const parameters=new URLSearchParams({page:state.adminPage});if(state.adminFilter)parameters.set('user_id',state.adminFilter);
    const data=await api('/api/admin/invoices?'+parameters);
    content=`<section class="panel"><form id="admin-filter" class="filters"><label>用户编号<input name="user_id" type="number" min="1" placeholder="全部用户" value="${escapeHTML(state.adminFilter || '')}"></label><button class="primary" type="submit">筛选</button></form>${data.items.length?`<div class="table-wrap"><table><thead><tr><th>用户</th><th>发票号码</th><th>购买方</th><th>金额</th><th>状态</th><th>原件</th></tr></thead><tbody>${data.items.map(row=>`<tr><td>${escapeHTML(row.username)}<span class="subline">用户 #${row.user_id}</span></td><td>${escapeHTML(row.invoice_number || '待补录')}</td><td>${escapeHTML(row.buyer_name || '—')}</td><td class="amount">${row.amount==null?'—':'¥ '+money(row.amount)}</td><td>${badge(row.status)}</td><td><div class="row-actions"><button class="subtle small" data-preview="${row.id}">预览</button><a class="button small" href="/api/admin/invoices/${row.id}/file" download>原件下载</a><button class="subtle small danger" data-delete-invoice="${row.id}">删除</button></div></td></tr>`).join('')}</tbody></table></div>`:empty('暂无发票记录','用户通过飞书收录或手动上传后会显示在这里。')}${pagination(data.total,state.adminPage,data.page_size)}</section>`;
    afterBind=()=>{bindPreviews(data.items,true);$('#admin-filter').onsubmit=event=>{event.preventDefault();state.adminFilter=new FormData(event.currentTarget).get('user_id');state.adminPage=1;renderRoute();};$('#prev').onclick=()=>{state.adminPage--;renderRoute();};$('#next').onclick=()=>{state.adminPage++;renderRoute();};};
  }else{
    const source=state.logSource || 'web',data=await api(`/api/admin/logs?source=${source}`);
    content=`<section class="panel"><div class="panel-head"><h2>最近的系统日志</h2><div class="actions"><select id="log-source" aria-label="日志来源"><option value="web" ${source==='web'?'selected':''}>Web 服务</option><option value="feishu" ${source==='feishu'?'selected':''}>飞书机器人</option></select><button id="refresh-logs">刷新</button></div></div><pre class="log">${escapeHTML(data.lines.join('\n') || '暂无日志。')}</pre></section>`;
    afterBind=()=>{$('#log-source').onchange=event=>{state.logSource=event.target.value;renderRoute();};$('#refresh-logs').onclick=()=>renderRoute();};
  }
  return {html:heading('管理后台','查看用户、发票记录和服务运行情况。')+tabs+content,bind:()=>{$$('[data-tab]').forEach(button=>button.onclick=()=>{state.adminTab=button.dataset.tab;state.adminPage=1;renderRoute();});afterBind();}};
}

function editUser(user) {
  openForm('管理用户 · '+escapeHTML(user.username),`<div class="stack"><label>显示名称<input name="name" maxlength="100" value="${escapeHTML(user.name)}"></label><label>用户角色<select name="role"><option value="user" ${user.role==='user'?'selected':''}>普通用户</option><option value="admin" ${user.role==='admin'?'selected':''}>管理员</option></select></label><label class="check"><input type="checkbox" name="is_active" ${user.is_active?'checked':''}>启用该账号</label><label>重置密码<input name="new_password" type="password" autocomplete="new-password" minlength="8" maxlength="128" placeholder="留空则不修改密码"></label>${user.feishu_open_id?'<label class="check"><input type="checkbox" name="unbind_feishu">解除该用户的飞书绑定</label>':''}<p class="muted">至少需要保留一名启用的管理员。重置密码后，该用户需要重新登录。</p></div>`,'保存设置',async form=>{const values=Object.fromEntries(form);values.is_active=form.has('is_active');values.unbind_feishu=form.has('unbind_feishu');if(!values.new_password)delete values.new_password;await api(`/api/admin/users/${user.id}`,{method:'PUT',body:JSON.stringify(values)});toast('用户设置已保存');const session=await api('/api/auth/session');state.user=session.user;state.csrf=session.csrf_token;if(!state.user)renderAuth();else if(state.user.role!=='admin')navigate('/invoices');else await renderRoute();});
}

window.addEventListener('popstate',()=>state.user?renderRoute():renderAuth());
(async()=>{try{const session=await api('/api/auth/session');state.csrf=session.csrf_token;state.user=session.user;if(state.user)await renderRoute();else renderAuth();}catch(error){$('#app').innerHTML='<div class="boot">暂时无法连接服务，请刷新页面重试。</div>';toast(error.message,true);}})();
