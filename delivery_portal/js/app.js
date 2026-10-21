// منطق الواجهة: تسجيل الدخول، عرض طلبات التوصيل المُسندة، وتنفيذ إجراءَي
// "تأكيد الاستلام" و"تأكيد الوصول للزبون". لا إدارة سائقين هنا عمداً — هذه
// الشركة تتولى تنظيم فريقها داخلياً؛ الصفحة تعرض فقط ما تحتاجه لإكمال كل طلب.

const STATUS_LABELS = {
  assigned: 'بانتظار الاستلام',
  picked_up: 'تم الاستلام',
  delivered: 'تم التسليم للزبون',
  confirmed: 'مؤكَّد من الزبون',
  refused: 'رفضه الزبون',
  failed: 'فشل التوصيل',
  cancelled: 'ملغى',
};

const DIRECTION_LABELS = {
  outbound: 'توصيل للزبون',
  return: 'إرجاع للبائع',
};

// التبويبات المعروضة أعلى القائمة وما يقابلها من قيمة status في الـ API.
// null = الكل.
const TABS = [
  { key: 'active', label: 'نشطة', statuses: ['assigned', 'picked_up', 'delivered'] },
  { key: 'assigned', label: 'بانتظار الاستلام', statuses: ['assigned'] },
  { key: 'done', label: 'مكتملة', statuses: ['confirmed', 'refused', 'failed', 'cancelled'] },
];

// ترتيب المحافظات الثابت — يطابق core/iraq_governorates.py على الباكند، حتى
// تظهر خيارات الفلتر بترتيب معروف بدل ترتيب الظهور العشوائي في البيانات.
const GOVERNORATE_ORDER = [
  'بغداد', 'البصرة', 'نينوى', 'أربيل', 'النجف', 'كربلاء', 'كركوك', 'الأنبار',
  'بابل', 'ذي قار', 'ديالى', 'واسط', 'ميسان', 'المثنى', 'القادسية',
  'صلاح الدين', 'دهوك', 'السليمانية',
];
const UNSET_GOVERNORATE_LABEL = 'غير محدد';

const state = {
  tab: 'active',
  governorate: 'all',
  orders: [],
  loading: false,
  error: null,
  refreshTimer: null,
};

const els = {
  loginView: document.getElementById('login-view'),
  dashboardView: document.getElementById('dashboard-view'),
  loginForm: document.getElementById('login-form'),
  loginError: document.getElementById('login-error'),
  loginButton: document.getElementById('login-button'),
  accountLabel: document.getElementById('account-label'),
  logoutButton: document.getElementById('logout-button'),
  refreshButton: document.getElementById('refresh-button'),
  tabs: document.getElementById('tabs'),
  governorateFilter: document.getElementById('governorate-filter'),
  ordersList: document.getElementById('orders-list'),
  emptyState: document.getElementById('empty-state'),
  errorBanner: document.getElementById('error-banner'),
};

