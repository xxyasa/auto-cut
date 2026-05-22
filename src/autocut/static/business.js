/** src/autocut/static/business.js */

// ==========================================
// Config & State
// ==========================================
const API_BASE = '/api/business';
const STORAGE_KEYS = {
  remixDuration: 'autocut_remix_duration',
  remixPlanCount: 'autocut_remix_plan_count',
  remixPlanCountAuto: 'autocut_remix_plan_count_auto',
};
const MAX_TAGS = 200;
const STATE = {
  token: localStorage.getItem('autocut_api_token') || '',
  pollTimer: null,
  detailPollTimer: null,
  currentJobId: null,
  currentJob: null,
  brands: [],
  products: [],
  jobs: [],
  selectedJobIds: new Set(),
  expandedChipContainers: new Set(),
  batchDownloading: false,
  generatingRemixPlan: false,
  uploadedFile: null,     // 单文件兼容（非 upload tab 用）
  uploadedFiles: [],      // 批量上传文件列表
  // 上一次成功渲染的任务列表签名，用于跳过无变化的 DOM 重建（性能优化）
  lastJobsSignature: '',
  // Combobox 实例（init() 阶段创建）
  brandCombo: null,
  productCombo: null,
  asrCombo: null,
  // 当前选中的品牌/产品 id（用于联想词即时 PATCH）
  currentBrandId: null,
  currentProductId: null,
};

// ==========================================
// Utilities
// ==========================================
const utils = {
  getToken() {
    return document.getElementById('api-token').value || STATE.token;
  },

  escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  },
  
  async fetchApi(url, options = {}) {
    const token = this.getToken();
    if (!token) {
      ui.toast('请输入 API Token', 'error');
      return null;
    }
    
    const headers = {
      'Authorization': `Bearer ${token}`,
      ...options.headers
    };
    
    if (options.body && !(options.body instanceof FormData) && typeof options.body !== 'string') {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(options.body);
    }
    
    try {
      const response = await fetch(`${API_BASE}${url}`, { ...options, headers });
      if (response.status === 401) {
        ui.toast('Token 无效或已过期，请重新输入', 'error');
        document.getElementById('api-token').focus();
        return null;
      }
      
      let data = null;
      const contentType = response.headers.get('content-type');
      if (contentType && contentType.includes('application/json')) {
        data = await response.json();
      }
      
      if (!response.ok) {
        let msg = `请求失败: ${response.status}`;
        if (data && data.detail) {
          msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        }
        ui.toast(msg, 'error');
        return null;
      }
      
      return data || true;
    } catch (err) {
      console.error(err);
      ui.toast('网络错误，请重试', 'error');
      return null;
    }
  },
  
  formatDate(ts) {
    if (ts === null || ts === undefined || ts === '') return '-';
    let d;
    if (typeof ts === 'number') {
      // 兼容：秒级 (10 位) 或 毫秒级 (13 位)
      d = new Date(ts < 1e12 ? ts * 1000 : ts);
    } else if (typeof ts === 'string') {
      // 纯数字字符串
      if (/^\d+$/.test(ts)) {
        const n = Number(ts);
        d = new Date(n < 1e12 ? n * 1000 : n);
      } else {
        // ISO 字符串 (后端 _now_iso 输出)
        d = new Date(ts);
      }
    } else {
      d = new Date(ts);
    }
    if (isNaN(d.getTime())) return '-';
    return d.toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  },
  
  getFilename(path) {
    if (!path) return '';
    return path.split(/[/\\]/).pop();
  },

  jobBrand(job) {
    return String(job.brand_name || '').trim();
  },

  jobProduct(job) {
    const product = String(job.product_name || '').trim();
    if (product) return product;
    const display = String(job.display_name || '').trim();
    if (!display) return '';
    const parts = display.split('-');
    return parts.length > 1 ? parts.slice(1).join('-') : display;
  },

  isJobDownloadable(job) {
    return Boolean(job.downloadable) || (
      ['done', 'partial_success'].includes(job.status) &&
      job.artifacts &&
      Array.isArray(job.artifacts.remix_plans) &&
      job.artifacts.remix_plans.length > 0 &&
      (!job.tracks_result || job.tracks_result.remix !== 'failed')
    );
  },

  statusLabel(status) {
    const labels = {
      queued: '排队中',
      downloading: '下载中',
      running: '处理中',
      remixing: '生成成片',
      done: '已完成',
      partial_success: '部分成功',
      failed: '失败',
    };
    return labels[status] || status || '-';
  },

  jobPlanSummary(job) {
    const plans = job.artifacts && Array.isArray(job.artifacts.remix_plans)
      ? job.artifacts.remix_plans
      : [];
    if (plans.length === 0) return '';
    const scores = plans
      .map(plan => Number(plan.quality && plan.quality.score))
      .filter(score => Number.isFinite(score));
    const bestScore = scores.length ? Math.max(...scores) : null;
    return bestScore === null ? `${plans.length}个方案` : `${plans.length}个方案 · 最高${bestScore}分`;
  },

  // 把时间戳格式化为任务名称：可选前缀-YYYYMMDD-HHmmss-任务-seq
  formatTaskName(ts, seq, prefix = '') {
    const safePrefix = String(prefix || '').trim();
    const suffix = safePrefix ? `${safePrefix}-` : '';
    if (!ts) return `${suffix}任务-${seq}`;
    let d;
    if (typeof ts === 'number') {
      d = new Date(ts < 1e12 ? ts * 1000 : ts);
    } else if (typeof ts === 'string') {
      d = /^\d+$/.test(ts) ? new Date(Number(ts) < 1e12 ? Number(ts) * 1000 : Number(ts)) : new Date(ts);
    } else {
      d = new Date(ts);
    }
    if (isNaN(d.getTime())) return `${suffix}任务-${seq}`;
    const pad = n => String(n).padStart(2, '0');
    const date = `${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`;
    const time = `${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    return `${suffix}${date}-${time}-任务-${seq}`;
  }
};

// ==========================================
// UI Functions
// ==========================================
const ui = {
  toast(msg, type = 'info') {
    const container = document.getElementById('toast-container');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      el.style.transform = 'translateX(100%)';
      setTimeout(() => el.remove(), 300);
    }, 3000);
  },
  
  switchTab(tabGroupSelector, targetTab, contentPrefix) {
    const root = document.querySelector(tabGroupSelector);
    if (!root) return;
    const attr = contentPrefix.includes('detail') ? 'data-detail-tab' : 'data-tab';
    const tabs = root.querySelectorAll('.tab');
    tabs.forEach(t => t.classList.remove('active'));
    const targetEl = root.querySelector(`.tab[${attr}="${targetTab}"]`);
    if (targetEl) targetEl.classList.add('active');

    // 只切换"内容容器"，不要把 tab 栏自身（.tabs）也隐藏。
    document.querySelectorAll(`[id^="${contentPrefix}"]`).forEach(el => {
      if (el.classList.contains('tabs')) return; // 跳过 tab 栏本身（如 #source-tabs / #detail-tabs）
      el.classList.add('hidden');
      el.classList.remove('flex');
    });
    const content = document.getElementById(`${contentPrefix}${targetTab}`);
    if (content) {
      content.classList.remove('hidden');
      content.classList.add('flex');
    }
  },
  
  showModal(id) {
    document.getElementById(id).classList.add('active');
    const input = document.querySelector(`#${id} input`);
    if (input) {
      setTimeout(() => input.focus(), 100);
    }
  },
  
  hideModal(id) {
    document.getElementById(id).classList.remove('active');
  },
  
  renderChips(containerId, items, onRemove) {
    const container = document.getElementById(containerId);
    container.__chipItems = items || [];
    container.__chipOnRemove = onRemove || null;
    container.innerHTML = '';
    const visibleItems = (items || []).slice(0, MAX_TAGS);
    const expanded = STATE.expandedChipContainers.has(containerId);
    container.classList.add('chip-container-collapsible');
    container.classList.toggle('expanded', expanded);
    container.classList.toggle('collapsed', !expanded);
    visibleItems.forEach((item, index) => {
      const el = document.createElement('div');
      el.className = 'chip';
      el.textContent = item;
      
      if (onRemove) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.innerHTML = '×';
        btn.onclick = () => onRemove(index);
        el.appendChild(btn);
      }
      container.appendChild(el);
    });
    this.renderChipToggle(container, containerId, visibleItems.length, items.length);
  },

  renderChipToggle(container, containerId, visibleCount, totalCount) {
    let toggle = document.getElementById(`${containerId}-toggle`);
    if (!toggle) {
      toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.id = `${containerId}-toggle`;
      toggle.className = 'chip-toggle btn btn-secondary btn-sm';
      container.insertAdjacentElement('afterend', toggle);
    }
    const expanded = STATE.expandedChipContainers.has(containerId);
    toggle.textContent = expanded ? '收起' : `展开全部${totalCount > MAX_TAGS ? `（前${MAX_TAGS}个）` : ''}`;
    toggle.onclick = () => {
      if (STATE.expandedChipContainers.has(containerId)) {
        STATE.expandedChipContainers.delete(containerId);
      } else {
        STATE.expandedChipContainers.add(containerId);
      }
      this.renderChips(containerId, container.__chipItems || [], container.__chipOnRemove || null);
    };
    requestAnimationFrame(() => {
      const shouldShow = totalCount > MAX_TAGS || container.scrollHeight > container.clientHeight + 2 || expanded;
      toggle.hidden = !shouldShow || visibleCount === 0;
    });
  },
  
  renderSuggestedChips(containerId, items, onAdd) {
    const container = document.getElementById(containerId);
    container.innerHTML = '';
    if (!items || items.length === 0) return;
    
    items.forEach(item => {
      const el = document.createElement('div');
      el.className = 'chip suggested';
      el.textContent = `+ ${item}`;
      el.onclick = () => onAdd(item);
      container.appendChild(el);
    });
  }
};

