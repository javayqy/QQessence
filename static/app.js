/* ==========================================================================
   QQ 群精华 · 本地存档  —  前端交互逻辑
   原生 ES6，无任何框架 / CDN 依赖。整体结构：
     1. 常量与工具
     2. 全局状态
     3. API 封装
     4. Toast / 主题
     5. 渲染：配置、群列表、快照列表、消息流
     6. 事件绑定与初始化
   ========================================================================== */

(function () {
  'use strict';

  /* ---------------------------------------------------------------------- */
  /* 1. 常量与工具                                                          */
  /* ---------------------------------------------------------------------- */

  const THEME_KEY = 'qqessence-theme';
  const SEARCH_DEBOUNCE_MS = 200;

  const TYPE_LABELS = {
    text: '文本',
    image: '图片',
    mix: '图文',
    file: '文件',
    link: '链接',
    forward: '转发',
    unknown: '未识别',
  };

  const dom = {
    cookieInput: () => document.getElementById('cookieInput'),
    cookieBadge: () => document.getElementById('cookieBadge'),
    cookieMeta: () => document.getElementById('cookieMeta'),
    saveCookieBtn: () => document.getElementById('saveCookieBtn'),
    clearCookieBtn: () => document.getElementById('clearCookieBtn'),
    gcInput: () => document.getElementById('gcInput'),
    fetchBtn: () => document.getElementById('fetchBtn'),
    downloadOnFetch: () => document.getElementById('downloadOnFetch'),
    reloadGroupsBtn: () => document.getElementById('reloadGroupsBtn'),
    groupList: () => document.getElementById('groupList'),
    groupMeta: () => document.getElementById('groupMeta'),
    snapshotList: () => document.getElementById('snapshotList'),
    snapshotCount: () => document.getElementById('snapshotCount'),
    viewTitle: () => document.getElementById('viewTitle'),
    viewSubtitle: () => document.getElementById('viewSubtitle'),
    searchInput: () => document.getElementById('searchInput'),
    onlyImages: () => document.getElementById('onlyImages'),
    downloadBtn: () => document.getElementById('downloadBtn'),
    exportSelect: () => document.getElementById('exportSelect'),
    content: () => document.getElementById('content'),
    themeBtn: () => document.getElementById('themeBtn'),
    lightbox: () => document.getElementById('lightbox'),
    lightboxImage: () => document.getElementById('lightboxImage'),
    lightboxCounter: () => document.getElementById('lightboxCounter'),
    lightboxClose: () => document.getElementById('lightboxClose'),
    lightboxPrev: () => document.getElementById('lightboxPrev'),
    lightboxNext: () => document.getElementById('lightboxNext'),
    toasts: () => document.getElementById('toasts'),
  };

  /** HTML 转义，所有插入 innerHTML 的用户内容都必须经过它。 */
  function escapeHtml(value) {
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, (ch) => map[ch]);
  }

  /** 防抖。 */
  function debounce(fn, wait) {
    let timer = null;
    return function debounced() {
      const args = arguments;
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        timer = null;
        fn.apply(null, args);
      }, wait);
    };
  }

  /** 解析时间字符串为 Date。 */
  function parseTime(iso, raw) {
    if (iso) {
      const parsed = new Date(iso);
      if (!Number.isNaN(parsed.getTime())) return parsed;
    }
    if (raw) {
      const normalized = String(raw).trim().replace(' ', 'T');
      const parsed = new Date(normalized);
      if (!Number.isNaN(parsed.getTime())) return parsed;
    }
    return null;
  }

  /** 相对时间：刚刚 / n 分钟前 / n 小时前 / n 天前 / 日期。 */
  function relativeTime(iso, raw) {
    const time = parseTime(iso, raw);
    if (!time) return String(raw || '');
    const diffSeconds = Math.floor((Date.now() - time.getTime()) / 1000);
    if (diffSeconds < 60) return '刚刚';
    if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} 分钟前`;
    if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} 小时前`;
    if (diffSeconds < 86400 * 30) return `${Math.floor(diffSeconds / 86400)} 天前`;
    const year = time.getFullYear();
    const month = String(time.getMonth() + 1).padStart(2, '0');
    const day = String(time.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  /** 字节数格式化。 */
  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let size = value / 1024;
    let index = 0;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${size.toFixed(1)} ${units[index]}`;
  }

  /* ---------------------------------------------------------------------- */
  /* 2. 全局状态                                                            */
  /* ---------------------------------------------------------------------- */

  const state = {
    config: null,
    groups: [],
    snapshots: [],
    snapshot: null,
    messages: [],
    activeId: '',
    query: '',
    onlyImages: false,
    lightboxItems: [],
    lightboxIndex: 0,
    busy: false,
  };

  /* ---------------------------------------------------------------------- */
  /* 3. API 封装                                                            */
  /* ---------------------------------------------------------------------- */

  /** 统一的 API 异常。 */
  class ApiError extends Error {
    constructor(message, hint) {
      super(message);
      this.name = 'ApiError';
      this.hint = hint || '';
    }
  }

  /**
   * 发起 API 请求，统一处理 `{ok:false}` 与网络错误。
   * @param {string} path 请求路径
   * @param {{method?:string, body?:any}} options 选项
   * @returns {Promise<any>} data 字段
   */
  async function api(path, options) {
    const settings = options || {};
    const requestInit = { method: settings.method || 'GET', headers: {} };
    if (settings.body !== undefined) {
      requestInit.headers['Content-Type'] = 'application/json';
      requestInit.body = JSON.stringify(settings.body);
    }

    let response;
    try {
      response = await fetch(path, requestInit);
    } catch (error) {
      throw new ApiError('无法连接后端服务', '请确认 python app.py 仍在运行');
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }

    if (!payload || payload.ok !== true) {
      const message = (payload && payload.error) || `请求失败（HTTP ${response.status}）`;
      const hint = (payload && payload.hint) || '';
      throw new ApiError(message, hint);
    }
    return payload.data;
  }

  /* ---------------------------------------------------------------------- */
  /* 4. Toast / 主题                                                        */
  /* ---------------------------------------------------------------------- */

  /**
   * 弹出提示。
   * @param {string} message 主文案
   * @param {'info'|'success'|'error'} type 类型
   * @param {string} hint 附加说明
   */
  function toast(message, type, hint) {
    const container = dom.toasts();
    if (!container) return;

    const node = document.createElement('div');
    node.className = `toast ${type || 'info'}`;
    const text = hint ? `${message}\n${hint}` : message;
    node.textContent = text;
    node.style.whiteSpace = 'pre-wrap';
    container.appendChild(node);

    window.setTimeout(() => {
      node.classList.add('leaving');
      window.setTimeout(() => node.remove(), 220);
    }, type === 'error' ? 5200 : 3000);
  }

  /** 应用主题。 */
  function applyTheme(theme) {
    const next = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    const button = dom.themeBtn();
    if (button) button.textContent = next === 'dark' ? '切换到浅色' : '切换到深色';
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch (error) {
      /* localStorage 不可用时静默忽略 */
    }
  }

  /** 初始化主题。 */
  function initTheme() {
    let stored = 'light';
    try {
      stored = window.localStorage.getItem(THEME_KEY) || 'light';
    } catch (error) {
      stored = 'light';
    }
    applyTheme(stored);
  }

  /** 切换主题。 */
  function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    applyTheme(current === 'dark' ? 'light' : 'dark');
  }

  /* ---------------------------------------------------------------------- */
  /* 5. 渲染：配置                                                          */
  /* ---------------------------------------------------------------------- */

  function renderConfig() {
    const config = state.config || {};
    const badge = dom.cookieBadge();
    const meta = dom.cookieMeta();
    const input = dom.cookieInput();

    if (badge) {
      if (config.has_cookie) {
        badge.textContent = '已配置';
        badge.className = 'badge badge-ok';
      } else {
        badge.textContent = '未配置';
        badge.className = 'badge badge-idle';
      }
    }

    if (meta) {
      if (config.has_cookie) {
        const parts = [];
        if (config.uin) parts.push(`QQ ${config.uin}`);
        if (config.cookie_masked) parts.push(config.cookie_masked);
        if (config.updated_at) parts.push(`更新于 ${config.updated_at}`);
        meta.textContent = parts.join(' · ') || '已配置';
      } else {
        meta.textContent = '未登录，无法拉取精华消息';
      }
    }

    if (input) {
      // 不回显完整 Cookie（后端也不会返回），只提示占位文案。
      input.placeholder = config.has_cookie
        ? '已保存 Cookie（出于安全不回显）。如需更换，请粘贴新的整段 Cookie 再保存。'
        : 'a=b; c=d; skey=...; p_skey=...';
    }
  }

  /* ---------------------------------------------------------------------- */
  /* 6. 渲染：群列表                                                        */
  /* ---------------------------------------------------------------------- */

  function renderGroups() {
    const list = dom.groupList();
    const meta = dom.groupMeta();
    if (!list) return;

    if (!state.groups.length) {
      list.hidden = true;
      list.innerHTML = '';
      if (meta) {
        meta.textContent = state.config && state.config.has_cookie
          ? '未获取到群列表，可直接手动输入群号'
          : '未配置 Cookie，请手动输入群号';
      }
      return;
    }

    list.hidden = false;
    list.innerHTML = state.groups
      .map(
        (group) => `
        <button class="group-item" type="button" data-gc="${escapeHtml(group.gc)}">
          <span class="gname">${escapeHtml(group.name || group.gc)}</span>
          <span class="ggc">${escapeHtml(group.gc)}</span>
        </button>`
      )
      .join('');

    if (meta) meta.textContent = `共 ${state.groups.length} 个群`;
  }

  /* ---------------------------------------------------------------------- */
  /* 7. 渲染：快照列表                                                      */
  /* ---------------------------------------------------------------------- */

  function renderSnapshots() {
    const list = dom.snapshotList();
    const counter = dom.snapshotCount();
    if (!list) return;

    if (counter) counter.textContent = String(state.snapshots.length);

    if (!state.snapshots.length) {
      list.innerHTML = '<div class="list-empty">暂无快照</div>';
      return;
    }

    list.innerHTML = state.snapshots
      .map((item) => {
        const active = item.id === state.activeId ? ' active' : '';
        const imageText = item.image_count ? ` · ${item.image_count} 图` : '';
        return `
        <button class="snap-item${active}" type="button" data-id="${escapeHtml(item.id)}">
          <div class="snap-head">
            <span class="snap-gc">${escapeHtml(item.gc)}</span>
            <span class="snap-time">${escapeHtml(item.created_at_display || item.created_at || '')}</span>
          </div>
          <div class="snap-stats">
            <span>${escapeHtml(String(item.count || 0))} 条${imageText}</span>
            <span>${escapeHtml(formatBytes(item.size_bytes))}</span>
            ${item.downloaded_images ? `<span>已下 ${escapeHtml(String(item.downloaded_images))}</span>` : ''}
          </div>
          <span class="snap-delete" data-delete="${escapeHtml(item.id)}" title="删除该快照">×</span>
        </button>`;
      })
      .join('');
  }

  /* ---------------------------------------------------------------------- */
  /* 8. 渲染：工具条                                                        */
  /* ---------------------------------------------------------------------- */

  function renderToolbar() {
    const title = dom.viewTitle();
    const subtitle = dom.viewSubtitle();
    const snapshot = state.snapshot;

    if (!snapshot) {
      if (title) title.textContent = '未选择快照';
      if (subtitle) subtitle.textContent = '从左侧选择一条历史记录，或拉取新的群精华';
      if (dom.downloadBtn()) dom.downloadBtn().disabled = true;
      return;
    }

    const total = state.messages.length;
    const visible = filteredMessages().length;
    if (title) title.textContent = `群 ${snapshot.gc}`;
    if (subtitle) {
      const shown = visible === total ? `共 ${total} 条` : `显示 ${visible} / 共 ${total} 条`;
      subtitle.textContent = `${snapshot.created_at_display || snapshot.created_at || ''} · ${shown}`;
    }
    if (dom.downloadBtn()) {
      dom.downloadBtn().disabled = !(snapshot.image_count > 0);
    }
  }

  /* ---------------------------------------------------------------------- */
  /* 9. 渲染：消息流                                                        */
  /* ---------------------------------------------------------------------- */

  /** 当前过滤条件下的消息列表。 */
  function filteredMessages() {
    const query = state.query.trim().toLowerCase();
    return state.messages.filter((message) => {
      if (state.onlyImages && !(message.images && message.images.length)) return false;
      if (!query) return true;

      const haystack = [
        message.sender_name,
        message.sender_uin,
        message.text,
        ...(message.files || []).map((item) => item.name),
        ...(message.links || []).map((item) => `${item.title} ${item.url}`),
      ]
        .filter(Boolean)
        .join('\n')
        .toLowerCase();

      return haystack.indexOf(query) !== -1;
    });
  }

  /** 解析图片显示地址：优先本地已下载的媒体路径。 */
  function resolveImage(snapshot, url) {
    const localImages = (snapshot && snapshot.local_images) || {};
    if (Object.prototype.hasOwnProperty.call(localImages, url)) {
      return localImages[url];
    }
    return url;
  }

  /** 渲染单条消息卡片。 */
  function renderMessageCard(message, index) {
    const snapshot = state.snapshot || {};
    const name = escapeHtml(message.sender_name || '未知成员');
    const initials = escapeHtml(String(message.sender_name || '?').slice(0, 1));
    const avatar = message.avatar ? escapeHtml(message.avatar) : '';
    const timeTitle = escapeHtml(message.time_raw || message.time_iso || '');
    const timeText = escapeHtml(relativeTime(message.time_iso, message.time_raw));

    const avatarHtml = avatar
      ? `<img class="avatar" src="${avatar}" alt="" loading="lazy"
              onerror="this.style.display='none';this.nextElementSibling.style.display='grid';">
         <span class="avatar avatar-fallback" style="display:none">${initials}</span>`
      : `<span class="avatar avatar-fallback">${initials}</span>`;

    const parts = [];
    parts.push(`<article class="message-card" data-index="${index}">`);
    parts.push(avatarHtml);
    parts.push('<div class="msg-body">');
    parts.push('<div class="msg-head">');
    parts.push(`<span class="msg-name">${name}</span>`);
    if (message.sender_uin) {
      parts.push(`<span class="msg-uin">QQ ${escapeHtml(message.sender_uin)}</span>`);
    }
    parts.push(`<span class="msg-time" title="${timeTitle}">${timeText}</span>`);
    parts.push('</div>');

    const text = String(message.text || '').trim();
    if (text) {
      const paragraphs = text
        .split('\n')
        .filter((line) => line.trim().length > 0)
        .map((line) => `<p>${escapeHtml(line)}</p>`)
        .join('');
      parts.push(`<div class="msg-text">${paragraphs || `<p>${escapeHtml(text)}</p>`}</div>`);
    }

    const images = (message.images || []).filter((url) => typeof url === 'string' && url);
    if (images.length) {
      const gridClass = images.length === 1 ? 'grid-1' : images.length === 2 ? 'grid-2' : 'grid-many';
      parts.push(`<div class="msg-images ${gridClass}">`);
      images.forEach((url) => {
        const src = escapeHtml(resolveImage(snapshot, url));
        parts.push(
          `<button class="msg-image" type="button" data-src="${src}" data-message="${index}">
             <img src="${src}" alt="图片" loading="lazy"
                  onerror="this.parentElement.classList.add('broken');this.style.display='none';">
           </button>`
        );
      });
      parts.push('</div>');
    }

    (message.files || []).forEach((file) => {
      const fileName = escapeHtml(file.name || '文件');
      const fileSize = file.size ? `<span class="file-size">${escapeHtml(file.size)}</span>` : '';
      const icon = '<span class="file-icon">📎</span>';
      if (file.url) {
        parts.push(
          `<a class="file-card" href="${escapeHtml(file.url)}" target="_blank" rel="noopener">
             ${icon}<span class="file-name">${fileName}</span>${fileSize}</a>`
        );
      } else {
        parts.push(`<div class="file-card">${icon}<span class="file-name">${fileName}</span>${fileSize}</div>`);
      }
    });

    (message.links || []).forEach((link) => {
      const url = escapeHtml(link.url || '');
      const title = escapeHtml(link.title || link.url || '');
      parts.push(
        `<a class="link-card" href="${url}" target="_blank" rel="noopener">
           <span class="link-icon">🔗</span>
           <span class="link-body">
             <span class="link-title">${title}</span>
             <span class="link-url">${url}</span>
           </span>
         </a>`
      );
    });

    if (message.type && message.type !== 'text' && message.type !== 'mix') {
      const label = TYPE_LABELS[message.type] || message.type;
      parts.push(`<span class="msg-type-tag">${escapeHtml(label)}</span>`);
    }

    if (!text && !images.length && !(message.files || []).length && !(message.links || []).length) {
      parts.push('<div class="msg-text"><p>（该消息没有可展示的内容，可能类型未识别）</p></div>');
    }

    parts.push('</div></article>');
    return parts.join('\n');
  }

  /** 渲染消息流整体。 */
  function renderMessages() {
    const content = dom.content();
    if (!content) return;

    if (!state.snapshot) {
      content.innerHTML = `
        <div class="empty-state">
          <div class="empty-art">📦</div>
          <h3>还没有可查看的精华消息</h3>
          <p>先在左侧保存 Cookie，填写群号后点击「拉取并保存」。<br>没有 Cookie 接口权限时，可直接手动输入群号。</p>
        </div>`;
      renderToolbar();
      return;
    }

    const visible = filteredMessages();
    state.lightboxItems = [];

    if (!state.messages.length) {
      content.innerHTML = `
        <div class="empty-state">
          <div class="empty-art">🗒️</div>
          <h3>该快照没有消息</h3>
          <p>可能是该群暂无精华消息，或 Cookie 无权限访问该群。<br>请检查群号与 Cookie 后重新拉取。</p>
        </div>`;
      renderToolbar();
      return;
    }

    if (!visible.length) {
      content.innerHTML = `
        <div class="empty-state">
          <div class="empty-art">🔍</div>
          <h3>没有匹配的消息</h3>
          <p>试试更换关键词，或关闭「只看有图」筛选。</p>
        </div>`;
      renderToolbar();
      return;
    }

    const html = visible.map((message, index) => renderMessageCard(message, index)).join('\n');
    content.innerHTML = `<div class="content-inner">${html}</div>`;

    // 收集当前可见消息里的图片，供灯箱使用。
    visible.forEach((message) => {
      (message.images || []).forEach((url) => {
        if (typeof url === 'string' && url) {
          state.lightboxItems.push(resolveImage(state.snapshot, url));
        }
      });
    });

    renderToolbar();
  }

  /** 渲染骨架屏。 */
  function renderSkeleton(count) {
    const content = dom.content();
    if (!content) return;
    const total = count || 4;
    const cards = [];
    for (let index = 0; index < total; index += 1) {
      cards.push(`
        <div class="skeleton">
          <div class="sk-avatar"></div>
          <div class="sk-lines">
            <div class="sk-line w40"></div>
            <div class="sk-line w90"></div>
            <div class="sk-line w70"></div>
          </div>
        </div>`);
    }
    content.innerHTML = `<div class="content-inner">${cards.join('')}</div>`;
  }

  /* ---------------------------------------------------------------------- */
  /* 10. 灯箱                                                              */
  /* ---------------------------------------------------------------------- */

  function openLightbox(src) {
    const items = state.lightboxItems;
    const index = items.indexOf(src);
    state.lightboxIndex = index >= 0 ? index : 0;
    const box = dom.lightbox();
    if (!box) return;

    if (!items.length) {
      state.lightboxItems = [src];
      state.lightboxIndex = 0;
    }

    box.hidden = false;
    updateLightbox();
  }

  function updateLightbox() {
    const items = state.lightboxItems;
    const image = dom.lightboxImage();
    const counter = dom.lightboxCounter();
    if (!items.length || !image) return;

    const index = Math.min(Math.max(state.lightboxIndex, 0), items.length - 1);
    state.lightboxIndex = index;
    image.src = items[index];
    if (counter) counter.textContent = items.length > 1 ? `${index + 1} / ${items.length}` : '';
  }

  function closeLightbox() {
    const box = dom.lightbox();
    if (box) box.hidden = true;
    const image = dom.lightboxImage();
    if (image) image.removeAttribute('src');
  }

  function stepLightbox(delta) {
    const items = state.lightboxItems;
    if (items.length <= 1) return;
    state.lightboxIndex = (state.lightboxIndex + delta + items.length) % items.length;
    updateLightbox();
  }

  /* ---------------------------------------------------------------------- */
  /* 11. 数据加载                                                           */
  /* ---------------------------------------------------------------------- */

  async function loadConfig() {
    try {
      state.config = await api('/api/config');
    } catch (error) {
      state.config = { has_cookie: false, uin: '', cookie_masked: '' };
      toast(error.message, 'error', error.hint);
    }
    renderConfig();
  }

  async function loadSnapshots(preferredId) {
    try {
      const data = await api('/api/snapshots');
      state.snapshots = (data && data.snapshots) || [];
    } catch (error) {
      state.snapshots = [];
      toast(error.message, 'error', error.hint);
    }
    renderSnapshots();

    if (preferredId) {
      await openSnapshot(preferredId);
    } else if (!state.activeId && state.snapshots.length) {
      await openSnapshot(state.snapshots[0].id);
    }
  }

  async function openSnapshot(snapshotId) {
    if (!snapshotId) return;
    renderSkeleton(3);
    try {
      const data = await api(`/api/snapshots/${encodeURIComponent(snapshotId)}`);
      state.snapshot = data.snapshot;
      state.messages = data.messages || [];
      state.activeId = snapshotId;
      renderSnapshots();
      renderMessages();
      const gcInput = dom.gcInput();
      if (gcInput && state.snapshot && state.snapshot.gc) gcInput.value = state.snapshot.gc;
    } catch (error) {
      state.snapshot = null;
      state.messages = [];
      state.activeId = '';
      renderMessages();
      toast(error.message, 'error', error.hint);
    }
  }

  async function saveCookie() {
    const input = dom.cookieInput();
    if (!input) return;
    const value = input.value.trim();
    if (!value) {
      toast('请先粘贴 Cookie', 'error', '格式形如 a=b; c=d; skey=...; p_skey=...');
      return;
    }

    const button = dom.saveCookieBtn();
    setBusy(button, true);
    try {
      state.config = await api('/api/config', { method: 'POST', body: { cookie: value } });
      input.value = '';
      renderConfig();
      toast('Cookie 已保存', 'success');
      await loadGroups(true);
    } catch (error) {
      toast(error.message, 'error', error.hint);
    } finally {
      setBusy(button, false);
    }
  }

  async function clearCookie() {
    const button = dom.clearCookieBtn();
    setBusy(button, true);
    try {
      state.config = await api('/api/config/clear', { method: 'POST' });
      state.groups = [];
      renderConfig();
      renderGroups();
      toast('Cookie 已清除', 'success');
    } catch (error) {
      toast(error.message, 'error', error.hint);
    } finally {
      setBusy(button, false);
    }
  }

  async function loadGroups(silent) {
    const button = dom.reloadGroupsBtn();
    if (button) button.classList.add('loading');
    try {
      const data = await api('/api/groups');
      state.groups = (data && data.groups) || [];
      renderGroups();
      if (!silent) {
        toast(`已获取 ${state.groups.length} 个群`, 'success');
      }
    } catch (error) {
      state.groups = [];
      renderGroups();
      if (dom.groupMeta()) {
        dom.groupMeta().textContent = '群列表获取失败，可手动输入群号';
      }
      if (!silent) {
        toast(error.message, 'error', error.hint);
      } else {
        console.warn('[群列表] 静默获取失败：', error.message);
      }
    } finally {
      if (button) button.classList.remove('loading');
    }
  }

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) {
      button.classList.add('loading');
      button.disabled = true;
    } else {
      button.classList.remove('loading');
      button.disabled = false;
    }
  }

  async function fetchEssence() {
    const input = dom.gcInput();
    const gc = input ? input.value.trim() : '';
    if (!/^\d{5,12}$/.test(gc)) {
      toast('群号格式不正确', 'error', '群号必须是 5-12 位数字');
      return;
    }
    if (!state.config || !state.config.has_cookie) {
      toast('尚未配置 Cookie', 'error', '请先在「连接」卡片粘贴并保存 Cookie');
      return;
    }

    const button = dom.fetchBtn();
    const shouldDownload = Boolean(dom.downloadOnFetch() && dom.downloadOnFetch().checked);
    setBusy(button, true);
    renderSkeleton(4);
    if (dom.viewTitle()) dom.viewTitle().textContent = '正在拉取…';
    if (dom.viewSubtitle()) dom.viewSubtitle().textContent = `群 ${gc}`;

    try {
      const data = await api('/api/fetch', {
        method: 'POST',
        body: { gc: gc, max_pages: 3, download_images: shouldDownload },
      });
      state.snapshot = data.snapshot;
      state.messages = data.messages || [];
      state.activeId = data.snapshot.id;
      renderMessages();
      await loadSnapshots(data.snapshot.id);

      (data.warnings || []).forEach((warning) => toast(warning, 'info'));
      toast(`已保存快照：${state.messages.length} 条消息`, 'success');
    } catch (error) {
      renderMessages();
      toast(error.message, 'error', error.hint);
    } finally {
      setBusy(button, false);
      renderToolbar();
    }
  }

  async function downloadImages() {
    if (!state.activeId) return;
    const button = dom.downloadBtn();
    setBusy(button, true);
    try {
      const data = await api(`/api/snapshots/${encodeURIComponent(state.activeId)}/download`, {
        method: 'POST',
      });
      const stats = (data && data.stats) || {};
      state.snapshot = (data && data.snapshot) || state.snapshot;
      renderMessages();
      await loadSnapshots(state.activeId);
      toast(`图片下载完成：成功 ${stats.success || 0}，跳过 ${stats.skipped || 0}，失败 ${stats.failed || 0}`, 'success');
    } catch (error) {
      toast(error.message, 'error', error.hint);
    } finally {
      setBusy(button, false);
    }
  }

  function exportSnapshot(format) {
    if (!state.activeId || !format) return;
    const url = `/api/snapshots/${encodeURIComponent(state.activeId)}/export?format=${encodeURIComponent(format)}`;
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    toast(`正在导出 ${format.toUpperCase()}…`, 'success');
  }

  async function deleteSnapshot(snapshotId) {
    const confirmed = window.confirm(`确定删除快照 ${snapshotId} 吗？本地图片会一并删除，且不可恢复。`);
    if (!confirmed) return;

    try {
      await api(`/api/snapshots/${encodeURIComponent(snapshotId)}`, { method: 'DELETE' });
      toast('快照已删除', 'success');
      if (state.activeId === snapshotId) {
        state.activeId = '';
        state.snapshot = null;
        state.messages = [];
      }
      await loadSnapshots();
      if (!state.activeId) renderMessages();
    } catch (error) {
      toast(error.message, 'error', error.hint);
    }
  }

  /* ---------------------------------------------------------------------- */
  /* 12. 事件绑定                                                           */
  /* ---------------------------------------------------------------------- */

  function bindEvents() {
    const saveBtn = dom.saveCookieBtn();
    if (saveBtn) saveBtn.addEventListener('click', saveCookie);

    const clearBtn = dom.clearCookieBtn();
    if (clearBtn) clearBtn.addEventListener('click', clearCookie);

    const reloadGroups = dom.reloadGroupsBtn();
    if (reloadGroups) reloadGroups.addEventListener('click', () => loadGroups(false));

    const fetchBtn = dom.fetchBtn();
    if (fetchBtn) fetchBtn.addEventListener('click', fetchEssence);

    const gcInput = dom.gcInput();
    if (gcInput) {
      gcInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') fetchEssence();
      });
    }

    const groupList = dom.groupList();
    if (groupList) {
      groupList.addEventListener('click', (event) => {
        const target = event.target.closest('[data-gc]');
        if (!target) return;
        const gc = target.getAttribute('data-gc');
        if (gcInput) gcInput.value = gc;
      });
    }

    const snapshotList = dom.snapshotList();
    if (snapshotList) {
      snapshotList.addEventListener('click', (event) => {
        const deleteBtn = event.target.closest('[data-delete]');
        if (deleteBtn) {
          event.stopPropagation();
          deleteSnapshot(deleteBtn.getAttribute('data-delete'));
          return;
        }
        const item = event.target.closest('[data-id]');
        if (item) openSnapshot(item.getAttribute('data-id'));
      });
    }

    const searchInput = dom.searchInput();
    if (searchInput) {
      const handler = debounce(() => {
        state.query = searchInput.value || '';
        renderMessages();
      }, SEARCH_DEBOUNCE_MS);
      searchInput.addEventListener('input', handler);
    }

    const onlyImages = dom.onlyImages();
    if (onlyImages) {
      onlyImages.addEventListener('change', () => {
        state.onlyImages = Boolean(onlyImages.checked);
        renderMessages();
      });
    }

    const downloadBtn = dom.downloadBtn();
    if (downloadBtn) downloadBtn.addEventListener('click', downloadImages);

    const exportSelect = dom.exportSelect();
    if (exportSelect) {
      exportSelect.addEventListener('change', () => {
        const format = exportSelect.value;
        exportSelect.value = '';
        if (format) exportSnapshot(format);
      });
    }

    const themeBtn = dom.themeBtn();
    if (themeBtn) themeBtn.addEventListener('click', toggleTheme);

    // 消息图片点击（事件委托）
    const content = dom.content();
    if (content) {
      content.addEventListener('click', (event) => {
        const imageButton = event.target.closest('[data-src]');
        if (!imageButton) return;
        openLightbox(imageButton.getAttribute('data-src'));
      });
    }

    // 灯箱
    const lightbox = dom.lightbox();
    if (lightbox) {
      lightbox.addEventListener('click', (event) => {
        if (event.target === lightbox) closeLightbox();
      });
    }
    const closeBtn = dom.lightboxClose();
    if (closeBtn) closeBtn.addEventListener('click', closeLightbox);
    const prevBtn = dom.lightboxPrev();
    if (prevBtn) prevBtn.addEventListener('click', () => stepLightbox(-1));
    const nextBtn = dom.lightboxNext();
    if (nextBtn) nextBtn.addEventListener('click', () => stepLightbox(1));

    document.addEventListener('keydown', (event) => {
      const box = dom.lightbox();
      const open = box && !box.hidden;
      if (event.key === 'Escape') {
        if (open) closeLightbox();
        return;
      }
      if (!open) return;
      if (event.key === 'ArrowLeft') stepLightbox(-1);
      if (event.key === 'ArrowRight') stepLightbox(1);
    });
  }

  /* ---------------------------------------------------------------------- */
  /* 13. 初始化                                                             */
  /* ---------------------------------------------------------------------- */

  async function init() {
    initTheme();
    bindEvents();
    renderConfig();
    renderGroups();
    renderSnapshots();
    renderMessages();

    await loadConfig();
    await loadSnapshots();

    if (state.config && state.config.has_cookie) {
      // 首次进入时静默尝试拉群列表，失败不打扰用户。
      loadGroups(true);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