function fmtDateTime(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return d.toLocaleString('ar-IQ', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function fmtMoney(n) {
  const num = Number(n) || 0;
  return num.toLocaleString('ar-IQ') + ' د.ع';
}

function showView(view) {
  els.loginView.classList.toggle('hidden', view !== 'login');
  els.dashboardView.classList.toggle('hidden', view !== 'dashboard');
}

function renderTabs() {
  els.tabs.innerHTML = '';
  for (const tab of TABS) {
    const btn = document.createElement('button');
    btn.className = 'tab' + (tab.key === state.tab ? ' tab--active' : '');
    btn.textContent = tab.label;
    btn.addEventListener('click', () => {
      state.tab = tab.key;
      renderTabs();
      loadOrders();
    });
    els.tabs.appendChild(btn);
  }
}

function orderCardHtml(order) {
  const statusLabel = STATUS_LABELS[order.status] || order.status;
  const directionLabel = DIRECTION_LABELS[order.direction] || order.direction;
  const isReturn = order.direction === 'return';
  const governorateLabel = order.from_governorate || UNSET_GOVERNORATE_LABEL;

  const hasCash = order.cash_to_collect > 0;

  let actionHtml = '';
  if (order.status === 'assigned') {
    actionHtml = `<button class="btn btn--primary" data-action="pick-up" data-id="${order.id}">تأكيد الاستلام</button>`;
  } else if (order.status === 'picked_up' && !isReturn) {
    actionHtml = `<button class="btn btn--primary" data-action="mark-arrived" data-id="${order.id}">تأكيد الوصول للزبون</button>`;
  } else if (order.status === 'picked_up' && isReturn) {
    actionHtml = `<span class="hint">تم استلام المنتج — سيُعاد المبلغ للزبون تلقائياً</span>`;
  } else if (order.status === 'delivered') {
    actionHtml = `<span class="hint">${hasCash ? 'بانتظار تأكيد الزبون داخل التطبيق واستلامه النقد' : 'بانتظار تأكيد الزبون داخل التطبيق'}</span>`;
  }

  // عند التحصيل النقدي تحتجز الشركة رسمها الصافي من المبلغ وتسلّم الباقي،
  // فعرض الإجمالي وحده يوهمها بأن كل ما تجمعه مستحق للمنصّة.
  const cashHtml = hasCash
    ? `<div class="row row--highlight"><span>${order.cash_collected ? 'كاش حُصِّل من الزبون ✓' : 'كاش سيُحصَّل من الزبون عند تأكيده الاستلام'}</span><b>${fmtMoney(order.cash_to_collect)}</b></div>`
      + `<div class="row"><span>رسم التوصيل (صافي لكم)</span><b>${fmtMoney(order.company_fee_share)}</b></div>`
      + `<div class="row"><span>يُسلَّم للمنصّة</span><b>${fmtMoney(order.cash_due_to_platform)}</b></div>`
    : '';

  const notesHtml = order.special_notes
    ? `<div class="row"><span>ملاحظات</span><b>${escapeHtml(order.special_notes)}</b></div>`
    : '';

  const failureHtml = order.failure_reason
    ? `<div class="row row--danger"><span>السبب</span><b>${escapeHtml(order.failure_reason)}</b></div>`
    : '';

  const fromMap = order.from_maps_url
    ? `<a class="map-link" href="${escapeHtml(order.from_maps_url)}" target="_blank" rel="noopener">📍 فتح الموقع</a>`
    : '';
  const toMap = order.to_maps_url
    ? `<a class="map-link" href="${escapeHtml(order.to_maps_url)}" target="_blank" rel="noopener">📍 فتح الموقع</a>`
    : '';

  return `
    <article class="card" data-order-id="${order.id}">
      <div class="card__head">
        <span class="badge badge--${order.direction}">${directionLabel}</span>
        <span class="badge badge--status-${order.status}">${statusLabel}</span>
        <span class="badge badge--gov">📍 ${escapeHtml(governorateLabel)}</span>
      </div>
      <h3 class="card__title">${escapeHtml(order.item_summary)} <span class="muted">× ${order.quantity}</span></h3>
      <div class="party">
        <div class="party__col">
          <span class="party__label">من</span>
          <b>${escapeHtml(order.from_name)}</b>
          <a href="tel:${escapeHtml(order.from_phone)}">${escapeHtml(order.from_phone)}</a>
          <span class="party__addr">${escapeHtml(order.from_address)}</span>
          ${fromMap}
        </div>
        <div class="party__arrow">←</div>
        <div class="party__col">
          <span class="party__label">إلى</span>
          <b>${escapeHtml(order.to_name)}</b>
          <a href="tel:${escapeHtml(order.to_phone)}">${escapeHtml(order.to_phone)}</a>
          <span class="party__addr">${escapeHtml(order.to_address)}</span>
          ${toMap}
        </div>
      </div>
      ${cashHtml}
      ${notesHtml}
      ${failureHtml}
      <div class="card__foot">
        <span class="muted small">#${order.id} · أُسند ${fmtDateTime(order.assigned_at) || ''}</span>
        <button class="btn btn--ghost btn--copy" data-action="copy" data-id="${order.id}">📋 نسخ بيانات الطلب</button>
        ${actionHtml}
      </div>
    </article>
  `;
}

/** رحلة واحدة (نفس الزبون/العنوان) تضم عدة طلبات — رسم توصيل واحد فقط بينها.
 * انظر ReservationViewSet.send_to_delivery_batch على الباكند. */
function batchGroupHtml(orders) {
  const first = orders[0];
  const governorateLabel = first.from_governorate || UNSET_GOVERNORATE_LABEL;
  return `
    <section class="batch-group">
      <div class="batch-group__head">
        <span>🔗 رحلة مدموجة — ${orders.length} طلبات لنفس الزبون، بعنوان واحد · 📍 ${escapeHtml(governorateLabel)}</span>
        <span class="batch-group__head-right">
          <span class="muted small">أُسندت ${fmtDateTime(first.assigned_at) || ''}</span>
          <button class="btn btn--ghost btn--copy" data-action="copy-batch" data-batch="${first.batch}">📋 نسخ بيانات الرحلة</button>
        </span>
      </div>
      ${orders.map(orderCardHtml).join('')}
    </section>
  `;
}

function orderCopyText(order) {
  const lines = [
    `📦 طلب #${order.id} — ${order.item_summary} × ${order.quantity}`,
    `من: ${order.from_name} — ${order.from_phone}`,
    `العنوان: ${order.from_address}`,
    `إلى: ${order.to_name} — ${order.to_phone}`,
    `العنوان: ${order.to_address}`,
  ];
  if (order.cash_to_collect > 0) {
    lines.push(`المبلغ المطلوب تحصيله: ${fmtMoney(order.cash_to_collect)}`);
  }
  if (order.special_notes) {
    lines.push(`ملاحظات: ${order.special_notes}`);
  }
  return lines.join('\n');
}

/** رحلة مدموجة تشترك في نفس طرفَي "من"/"إلى" — نص واحد يجمع كل الأصناف
 * بدل تكرار عنوان الاستلام/التسليم لكل طلب على حدة. */
function batchCopyText(orders) {
  const first = orders[0];
  const lines = [
    `🔗 رحلة مدموجة — ${orders.length} طلبات`,
    `من: ${first.from_name} — ${first.from_phone}`,
    `العنوان: ${first.from_address}`,
    `إلى: ${first.to_name} — ${first.to_phone}`,
    `العنوان: ${first.to_address}`,
    'الأصناف:',
    ...orders.map((o) => `  • ${o.item_summary} × ${o.quantity}`),
  ];
  const totalCash = orders.reduce((sum, o) => sum + (Number(o.cash_to_collect) || 0), 0);
  if (totalCash > 0) {
    const totalDue = orders.reduce((sum, o) => sum + (Number(o.cash_due_to_platform) || 0), 0);
    lines.push(`إجمالي المبلغ المطلوب تحصيله: ${fmtMoney(totalCash)}`);
    lines.push(`منه يُسلَّم للمنصّة: ${fmtMoney(totalDue)}`);
  }
  return lines.join('\n');
}

/** ينسخ للحافظة عبر Clipboard API، مع بديل قديم (execCommand) للحالات التي
 * لا يتوفر فيها (سياق غير آمن مثلاً) بدل ما يفشل الزر بصمت. */
async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  document.body.removeChild(textarea);
}

function flashCopied(btn) {
  const original = btn.textContent;
  btn.textContent = '✓ تم النسخ';
  btn.disabled = true;
  setTimeout(() => {
    btn.textContent = original;
    btn.disabled = false;
  }, 1500);
}

async function handleCopy(btn, text) {
  try {
    await copyToClipboard(text);
    flashCopied(btn);
  } catch (e) {
    state.error = 'تعذّر النسخ للحافظة';
    renderOrders();
  }
}

function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

/** يبني خيارات فلتر المحافظة من المحافظات الموجودة فعلاً في state.orders —
 * فقط ما له طلبات يظهر كخيار. يحافظ على الاختيار الحالي إن ظل صالحاً، وإلا
 * يرجعه لـ "كل المحافظات" بدل ما يعلّق على خيار بلا نتائج بصمت. */
function renderGovernorateFilter() {
  const present = new Set(state.orders.map((o) => o.from_governorate || UNSET_GOVERNORATE_LABEL));
  const options = GOVERNORATE_ORDER.filter((g) => present.has(g));
  if (present.has(UNSET_GOVERNORATE_LABEL)) options.push(UNSET_GOVERNORATE_LABEL);

  if (state.governorate !== 'all' && !options.includes(state.governorate)) {
    state.governorate = 'all';
  }

  els.governorateFilter.innerHTML = [
    `<option value="all">كل المحافظات</option>`,
    ...options.map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`),
  ].join('');
  els.governorateFilter.value = state.governorate;
}

function renderOrders() {
  els.errorBanner.classList.toggle('hidden', !state.error);
  els.errorBanner.textContent = state.error || '';

  renderGovernorateFilter();

  if (state.loading && state.orders.length === 0) {
    els.ordersList.innerHTML = '<p class="muted">جارِ التحميل…</p>';
    els.emptyState.classList.add('hidden');
    return;
  }

  const visibleOrders =
    state.governorate === 'all'
      ? state.orders
      : state.orders.filter((o) => (o.from_governorate || UNSET_GOVERNORATE_LABEL) === state.governorate);

  if (visibleOrders.length === 0) {
    els.ordersList.innerHTML = '';
    els.emptyState.textContent =
      state.orders.length === 0
        ? 'لا توجد طلبات في هذا القسم حالياً.'
        : 'لا توجد طلبات من هذه المحافظة في هذا القسم.';
    els.emptyState.classList.remove('hidden');
    return;
  }

  els.emptyState.classList.add('hidden');

  // visibleOrders هي أصلاً مرتّبة حسب الأحدث — نمر عليها بنفس الترتيب، ونعرض
  // كل دفعة مدموجة (نفس batch) مرة واحدة فقط، في موضع أول ظهور لها.
  const ordersByBatch = new Map();
  for (const order of visibleOrders) {
    if (order.batch == null) continue;
    if (!ordersByBatch.has(order.batch)) ordersByBatch.set(order.batch, []);
    ordersByBatch.get(order.batch).push(order);
  }

  const renderedBatches = new Set();
  const html = [];
  for (const order of visibleOrders) {
    if (order.batch != null) {
      if (renderedBatches.has(order.batch)) continue;
      renderedBatches.add(order.batch);
      html.push(batchGroupHtml(ordersByBatch.get(order.batch)));
    } else {
      html.push(orderCardHtml(order));
    }
  }
  els.ordersList.innerHTML = html.join('');
}

/** إذا فقدنا الجلسة (رفض تجديد التوكن) نعيد المستخدم لشاشة الدخول فوراً
 * بدل ترك لوحة تحكم معطوبة تعرض خطأً دائماً. */
function bounceToLoginIfSessionLost() {
  if (!Auth.isLoggedIn()) {
    stopAutoRefresh();
    showView('login');
    return true;
  }
  return false;
}

async function loadOrders() {
  state.loading = true;
  state.error = null;
  renderOrders();
  try {
    const tab = TABS.find((t) => t.key === state.tab);
    // فلترة الحالة تتم بجلب كل حالة على حدة ودمجها، لأن الـ API يقبل قيمة
    // واحدة لكل معامل status — التبويبات تحتاج أكثر من حالة أحياناً.
    const results = await Promise.all(
      tab.statuses.map((s) => apiFetchJson(`/delivery-orders/?status=${s}&page_size=100`))
    );
    const merged = results.flatMap((r) => r.results || r || []);
    merged.sort((a, b) => new Date(b.assigned_at) - new Date(a.assigned_at));
    state.orders = merged;
  } catch (e) {
    if (bounceToLoginIfSessionLost()) return;
    state.error = e.message || 'فشل تحميل الطلبات';
  } finally {
    state.loading = false;
    renderOrders();
  }
}

async function handleAction(action, id) {
  const endpoint =
    action === 'pick-up' ? `/delivery-orders/${id}/pick-up/` : `/delivery-orders/${id}/mark-arrived/`;
  try {
    await apiFetchJson(endpoint, { method: 'POST' });
    await loadOrders();
  } catch (e) {
    if (bounceToLoginIfSessionLost()) return;
    state.error = e.message || 'فشل تنفيذ الإجراء';
    renderOrders();
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  state.refreshTimer = setInterval(loadOrders, 30000);
}

function stopAutoRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

function enterDashboard() {
  const tokens = Auth.getTokens();
  els.accountLabel.textContent = tokens?.username || '';
  showView('dashboard');
  renderTabs();
  loadOrders();
  startAutoRefresh();
}

// ── الأحداث ──────────────────────────────────────────────────────────────

els.loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  els.loginError.classList.add('hidden');
  els.loginButton.disabled = true;
  els.loginButton.textContent = 'جارِ الدخول…';
  try {
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    await Auth.login(username, password);
    enterDashboard();
  } catch (err) {
    els.loginError.textContent = err.message || 'فشل تسجيل الدخول';
    els.loginError.classList.remove('hidden');
  } finally {
    els.loginButton.disabled = false;
    els.loginButton.textContent = 'دخول';
  }
});

els.logoutButton.addEventListener('click', () => {
  Auth.logout();
  stopAutoRefresh();
  showView('login');
});

els.refreshButton.addEventListener('click', loadOrders);

els.governorateFilter.addEventListener('change', () => {
  state.governorate = els.governorateFilter.value;
  renderOrders();
});

els.ordersList.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;

  if (btn.dataset.action === 'copy') {
    const order = state.orders.find((o) => String(o.id) === btn.dataset.id);
    if (order) handleCopy(btn, orderCopyText(order));
    return;
  }
  if (btn.dataset.action === 'copy-batch') {
    const batchId = btn.dataset.batch;
    const orders = state.orders.filter((o) => String(o.batch) === batchId);
    if (orders.length) handleCopy(btn, batchCopyText(orders));
    return;
  }

  btn.disabled = true;
  handleAction(btn.dataset.action, btn.dataset.id);
});

// ── نقطة الانطلاق ─────────────────────────────────────────────────────────

if (Auth.isLoggedIn()) {
  enterDashboard();
} else {
  showView('login');
}