window.closeModal = ui.hideModal;

// ==========================================
// Form State Management
// ==========================================
const formState = {
  sellingPoints: [],
  assocWords: [],

  // 新建产品 modal 临时状态
  newProductSellingPoints: [],
  newProductAssocs: [],

  pushTag(list, val) {
    if (!val || list.includes(val)) return false;
    if (list.length >= MAX_TAGS) {
      ui.toast(`最多支持 ${MAX_TAGS} 个标签`, 'warning');
      return false;
    }
    list.push(val);
    return true;
  },

  renderSellingPoints() {
    ui.renderChips('selling-points-chips', this.sellingPoints, i => {
      this.sellingPoints.splice(i, 1);
      this.renderSellingPoints();
    });
  },

  renderAssocWords() {
    ui.renderChips('assoc-chips', this.assocWords, i => {
      this.assocWords.splice(i, 1);
      this.renderAssocWords();
      apiOps.saveAssocDebounced();
    });
  },

  renderNewProductSellingPoints() {
    ui.renderChips('new-product-selling-points-chips', this.newProductSellingPoints, i => {
      this.newProductSellingPoints.splice(i, 1);
      this.renderNewProductSellingPoints();
    });
  },

  renderNewProductAssocs() {
    ui.renderChips('new-product-assoc-chips', this.newProductAssocs, i => {
      this.newProductAssocs.splice(i, 1);
      this.renderNewProductAssocs();
    });
  },
  
  addSellingPoint(val) {
    if (!this.pushTag(this.sellingPoints, val)) return;
    this.renderSellingPoints();
  },
  
  addAssoc(val) {
    if (!this.pushTag(this.assocWords, val)) return;
    this.renderAssocWords();
    apiOps.saveAssocDebounced();
  },

  addNewProductSellingPoint(val) {
    if (!this.pushTag(this.newProductSellingPoints, val)) return;
    this.renderNewProductSellingPoints();
  },

  addNewProductAssoc(val) {
    if (!this.pushTag(this.newProductAssocs, val)) return;
    this.renderNewProductAssocs();
  },

  resetNewProduct() {
    this.newProductSellingPoints = [];
    this.newProductAssocs = [];
    this.renderNewProductSellingPoints();
    this.renderNewProductAssocs();
    const nameInput = document.getElementById('new-product-name');
    const spInput = document.getElementById('new-product-selling-point-input');
    const asInput = document.getElementById('new-product-assoc-input');
    if (nameInput) nameInput.value = '';
    if (spInput) spInput.value = '';
    if (asInput) asInput.value = '';
  }
};

