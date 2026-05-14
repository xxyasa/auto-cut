/** src/autocut/static/business.js */

// ==========================================
// Config & State
// ==========================================
const API_BASE = '/api/business';
const STATE = {
  token: localStorage.getItem('autocut_api_token') || '',
  pollTimer: null,
  detailPollTimer: null,
  currentJobId: null,
  brands: [],
  products: [],
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

  // 把时间戳格式化为任务名称：YYYYMMDD-HHmmss-任务-seq
  formatTaskName(ts, seq) {
    if (!ts) return `任务-${seq}`;
    let d;
    if (typeof ts === 'number') {
      d = new Date(ts < 1e12 ? ts * 1000 : ts);
    } else if (typeof ts === 'string') {
      d = /^\d+$/.test(ts) ? new Date(Number(ts) < 1e12 ? Number(ts) * 1000 : Number(ts)) : new Date(ts);
    } else {
      d = new Date(ts);
    }
    if (isNaN(d.getTime())) return `任务-${seq}`;
    const pad = n => String(n).padStart(2, '0');
    const date = `${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`;
    const time = `${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    return `${date}-${time}-任务-${seq}`;
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
    container.innerHTML = '';
    items.forEach((item, index) => {
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
  
  addSellingPoint(val) {
    if (!val || this.sellingPoints.includes(val)) return;
    this.sellingPoints.push(val);
    ui.renderChips('selling-points-chips', this.sellingPoints, i => {
      this.sellingPoints.splice(i, 1);
      ui.renderChips('selling-points-chips', this.sellingPoints, this.sellingPoints);
    });
  },
  
  addAssoc(val) {
    if (!val || this.assocWords.includes(val)) return;
    this.assocWords.push(val);
    ui.renderChips('assoc-chips', this.assocWords, i => {
      this.assocWords.splice(i, 1);
      ui.renderChips('assoc-chips', this.assocWords, this.assocWords);
      apiOps.saveAssocDebounced();
    });
    apiOps.saveAssocDebounced();
  },

  addNewProductSellingPoint(val) {
    if (!val || this.newProductSellingPoints.includes(val)) return;
    this.newProductSellingPoints.push(val);
    ui.renderChips('new-product-selling-points-chips', this.newProductSellingPoints, i => {
      this.newProductSellingPoints.splice(i, 1);
      ui.renderChips('new-product-selling-points-chips', this.newProductSellingPoints, this.newProductSellingPoints);
    });
  },

  addNewProductAssoc(val) {
    if (!val || this.newProductAssocs.includes(val)) return;
    this.newProductAssocs.push(val);
    ui.renderChips('new-product-assoc-chips', this.newProductAssocs, i => {
      this.newProductAssocs.splice(i, 1);
      ui.renderChips('new-product-assoc-chips', this.newProductAssocs, this.newProductAssocs);
    });
  },

  resetNewProduct() {
    this.newProductSellingPoints = [];
    this.newProductAssocs = [];
    const spChips = document.getElementById('new-product-selling-points-chips');
    const asChips = document.getElementById('new-product-assoc-chips');
    if (spChips) spChips.innerHTML = '';
    if (asChips) asChips.innerHTML = '';
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
    formState.sellingPoints = [...(product.selling_points || [])];
    formState.assocWords = [...(product.associations || [])];
    ui.renderChips('assoc-chips', formState.assocWords, i => {
      formState.assocWords.splice(i, 1);
      ui.renderChips('assoc-chips', formState.assocWords, formState.assocWords);
      apiOps.saveAssocDebounced();
    });
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
      asr_engine: STATE.asrCombo ? STATE.asrCombo.getValue() : 'glm-asr'
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
    const res = await utils.fetchApi('/jobs?limit=50');
    if (!res || !res.jobs) return;
    
    const container = document.getElementById('jobs-list');
    const emptyState = document.getElementById('empty-state');
    
    if (res.jobs.length === 0) {
      if (container.childElementCount > 0) container.innerHTML = '';
      emptyState.classList.remove('hidden');
      STATE.lastJobsSignature = '';
      return;
    }
    
    emptyState.classList.add('hidden');

    // 计算签名：仅包含影响渲染的字段。若与上一次一致，跳过 DOM 重建，
    // 避免每次 5s 轮询打断用户交互（例如打开中的 <select> 下拉）。
    const signature = res.jobs
      .map(j => `${j.id}|${j.status}|${j.stage || ''}|${j.progress || 0}|${j.queued_at || ''}|${j.updated_at || ''}`)
      .join(';');
    if (signature !== STATE.lastJobsSignature) {
      const totalJobs = res.jobs.length;
      const frag = document.createDocumentFragment();
      res.jobs.forEach((job, idx) => {
        const el = document.createElement('div');
        el.className = 'job-item';
        el.onclick = () => showJobDetail(job.id);

        // 任务名称：YYYYMMDD-HHmmss-任务-序号（最旧的是001）
        const seq = String(totalJobs - idx).padStart(3, '0');
        const taskName = utils.formatTaskName(job.queued_at || job.updated_at, seq);

        el.innerHTML = `
          <div class="job-info">
            <div class="job-id">${taskName}</div>
            <div class="job-meta">
              <span>${utils.formatDate(job.queued_at || job.updated_at)}</span>
              <span>${job.stage || '-'}</span>
            </div>
            ${job.status === 'running' || job.status === 'downloading' ? `
            <div class="job-progress-bar">
              <div class="job-progress-fill" style="width:${Math.round((job.progress || 0) * 100)}%"></div>
            </div>` : ''}
          </div>
          <div class="badge ${job.status}">${job.status}</div>
        `;
        frag.appendChild(el);
      });
      // 一次性 replace，比 innerHTML='' + 多次 appendChild 更平滑
      container.replaceChildren(frag);
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

// ==========================================
// Detail View Logic
// ==========================================
function showJobDetail(id) {
  STATE.currentJobId = id;
  document.getElementById('view-list').classList.add('hidden');
  document.getElementById('view-detail').classList.remove('hidden');
  document.getElementById('view-detail').classList.add('flex');
  
  ui.switchTab('#detail-tabs', 'log', 'detail-');
  
  // immediate fetch
  fetchJobDetailOnce(id);
  
  // clear & start detail poll
  if (STATE.detailPollTimer) clearInterval(STATE.detailPollTimer);
  STATE.detailPollTimer = setInterval(() => fetchJobDetailOnce(id), 2000);
}

function hideJobDetail() {
  STATE.currentJobId = null;
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
  document.getElementById('detail-id').textContent = job.id;
  
  const statusEl = document.getElementById('detail-status');
  statusEl.className = `badge ${job.status}`;
  statusEl.textContent = job.status;
  
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
  plans.forEach((plan, idx) => {
    const mp4 = plan.mp4 || plan.video_path || '';
    const fn = utils.getFilename(mp4);
    if (!fn) return;
    const url = `/api/business/jobs/${jobId}/artifacts/${fn}`;
    const zipUrl = `/api/business/jobs/${jobId}/artifacts/${zipNameForRemixPlan(jobId, plan, idx)}`;
    const card = document.createElement('div');
    card.className = 'card';
    card.style.padding = '12px';
    card.innerHTML = `
      <div class="flex justify-between items-center mb-2">
        <strong>${plan.label || `方案 ${idx + 1}`}</strong>
        <span class="text-sm text-muted">${plan.duration ? `${Number(plan.duration).toFixed(1)}s` : ''}${plan.duplicate ? ' · 可能重复' : ''}</span>
      </div>
      <div class="video-container"><video controls src="${url}"></video></div>
      <a class="btn btn-secondary btn-sm text-center" href="${url}" target="_blank" download>合并下载 MP4</a>
      <a class="btn btn-secondary btn-sm text-center" href="${zipUrl}" target="_blank" download>分开下载 ZIP</a>
    `;
    container.appendChild(card);
  });
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
    ],
    onChange: value => {
      document.getElementById('transcript-path-wrap').classList.toggle('hidden', value !== 'transcript');
    },
  });
  STATE.asrCombo.setValue('glm-asr');
  // glm-asr 不显示 transcript-path
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

document.addEventListener('DOMContentLoaded', init);
