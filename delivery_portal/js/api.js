// طبقة الاتصال بالـ API: تسجيل الدخول عبر JWT، تخزين التوكن، وتجديده تلقائياً
// عند انتهاء صلاحيته. لا اعتماد على أي مكتبة خارجية.

const TOKEN_KEY = 'delivery_portal_tokens';

const Auth = {
  getTokens() {
    const raw = localStorage.getItem(TOKEN_KEY);
    return raw ? JSON.parse(raw) : null;
  },

  setTokens(tokens) {
    localStorage.setItem(TOKEN_KEY, JSON.stringify(tokens));
  },

  clearTokens() {
    localStorage.removeItem(TOKEN_KEY);
  },

  isLoggedIn() {
    return !!this.getTokens();
  },

  async login(username, password) {
    const res = await fetch(`${CONFIG.API_BASE_URL}/auth/token/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      // 403 = كلمة صحيحة يرفضها الخادم لسبب يشرحه (كلمة مرور لم تعد مقبولة)،
      // فرسالته تُعرض كما هي. أي خطأ آخر يبقى رسالة واحدة لا تكشف شيئاً.
      let detail = null;
      if (res.status === 403) {
        try { detail = (await res.json()).detail; } catch (_) { /* not JSON */ }
      }
      throw new Error(typeof detail === 'string' && detail
        ? detail
        : 'اسم المستخدم أو كلمة المرور غير صحيحة');
    }
    const data = await res.json();
    this.setTokens({ access: data.access, refresh: data.refresh, username });
    return data;
  },

  logout() {
    this.clearTokens();
  },

  // طلب تجديد واحد في كل وقت: الخادم يُبطل رمز التجديد بعد استعماله،
  // وثلاثة طلبات متوازية كانت تستعمل الرمز نفسه فتُخرج الشركة.
  _refreshing: null,

  refresh() {
    if (!this._refreshing) {
      this._refreshing = this._doRefresh().finally(() => { this._refreshing = null; });
    }
    return this._refreshing;
  },

  async _doRefresh() {
    const tokens = this.getTokens();
    if (!tokens || !tokens.refresh) throw new Error('لا يوجد جلسة دخول');
    const res = await fetch(`${CONFIG.API_BASE_URL}/auth/token/refresh/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh: tokens.refresh }),
    });
    if (res.status === 400 || res.status === 401) {
      this.clearTokens();
      throw new Error('انتهت الجلسة، الرجاء تسجيل الدخول مجدداً');
    }
    if (!res.ok) throw new Error('تعذّر الاتصال بالخادم، حاول بعد قليل');
    const data = await res.json();
    this.setTokens({ ...tokens, access: data.access, refresh: data.refresh || tokens.refresh });
    return data.access;
  },
};

/**
 * fetch مصادَق عليه — يرفق Bearer token تلقائياً، ويحاول تجديد التوكن مرة
 * واحدة إذا رجع الخادم 401 قبل أن يفشل نهائياً.
 */
async function apiFetch(path, options = {}) {
  const tokens = Auth.getTokens();
  if (!tokens) throw new Error('الرجاء تسجيل الدخول');

  const doFetch = (accessToken) =>
    fetch(`${CONFIG.API_BASE_URL}${path}`, {
      ...options,
      headers: {
        Accept: 'application/json',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        Authorization: `Bearer ${accessToken}`,
        ...(options.headers || {}),
      },
    });

  let res = await doFetch(tokens.access);
  if (res.status === 401) {
    const newAccess = await Auth.refresh();
    res = await doFetch(newAccess);
  }
  return res;
}

/** يرمي رسالة الخطأ من body.message إن وُجدت، وإلا رسالة عامة. */
async function apiFetchJson(path, options = {}) {
  const res = await apiFetch(path, options);
  let body = null;
  try {
    body = await res.json();
  } catch (_) {
    // بلا محتوى (204 مثلاً)
  }
  if (!res.ok) {
    const message = (body && (body.message || body.detail)) || 'حدث خطأ غير متوقع';
    throw new Error(message);
  }
  return body;
}