// ==========================================
// API Operations
// ==========================================
const apiOps = {
  async loadBrands() {
    const res = await utils.fetchApi('/brands');
    if (!res) return;
    STATE.brands = res.brands || [];

    if (STATE.brandCombo) {
      STATE.brandCombo.setOptions(STATE.brands.map(b => ({ value: b.id, label: b.name })));
    }
    if (STATE.productCombo) {
      STATE.productCombo.setOptions([]);
      STATE.productCombo.setDisabled(true);
    }
    document.getElementById('btn-new-product').disabled = true;
    document.getElementById('btn-del-product').disabled = true;

    // 默认选中第一个品牌（如果存在），并联动加载产品列表
    if (STATE.brands.length > 0 && STATE.brandCombo) {
      STATE.brandCombo.setValue(STATE.brands[0].id);
      await this.loadProducts(STATE.brands[0].id);
    }
  },
  
  async loadProducts(brandId) {
    if (!brandId) return;
    const res = await utils.fetchApi(`/brands/${brandId}/products`);
    if (!res) return;
    STATE.products = res.products || [];

    if (STATE.productCombo) {
      STATE.productCombo.setOptions(STATE.products.map(p => ({ value: p.id, label: p.name })));
      STATE.productCombo.setDisabled(STATE.products.length === 0);
    }
    const hasProducts = STATE.products.length > 0;
    document.getElementById('btn-new-product').disabled = false;
    document.getElementById('btn-del-product').disabled = !hasProducts;

    // 默认选中第一个产品，并把卖点/联想词回填到表单
    if (hasProducts && STATE.productCombo) {
      STATE.productCombo.setValue(STATE.products[0].id);
      this.loadProductDetails(STATE.products[0]);
    }
  },
  
  async createBrand(name) {
    const res = await utils.fetchApi('/brands', {
      method: 'POST',
      body: { name, associations: [] }
    });
    if (res) {
      ui.toast('品牌创建成功', 'success');
      ui.hideModal('modal-brand');
      await this.loadBrands();
      if (STATE.brandCombo) STATE.brandCombo.setValue(res.id);
      await this.loadProducts(res.id);
    }
  },
  
  async createProduct(brandId, name, sellingPoints = [], associations = []) {
    const res = await utils.fetchApi(`/brands/${brandId}/products`, {
      method: 'POST',
      body: {
        name,
        selling_points: sellingPoints,
        associations,
      }
    });
    if (res) {
      ui.toast('产品创建成功', 'success');
      ui.hideModal('modal-product');
      await this.loadProducts(brandId);
      if (STATE.productCombo) STATE.productCombo.setValue(res.id);
      this.loadProductDetails(res);
    }
  },
  
  loadProductDetails(product) {
    if (!product) return;
    // 记录当前产品，用于即时 PATCH 保存联想词
    STATE.currentBrandId = STATE.brandCombo ? STATE.brandCombo.getValue() : null;
    STATE.currentProductId = product.id || null;
    formState.sellingPoints = [...(product.selling_points || [])].slice(0, MAX_TAGS);
    formState.assocWords = [...(product.associations || [])].slice(0, MAX_TAGS);
    formState.renderSellingPoints();
    formState.renderAssocWords();
  },

  // 500ms debounce，防止快速多次修改时频繁请求
  _saveAssocTimer: null,
  saveAssocDebounced() {
    clearTimeout(this._saveAssocTimer);
    this._saveAssocTimer = setTimeout(() => this.saveAssoc(), 500);
  },

  async saveAssoc() {
    if (!STATE.currentBrandId || !STATE.currentProductId) return;
    await utils.fetchApi(
      `/brands/${STATE.currentBrandId}/products/${STATE.currentProductId}`,
      { method: 'PATCH', body: { associations: [...formState.assocWords] } }
    );
  },

  async deleteProduct() {
    const brandId = (STATE.brandCombo ? STATE.brandCombo.getValue() : null) || STATE.currentBrandId;
    const productId = (STATE.productCombo ? STATE.productCombo.getValue() : null) || STATE.currentProductId;
    if (!brandId || !productId) return ui.toast('请先选择产品', 'error');
    const product = STATE.products.find(p => p.id === productId);
    const name = product ? product.name : productId;

    // 用自定义 modal 二次确认，避免浏览器原生 confirm 弹窗
    document.getElementById('modal-confirm-delete-msg').textContent = `确认删除产品「${name}」？此操作不可撤销。`;
    ui.showModal('modal-confirm-delete');

    // 绑定一次性确认按钮（先移除旧的，避免多次绑定）
    const okBtn = document.getElementById('modal-confirm-delete-ok');
    const cancelBtns = document.querySelectorAll('#modal-confirm-delete .btn-secondary');
    if (okBtn._deleteHandler) okBtn.removeEventListener('click', okBtn._deleteHandler);

    const onConfirm = async () => {
      okBtn.removeEventListener('click', onConfirm);
      okBtn._deleteHandler = null;
      ui.hideModal('modal-confirm-delete');
      const res = await utils.fetchApi(`/brands/${brandId}/products/${productId}`, { method: 'DELETE' });
      if (res !== null) {
        ui.toast('产品已删除', 'success');
        STATE.currentProductId = null;
        STATE.currentBrandId = brandId;
        await this.loadProducts(brandId);
      }
    };
    okBtn._deleteHandler = onConfirm;
    okBtn.addEventListener('click', onConfirm);
  },
  
  async uploadFile(file) {
    const fd = new FormData();
    fd.append('file', file);

    const statusEl = document.getElementById('upload-status');
    statusEl.textContent = '上传中...';

    const res = await utils.fetchApi('/upload', {
      method: 'POST',
      body: fd
    });

    if (res) {
      STATE.uploadedFile = res;
      statusEl.textContent = `${res.filename} (已上传)`;
      document.getElementById('upload-zone').style.borderColor = 'var(--success)';
    } else {
      statusEl.textContent = '上传失败';
    }
    return res;
  },

  // 批量上传多个文件，渲染 batch-file-list
  async uploadFiles(files) {
    STATE.uploadedFiles = [];
    const statusEl = document.getElementById('upload-status');
    const listEl = document.getElementById('batch-file-list');

    if (files.length === 1) {
      // 单文件走原逻辑
      listEl.classList.add('hidden');
      await this.uploadFile(files[0]);
      return;
    }

    // 多文件
    statusEl.textContent = `已选择 ${files.length} 个文件，上传中...`;
    listEl.classList.remove('hidden');
    listEl.innerHTML = '';
    document.getElementById('upload-zone').style.borderColor = '';

    for (const file of files) {
      const item = document.createElement('div');
      item.className = 'batch-file-item';
      item.textContent = `${file.name} 上传中...`;
      listEl.appendChild(item);

      const fd = new FormData();
      fd.append('file', file);
      const res = await utils.fetchApi('/upload', { method: 'POST', body: fd });
      if (res) {
        STATE.uploadedFiles.push(res);
        item.textContent = `✓ ${file.name}`;
        item.classList.add('done');
      } else {
        item.textContent = `✗ ${file.name} 上传失败`;
        item.classList.add('error');
      }
    }
    statusEl.textContent = `${STATE.uploadedFiles.length}/${files.length} 个文件上传完成`;
  },
  
  async suggestAssoc() {
    const mode = document.querySelector('input[name="product-mode"]:checked').value;
    let payload = null;
    
    if (mode === 'brand') {
      const brandId = STATE.brandCombo ? STATE.brandCombo.getValue() : null;
      const productId = STATE.productCombo ? STATE.productCombo.getValue() : null;
      if (!brandId) return ui.toast('请先选择品牌');
      payload = { brand_id: brandId };
      if (productId) payload.product_id = productId;
    } else {
      const name = document.getElementById('temp-product-name').value;
      if (!name) return ui.toast('请先填写产品名称');
      payload = { product_name: name, selling_points: formState.sellingPoints };
    }
    
    // API 实际上只要 product_name 和 selling_points
    // 为了兼容，如果选择了产品，我们要找到产品的 name 和 selling points
    if (mode === 'brand' && payload.product_id) {
      const p = STATE.products.find(x => x.id === payload.product_id);
      if (p) {
        payload.product_name = p.name;
        payload.selling_points = p.selling_points || [];
      }
    } else if (mode === 'brand') {
      const b = STATE.brands.find(x => x.id === payload.brand_id);
      if (b) {
        payload.product_name = b.name;
        payload.selling_points = [];
      }
    }
    
    const brandIdStr = mode === 'brand' ? (STATE.brandCombo ? STATE.brandCombo.getValue() : null) : 'temp';
    const postUrl = mode === 'brand' ? `/brands/${brandIdStr}/suggest-associations` : `/brands/temp/suggest-associations`;
    
    // 补齐字段
    const data = {
      product_name: payload.product_name || '未知产品',
      selling_points: payload.selling_points || []
    };
    
    const res = await utils.fetchApi(`/brands/${brandIdStr}/suggest-associations`, {
      method: 'POST',
      body: data
    });
    
    if (res && res.suggestions) {
      const news = res.suggestions.filter(s => !formState.assocWords.includes(s));
      ui.renderSuggestedChips('suggest-chips', news, (item) => {
        formState.addAssoc(item);
        // 移除已添加的 chip
        const chips = Array.from(document.getElementById('suggest-chips').children);
        const target = chips.find(el => el.textContent === `+ ${item}`);
        if (target) target.remove();
        // 若建议词全部添加完，隐藏全选按钮
        if (document.getElementById('suggest-chips').childElementCount === 0) {
          document.getElementById('btn-suggest-all').hidden = true;
        }
      });
      document.getElementById('btn-suggest-all').hidden = news.length === 0;
    }
  },
  
  async createJob() {
    const payload = {
      source: {},
      brand_product: {
        extra_terms: formState.assocWords
      },
      tracks: [],
      remix: {},
      asr_engine: STATE.asrCombo ? STATE.asrCombo.getValue() : 'whisper-api'
    };
    
    // Source
    const activeSourceTab = document.querySelector('.tabs [data-tab].active').dataset.tab;
    if (activeSourceTab === 'upload') {
      // 先占位，后面批量时会替换
      if (!STATE.uploadedFile && STATE.uploadedFiles.length === 0) return ui.toast('请先上传视频', 'error');
      payload.source = STATE.uploadedFile
        ? { type: 'upload', value: STATE.uploadedFile.filename, uploaded_path: STATE.uploadedFile.uploaded_path }
        : { type: 'upload', value: STATE.uploadedFiles[0].filename, uploaded_path: STATE.uploadedFiles[0].uploaded_path };
    } else if (activeSourceTab === 'oss') {
      const v = document.getElementById('oss-input').value;
      if (!v) return ui.toast('请填写 OSS 链接', 'error');
      payload.source = { type: 'oss', value: v };
    } else if (activeSourceTab === 'url') {
      const v = document.getElementById('url-input').value;
      if (!v) return ui.toast('请填写 URL', 'error');
      payload.source = { type: 'url', value: v };
    }
    
    // Brand Product
    const mode = document.querySelector('input[name="product-mode"]:checked').value;
    if (mode === 'brand') {
      payload.brand_product.brand_id = STATE.brandCombo ? STATE.brandCombo.getValue() : null;
      payload.brand_product.product_id = STATE.productCombo ? STATE.productCombo.getValue() : null;
      if (!payload.brand_product.brand_id) return ui.toast('请选择品牌', 'error');
    } else {
      payload.brand_product.product = document.getElementById('temp-product-name').value;
      payload.brand_product.selling_points = formState.sellingPoints;
      if (!payload.brand_product.product) return ui.toast('请填写产品名称', 'error');
    }
    
    // Tracks
    if (document.getElementById('track-enabled').checked) payload.tracks.push('enabled');
    if (document.getElementById('track-remix').checked) payload.tracks.push('remix');
    if (payload.tracks.length === 0) return ui.toast('请至少选择一个生成轨道', 'error');
    
    // Remix opts
    if (payload.tracks.includes('remix')) {
      payload.remix = {
        target_duration: parseFloat(document.getElementById('remix-duration').value),
        use_llm: document.getElementById('remix-use-llm').checked,
        plan_count: Math.min(5, Math.max(1, parseInt(document.getElementById('remix-plan-count').value || '1', 10))),
        plan_count_auto: document.getElementById('remix-plan-count-auto').checked,
        stream: false
      };
    }
    
    // ASR
    if (payload.asr_engine === 'transcript') {
      const tp = document.getElementById('transcript-path').value;
      if (tp) payload.transcript_path = tp;
    }
    
    // 提交：多文件时批量创建
    if (activeSourceTab === 'upload' && STATE.uploadedFiles.length > 1) {
      let created = 0;
      for (const uploaded of STATE.uploadedFiles) {
        const batchPayload = JSON.parse(JSON.stringify(payload));
        batchPayload.source = {
          type: 'upload',
          value: uploaded.filename,
          uploaded_path: uploaded.uploaded_path
        };
        const r = await utils.fetchApi('/jobs', { method: 'POST', body: batchPayload });
        if (r) created++;
      }
      if (created > 0) {
        ui.toast(`已创建 ${created} 个任务`, 'success');
        this.pollJobs();
      }
      return;
    }

    const res = await utils.fetchApi('/jobs', {
      method: 'POST',
      body: payload
    });
    
    if (res) {
      ui.toast('任务创建成功', 'success');
      this.pollJobs();
    }
  },
  
  async pollJobs() {
    const res = await utils.fetchApi('/jobs?limit=200');
    if (!res || !res.jobs) return;

    // 计算签名：仅包含影响渲染的字段。若与上一次一致，跳过 DOM 重建，
    // 避免每次 5s 轮询打断用户交互（例如打开中的 <select> 下拉）。
    const signature = res.jobs
      .map(j => `${j.id}|${j.display_name || ''}|${j.brand_name || ''}|${j.product_name || ''}|${j.downloadable ? 1 : 0}|${j.status}|${j.stage || ''}|${j.progress || 0}|${j.queued_at || ''}|${j.updated_at || ''}`)
      .join(';');
    if (signature !== STATE.lastJobsSignature) {
      STATE.jobs = res.jobs;
      syncSelectedJobsWithLatestData();
      updateJobFilterOptions(res.jobs);
      renderJobList();
      STATE.lastJobsSignature = signature;
    }
    
    // refresh detail if opened
    if (STATE.currentJobId) {
      const activeJob = res.jobs.find(j => j.id === STATE.currentJobId);
      if (activeJob) updateDetailView(activeJob);
    }
  },
  
  async getJobLog(id) {
    const res = await utils.fetchApi(`/jobs/${id}/log?tail=200`);
    if (res && res.lines) {
      const el = document.getElementById('detail-log');
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
      el.textContent = res.lines.join('\n');
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
  }
};

function syncSelectedJobsWithLatestData() {
  const byId = new Map(STATE.jobs.map(job => [job.id, job]));
  for (const jobId of Array.from(STATE.selectedJobIds)) {
    const job = byId.get(jobId);
    if (!job || !utils.isJobDownloadable(job)) {
      STATE.selectedJobIds.delete(jobId);
    }
  }
}

function updateJobFilterOptions(jobs) {
  const brandSelect = document.getElementById('job-filter-brand');
  const productSelect = document.getElementById('job-filter-product');
  if (!brandSelect || !productSelect) return;
  const currentBrand = brandSelect.value;
  const currentProduct = productSelect.value;
  const brands = Array.from(new Set(jobs.map(utils.jobBrand).filter(Boolean))).sort();
  const products = Array.from(new Set(
    jobs
      .filter(job => !currentBrand || utils.jobBrand(job) === currentBrand)
      .map(utils.jobProduct)
      .filter(Boolean)
  )).sort();
  renderSelectOptions(brandSelect, '全部品牌', brands, currentBrand);
  renderSelectOptions(productSelect, '全部产品', products, currentProduct);
}

function renderSelectOptions(select, emptyLabel, values, currentValue) {
  select.innerHTML = `<option value="">${emptyLabel}</option>`;
  values.forEach(value => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  select.value = values.includes(currentValue) ? currentValue : '';
}

function filteredJobs() {
  const brand = document.getElementById('job-filter-brand')?.value || '';
  const product = document.getElementById('job-filter-product')?.value || '';
  const start = document.getElementById('job-filter-start')?.value || '';
  const end = document.getElementById('job-filter-end')?.value || '';
  const startTime = start ? new Date(`${start}T00:00:00`).getTime() : null;
  const endTime = end ? new Date(`${end}T23:59:59.999`).getTime() : null;
  return STATE.jobs.filter(job => {
    if (brand && utils.jobBrand(job) !== brand) return false;
    if (product && utils.jobProduct(job) !== product) return false;
    const ts = new Date(job.queued_at || job.updated_at || 0).getTime();
    if (startTime !== null && (!ts || ts < startTime)) return false;
    if (endTime !== null && (!ts || ts > endTime)) return false;
    return true;
  });
}

function renderJobList() {
  const container = document.getElementById('jobs-list');
  const emptyState = document.getElementById('empty-state');
  if (!container || !emptyState) return;

  const jobs = filteredJobs();
  if (STATE.jobs.length === 0) {
    container.replaceChildren();
    emptyState.textContent = '还没有任务，去左侧创建一个吧';
    emptyState.classList.remove('hidden');
    updateBatchDownloadButton();
    return;
  }
  if (jobs.length === 0) {
    container.replaceChildren();
    emptyState.textContent = '没有符合筛选条件的任务';
    emptyState.classList.remove('hidden');
    updateBatchDownloadButton();
    return;
  }

  emptyState.classList.add('hidden');
  const totalJobs = jobs.length;
  const frag = document.createDocumentFragment();
  jobs.forEach((job, idx) => {
    const downloadable = utils.isJobDownloadable(job);
    const checked = STATE.selectedJobIds.has(job.id);
    const el = document.createElement('div');
    el.className = `job-item${downloadable ? '' : ' job-item-disabled-select'}`;

    const seq = String(totalJobs - idx).padStart(3, '0');
    const taskName = utils.formatTaskName(job.queued_at || job.updated_at, seq, job.display_name || '');
    const planSummary = utils.jobPlanSummary(job);
    el.innerHTML = `
      <label class="job-select" title="${downloadable ? '选择用于批量下载' : '仅已完成且有成片方案的任务可批量下载'}">
        <input type="checkbox" data-job-select="${utils.escapeHtml(job.id)}" ${checked ? 'checked' : ''} ${downloadable ? '' : 'disabled'}>
      </label>
      <div class="job-open-area" role="button" tabindex="0">
        <div class="job-info">
          <div class="job-id">${utils.escapeHtml(taskName)}</div>
          <div class="job-meta">
            <span>${utils.escapeHtml(utils.formatDate(job.queued_at || job.updated_at))}</span>
            <span>${utils.escapeHtml(job.stage || '-')}</span>
            ${planSummary ? `<span>${utils.escapeHtml(planSummary)}</span>` : ''}
          </div>
          ${job.status === 'running' || job.status === 'downloading' ? `
          <div class="job-progress-bar">
            <div class="job-progress-fill" style="width:${Math.round((job.progress || 0) * 100)}%"></div>
          </div>` : ''}
        </div>
        <div class="badge ${job.status}">${utils.escapeHtml(utils.statusLabel(job.status))}</div>
      </div>
    `;
    const checkbox = el.querySelector('[data-job-select]');
    const openArea = el.querySelector('.job-open-area');
    checkbox.addEventListener('click', event => event.stopPropagation());
    checkbox.addEventListener('change', event => {
      if (event.target.checked) {
        STATE.selectedJobIds.add(job.id);
      } else {
        STATE.selectedJobIds.delete(job.id);
      }
      updateBatchDownloadButton();
    });
    openArea.addEventListener('click', () => showJobDetail(job.id));
    openArea.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        showJobDetail(job.id);
      }
    });
    frag.appendChild(el);
  });
  container.replaceChildren(frag);
  updateBatchDownloadButton();
}

function visibleDownloadableJobs() {
  return filteredJobs().filter(utils.isJobDownloadable);
}

function updateBatchDownloadButton() {
  const btn = document.getElementById('btn-batch-download-jobs');
  if (!btn) return;
  const count = STATE.selectedJobIds.size;
  btn.textContent = STATE.batchDownloading ? `正在打包 (${count})...` : `批量下载 (${count})`;
  btn.disabled = count === 0 || STATE.batchDownloading;
}

function selectVisibleJobs() {
  visibleDownloadableJobs().forEach(job => STATE.selectedJobIds.add(job.id));
  renderJobList();
}

function clearSelectedJobs() {
  STATE.selectedJobIds.clear();
  renderJobList();
}

async function batchDownloadSelectedJobs() {
  const ids = Array.from(STATE.selectedJobIds);
  if (ids.length === 0 || STATE.batchDownloading) return;
  const url = `${API_BASE}/jobs/batch-remix-segments.zip?ids=${encodeURIComponent(ids.join(','))}`;
  STATE.batchDownloading = true;
  updateBatchDownloadButton();
  try {
    const response = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${utils.getToken()}`
      }
    });
    if (!response.ok) {
      let message = `批量下载失败: ${response.status}`;
      try {
        const data = await response.json();
        if (data && data.detail) message = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      } catch (_) {}
      ui.toast(message, 'error');
      return;
    }
    const blob = await response.blob();
    const downloadUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = filenameFromDisposition(response.headers.get('content-disposition')) || `autocut_batch_remix_segments_${Date.now()}.zip`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(downloadUrl), 30000);
    ui.toast('批量下载已生成', 'success');
  } catch (err) {
    console.error(err);
    ui.toast('批量下载失败，请重试', 'error');
  } finally {
    STATE.batchDownloading = false;
    updateBatchDownloadButton();
  }
}

function filenameFromDisposition(disposition) {
  if (!disposition) return '';
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match) return decodeURIComponent(utf8Match[1]);
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return match ? match[1] : '';
}

function clearJobFilters() {
  ['job-filter-brand', 'job-filter-product', 'job-filter-start', 'job-filter-end'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  updateJobFilterOptions(STATE.jobs);
  renderJobList();
}

// ==========================================
// Detail View Logic
// ==========================================
function showJobDetail(id) {
  STATE.currentJobId = id;
  document.getElementById('view-list').classList.add('hidden');
  document.getElementById('view-detail').classList.remove('hidden');
  document.getElementById('view-detail').classList.add('flex');
  
  ui.switchTab('#detail-tabs', 'log', 'detail-');
  STATE.detailTabAutoSelected = false;

  // immediate fetch
  fetchJobDetailOnce(id);
  
  // clear & start detail poll
  if (STATE.detailPollTimer) clearInterval(STATE.detailPollTimer);
  STATE.detailPollTimer = setInterval(() => fetchJobDetailOnce(id), 2000);
}

function hideJobDetail() {
  STATE.currentJobId = null;
  STATE.currentJob = null;
  document.getElementById('view-detail').classList.add('hidden');
  document.getElementById('view-detail').classList.remove('flex');
  document.getElementById('view-list').classList.remove('hidden');
  document.getElementById('view-list').classList.add('flex');
  if (STATE.detailPollTimer) {
    clearInterval(STATE.detailPollTimer);
    STATE.detailPollTimer = null;
  }
}

async function fetchJobDetailOnce(id) {
  const job = await utils.fetchApi(`/jobs/${id}`);
  if (job) {
    updateDetailView(job);
    if (!['queued', 'downloading', 'running'].includes(job.status)) {
      // stop fast poll if done
      if (STATE.detailPollTimer) {
        clearInterval(STATE.detailPollTimer);
        STATE.detailPollTimer = null;
      }
    }
  }
  apiOps.getJobLog(id);
}

function updateDetailView(job) {
  STATE.currentJob = job;
  document.getElementById('detail-id').textContent = job.id;
  
  const statusEl = document.getElementById('detail-status');
  statusEl.className = `badge ${job.status}`;
  statusEl.textContent = utils.statusLabel(job.status);
  
  document.getElementById('detail-stage').textContent = job.stage || '-';
  document.getElementById('detail-progress').style.width = `${(job.progress || 0) * 100}%`;
  
  // Video tabs setup
  const art = job.artifacts || {};
  
  if (art.enabled_mp4) {
    const fn = utils.getFilename(art.enabled_mp4);
    const url = `/api/business/jobs/${job.id}/artifacts/${fn}`;
    const zipUrl = `/api/business/jobs/${job.id}/artifacts/${job.id}_enabled_segments.zip`;
    document.getElementById('video-enabled').src = url;
    document.getElementById('link-enabled').href = url;
    document.getElementById('link-enabled-zip').href = zipUrl;
    document.querySelector('.tab[data-detail-tab="enabled"]').style.display = 'block';
  } else {
    document.querySelector('.tab[data-detail-tab="enabled"]').style.display = 'none';
  }
  
  const remixPlans = normalizeRemixPlans(job);
  if (remixPlans.length > 0) {
    renderRemixPlans(job.id, remixPlans);
    document.querySelector('.tab[data-detail-tab="remix"]').style.display = 'block';
    if (!STATE.detailTabAutoSelected) {
      STATE.detailTabAutoSelected = true;
      ui.switchTab('#detail-tabs', 'remix', 'detail-');
    }
  } else {
    document.querySelector('.tab[data-detail-tab="remix"]').style.display = 'none';
  }
}

function normalizeRemixPlans(job) {
  const art = job.artifacts || {};
  if (Array.isArray(art.remix_plans) && art.remix_plans.length > 0) {
    return art.remix_plans;
  }
  if (art.remix_mp4) {
    return [{ index: 1, label: '方案 1', mp4: art.remix_mp4 }];
  }
  return [];
}

function renderRemixPlans(jobId, plans) {
  const container = document.getElementById('remix-plans-container');
  if (!container) return;
  container.innerHTML = '';

  // 全部下载按钮：有多个方案时才显示
  const linkAllPlans = document.getElementById('link-all-plans-zip');
  if (linkAllPlans) {
    linkAllPlans.href = `/api/business/jobs/${jobId}/artifacts/${jobId}_all_plans_segments.zip`;
    linkAllPlans.classList.toggle('hidden', plans.length <= 1);
  }
  updateGenerateRemixPlanButton(plans);

  plans.forEach((plan, idx) => {
    const mp4 = plan.mp4 || plan.video_path || '';
    const fn = utils.getFilename(mp4);
    if (!fn) return;
    const url = `/api/business/jobs/${jobId}/artifacts/${fn}`;
    const zipUrl = `/api/business/jobs/${jobId}/artifacts/${zipNameForRemixPlan(jobId, plan, idx)}`;
    const scriptText = remixPlanScriptText(plan);
    const quality = plan.quality || {};
    const risks = Array.isArray(quality.risks) ? quality.risks : [];
    const qualityLevel = quality.level || 'unknown';
    const qualityScore = Number.isFinite(Number(quality.score)) ? Number(quality.score) : null;
    const card = document.createElement('div');
    card.className = 'card remix-plan-card';
    card.innerHTML = `
      <div class="flex justify-between items-center mb-2">
        <strong>${plan.label || `方案 ${idx + 1}`}</strong>
        <div class="remix-plan-summary">
          ${qualityScore === null ? '' : `<span class="quality-pill quality-${utils.escapeHtml(qualityLevel)}">${qualityScore}分 · ${utils.escapeHtml(quality.summary || qualityLevel)}</span>`}
          <span class="text-sm text-muted">${plan.duration ? `${Number(plan.duration).toFixed(1)}s` : ''}${plan.duplicate ? ' · 可能重复' : ''}</span>
        </div>
      </div>
      ${risks.length ? `<div class="remix-risk-strip">${risks.slice(0, 5).map(risk => `
        <span class="risk-chip risk-${utils.escapeHtml(risk.severity || 'low')}" title="${utils.escapeHtml(risk.message || '')}">${utils.escapeHtml(risk.label || risk.type || '风险')}</span>
      `).join('')}</div>` : '<div class="remix-risk-strip"><span class="risk-chip risk-good">未发现明显风险</span></div>'}
      <div class="remix-plan-body">
        <div class="remix-plan-video">
          <div class="video-container"><video controls src="${url}"></video></div>
          <div class="remix-plan-actions">
            <a class="btn btn-secondary btn-sm text-center" href="${url}" target="_blank" download>合并下载 MP4</a>
            <a class="btn btn-secondary btn-sm text-center" href="${zipUrl}" target="_blank" download>分开下载 ZIP</a>
          </div>
        </div>
        <div class="remix-plan-script">
          <div class="remix-plan-script-title">
            <span>口播文案</span>
            ${risks.length ? `<span class="text-muted">${risks.length} 个提示</span>` : ''}
          </div>
          <div class="remix-plan-script-text">${scriptText ? utils.escapeHtml(scriptText) : '<span class="text-muted">暂无文案</span>'}</div>
          ${risks.length ? `<div class="remix-risk-notes">${risks.slice(0, 4).map(risk => `
            <div><strong>${utils.escapeHtml(risk.label || risk.type || '提示')}：</strong>${utils.escapeHtml(risk.message || '')}</div>
          `).join('')}</div>` : ''}
        </div>
      </div>
    `;
    container.appendChild(card);
  });
}

function updateGenerateRemixPlanButton(plans) {
  const btn = document.getElementById('btn-generate-remix-plan');
  if (!btn) return;
  if (STATE.generatingRemixPlan) {
    btn.disabled = true;
    btn.textContent = '生成中...';
    return;
  }
  if (plans.length >= 5) {
    btn.disabled = true;
    btn.textContent = '已达到方案生成上限';
    return;
  }
  btn.disabled = false;
  btn.textContent = '生成新方案';
}

async function generateRemixPlan() {
  if (!STATE.currentJobId || STATE.generatingRemixPlan) return;
  const currentPlans = normalizeRemixPlans(STATE.currentJob || { artifacts: {} });
  if (currentPlans.length >= 5) {
    updateGenerateRemixPlanButton(currentPlans);
    return;
  }
  STATE.generatingRemixPlan = true;
  updateGenerateRemixPlanButton(currentPlans);
  try {
    const updatedJob = await utils.fetchApi(`/jobs/${STATE.currentJobId}/remix-plans`, {
      method: 'POST',
      body: {
        stream: false
      }
    });
    if (updatedJob) {
      ui.toast('新方案已生成', 'success');
      updateDetailView(updatedJob);
      apiOps.pollJobs();
    }
  } finally {
    STATE.generatingRemixPlan = false;
    const refreshedPlans = normalizeRemixPlans(STATE.currentJob || { artifacts: {} });
    updateGenerateRemixPlanButton(refreshedPlans);
  }
}

function remixPlanScriptText(plan) {
  if (plan.script_text) return String(plan.script_text).trim();
  if (Array.isArray(plan.items)) {
    return plan.items
      .map(item => item.text || item.clean_text || item.raw_text || '')
      .filter(Boolean)
      .join('\n')
      .trim();
  }
  return '';
}

function zipNameForRemixPlan(jobId, plan, idx) {
  if (plan.index && Number(plan.index) > 1) {
    return `${jobId}_remix_v${Number(plan.index)}_segments.zip`;
  }
  if (idx > 0) {
    return `${jobId}_remix_v${idx + 1}_segments.zip`;
  }
  return `${jobId}_remix_segments.zip`;
}

// ==========================================
// Initialization & Event Bindings
// ==========================================
function bindEvents() {
  const tokenInput = document.getElementById('api-token');
  tokenInput.value = STATE.token;
  tokenInput.addEventListener('change', (e) => {
    localStorage.setItem('autocut_api_token', e.target.value);
    STATE.token = e.target.value;
    // 同步写入 cookie，供 <video>/<a> 等浏览器原生请求携带鉴权
    document.cookie = `autocut_token=${encodeURIComponent(e.target.value)}; path=/; SameSite=Strict`;
    apiOps.loadBrands();
    apiOps.pollJobs();
  });
  
  document.getElementById('btn-theme').addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('autocut_theme', next);
  });
  
  document.getElementById('btn-refresh').addEventListener('click', () => {
    apiOps.pollJobs();
  });

  document.getElementById('job-filter-brand').addEventListener('change', () => {
    updateJobFilterOptions(STATE.jobs);
    renderJobList();
  });
  ['job-filter-product', 'job-filter-start', 'job-filter-end'].forEach(id => {
    document.getElementById(id).addEventListener('change', renderJobList);
  });
  document.getElementById('btn-clear-job-filters').addEventListener('click', clearJobFilters);
  document.getElementById('btn-select-visible-jobs').addEventListener('click', selectVisibleJobs);
  document.getElementById('btn-clear-selected-jobs').addEventListener('click', clearSelectedJobs);
  document.getElementById('btn-batch-download-jobs').addEventListener('click', batchDownloadSelectedJobs);
  document.getElementById('btn-generate-remix-plan').addEventListener('click', generateRemixPlan);

  document.getElementById('remix-duration').addEventListener('change', e => {
    const value = Math.min(60, Math.max(5, parseFloat(e.target.value || '30')));
    e.target.value = value;
    localStorage.setItem(STORAGE_KEYS.remixDuration, String(value));
  });
  document.getElementById('remix-plan-count').addEventListener('change', e => {
    const value = Math.min(5, Math.max(1, parseInt(e.target.value || '2', 10)));
    e.target.value = value;
    localStorage.setItem(STORAGE_KEYS.remixPlanCount, String(value));
  });
  document.getElementById('remix-plan-count-auto').addEventListener('change', e => {
    localStorage.setItem(STORAGE_KEYS.remixPlanCountAuto, e.target.checked ? '1' : '0');
    updateRemixPlanCountMode();
  });
  
  // Product Mode Toggle
  document.querySelectorAll('input[name="product-mode"]').forEach(r => {
    r.addEventListener('change', e => {
      const mode = e.target.value;
      document.getElementById('brand-mode-wrap').classList.toggle('hidden', mode !== 'brand');
      document.getElementById('temp-mode-wrap').classList.toggle('hidden', mode !== 'temp');
    });
  });
  
  // Brand & Product selection - 已在 init() 阶段通过 Combobox onChange 绑定
  
  // Modals
  document.getElementById('btn-new-brand').addEventListener('click', () => ui.showModal('modal-brand'));
  document.getElementById('btn-new-product').addEventListener('click', () => {
    if (!STATE.brandCombo || !STATE.brandCombo.getValue()) return ui.toast('请先选择品牌', 'error');
    formState.resetNewProduct();
    ui.showModal('modal-product');
  });

  document.getElementById('btn-del-product').addEventListener('click', () => {
    apiOps.deleteProduct();
  });
  
  document.getElementById('submit-new-brand').addEventListener('click', () => {
    apiOps.createBrand(document.getElementById('new-brand-name').value);
  });
  
  document.getElementById('submit-new-product').addEventListener('click', () => {
    const b = STATE.brandCombo ? STATE.brandCombo.getValue() : null;
    const name = document.getElementById('new-product-name').value.trim();
    if (!name) return ui.toast('请输入产品名称', 'error');
    apiOps.createProduct(
      b,
      name,
      [...formState.newProductSellingPoints],
      [...formState.newProductAssocs],
    );
  });

  // 新建产品 modal 内卖点 / 联想词 回车添加
  const npSpInput = document.getElementById('new-product-selling-point-input');
  if (npSpInput) {
    npSpInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        formState.addNewProductSellingPoint(e.target.value.trim());
        e.target.value = '';
      }
    });
  }
  const npAsInput = document.getElementById('new-product-assoc-input');
  if (npAsInput) {
    npAsInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        formState.addNewProductAssoc(e.target.value.trim());
        e.target.value = '';
      }
    });
  }
  
  // Chips
  const spInput = document.getElementById('temp-selling-point');
  spInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      formState.addSellingPoint(e.target.value.trim());
      e.target.value = '';
    }
  });
  
  const assocInput = document.getElementById('assoc-input');
  assocInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      formState.addAssoc(e.target.value.trim());
      e.target.value = '';
    }
  });
  
  document.getElementById('btn-suggest').addEventListener('click', () => {
    apiOps.suggestAssoc();
  });

  document.getElementById('btn-suggest-all').addEventListener('click', () => {
    const container = document.getElementById('suggest-chips');
    // 收集所有建议词文本（chip 里 textContent 是 "+ 词"）
    const items = Array.from(container.children).map(el => el.textContent.replace(/^\+ /, ''));
    items.forEach(item => formState.addAssoc(item));
    container.replaceChildren();
    document.getElementById('btn-suggest-all').hidden = true;
  });
  
  // Source Tabs
  document.querySelectorAll('#source-tabs [data-tab]').forEach(tab => {
    tab.addEventListener('click', e => {
      ui.switchTab('#source-tabs', e.target.dataset.tab, 'source-');
    });
  });
  
  // Detail Tabs
  document.querySelectorAll('#detail-tabs [data-detail-tab]').forEach(tab => {
    tab.addEventListener('click', e => {
      ui.switchTab('#detail-tabs', e.target.dataset.detailTab, 'detail-');
    });
  });
  
  // File Upload
  const uploadZone = document.getElementById('upload-zone');
  const fileInput = document.getElementById('file-input');
  uploadZone.addEventListener('click', () => fileInput.click());
  
  uploadZone.addEventListener('dragover', e => {
    e.preventDefault();
    uploadZone.classList.add('drag-over');
  });
  
  uploadZone.addEventListener('dragleave', () => {
    uploadZone.classList.remove('drag-over');
  });
  
  uploadZone.addEventListener('drop', e => {
    e.preventDefault();
    uploadZone.classList.remove('drag-over');
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      apiOps.uploadFiles(Array.from(e.dataTransfer.files));
    }
  });

  fileInput.addEventListener('change', e => {
    if (e.target.files && e.target.files.length > 0) {
      apiOps.uploadFiles(Array.from(e.target.files));
    }
  });
  
  // ASR - transcript-path 显隐已在 init() 阶段通过 asrCombo onChange 绑定
  
  // Detail Back
  document.getElementById('btn-back').addEventListener('click', hideJobDetail);
  
  // Form Submit
  document.getElementById('job-form').addEventListener('submit', e => {
    e.preventDefault();
    apiOps.createJob();
  });
  
  // Global Shortcuts
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
      e.preventDefault();
      document.getElementById('api-token').focus();
    }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      apiOps.createJob();
    }
    if (e.key === 'Escape') {
      ui.hideModal('modal-brand');
      ui.hideModal('modal-product');
    }
  });
}

function init() {
  restoreRemixSettings();

  // 实例化 Combobox（必须在 bindEvents/loadBrands 之前，因为 STATE.*Combo 会被它们引用）
  STATE.brandCombo = new Combobox(document.getElementById('brand-combobox'), {
    placeholder: '-- 选择品牌 --',
    searchable: true,
    onChange: value => {
      apiOps.loadProducts(value);
    },
  });
  STATE.productCombo = new Combobox(document.getElementById('product-combobox'), {
    placeholder: '-- 选择产品 --',
    searchable: true,
    disabled: true,
    onChange: value => {
      const p = STATE.products.find(x => x.id === value);
      if (p) apiOps.loadProductDetails(p);
    },
  });
  STATE.asrCombo = new Combobox(document.getElementById('asr-engine-combobox'), {
    placeholder: '-- 选择 ASR 引擎 --',
    searchable: false,
    options: [
      { value: 'transcript', label: 'transcript' },
      { value: 'faster-whisper', label: 'faster-whisper' },
      { value: 'funasr', label: 'funasr' },
      { value: 'glm-asr', label: 'glm-asr (默认，高精度)' },
      { value: 'whisper-api', label: 'whisper-large-v3 API' },
    ],
    onChange: value => {
      document.getElementById('transcript-path-wrap').classList.toggle('hidden', value !== 'transcript');
    },
  });
  STATE.asrCombo.setValue('whisper-api');
  document.getElementById('transcript-path-wrap').classList.add('hidden');

  bindEvents();
  if (STATE.token) {
    // 页面加载时同步 cookie，确保 <video> 标签能直接访问受保护的 mp4
    document.cookie = `autocut_token=${encodeURIComponent(STATE.token)}; path=/; SameSite=Strict`;
    apiOps.loadBrands();
    apiOps.pollJobs();
    STATE.pollTimer = setInterval(() => {
      if (!STATE.currentJobId) {
        apiOps.pollJobs();
      }
    }, 5000);
  }
}

function restoreRemixSettings() {
  const durationInput = document.getElementById('remix-duration');
  const planCountInput = document.getElementById('remix-plan-count');
  const planCountAutoInput = document.getElementById('remix-plan-count-auto');
  const storedDuration = parseFloat(localStorage.getItem(STORAGE_KEYS.remixDuration) || '');
  if (Number.isFinite(storedDuration)) {
    durationInput.value = Math.min(60, Math.max(5, storedDuration));
  }
  const storedPlanCount = parseInt(localStorage.getItem(STORAGE_KEYS.remixPlanCount) || '', 10);
  if (Number.isFinite(storedPlanCount)) {
    planCountInput.value = Math.min(5, Math.max(1, storedPlanCount));
  }
  planCountAutoInput.checked = localStorage.getItem(STORAGE_KEYS.remixPlanCountAuto) !== '0';
  updateRemixPlanCountMode();
}

function updateRemixPlanCountMode() {
  const autoInput = document.getElementById('remix-plan-count-auto');
  const planCountInput = document.getElementById('remix-plan-count');
  if (!autoInput || !planCountInput) return;
  planCountInput.disabled = autoInput.checked;
}

document.addEventListener('DOMContentLoaded', init);
