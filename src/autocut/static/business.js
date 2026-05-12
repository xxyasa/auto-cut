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
  uploadedFile: null,
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
    if (!ts) return '-';
    return new Date(ts * 1000).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  },
  
  getFilename(path) {
    if (!path) return '';
    return path.split(/[/\\]/).pop();
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
    const tabs = document.querySelectorAll(`${tabGroupSelector} .tab`);
    tabs.forEach(t => t.classList.remove('active'));
    document.querySelector(`${tabGroupSelector} .tab[data-${tabGroupSelector.includes('detail') ? 'detail-' : ''}tab="${targetTab}"]`).classList.add('active');
    
    document.querySelectorAll(`[id^="${contentPrefix}"]`).forEach(el => el.classList.add('hidden'));
    document.getElementById(`${contentPrefix}${targetTab}`).classList.remove('hidden');
    document.getElementById(`${contentPrefix}${targetTab}`).classList.add('flex');
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
    });
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
    
    const sel = document.getElementById('brand-select');
    sel.innerHTML = '<option value="">-- 选择品牌 --</option>';
    STATE.brands.forEach(b => {
      const opt = document.createElement('option');
      opt.value = b.id;
      opt.textContent = b.name;
      sel.appendChild(opt);
    });
    
    document.getElementById('product-select').innerHTML = '<option value="">-- 选择产品 --</option>';
    document.getElementById('product-select').disabled = true;
    document.getElementById('btn-new-product').disabled = true;
  },
  
  async loadProducts(brandId) {
    if (!brandId) return;
    const res = await utils.fetchApi(`/brands/${brandId}/products`);
    if (!res) return;
    STATE.products = res.products || [];
    
    const sel = document.getElementById('product-select');
    sel.innerHTML = '<option value="">-- 选择产品 --</option>';
    STATE.products.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      sel.appendChild(opt);
    });
    sel.disabled = false;
    document.getElementById('btn-new-product').disabled = false;
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
      document.getElementById('brand-select').value = res.id;
      await this.loadProducts(res.id);
    }
  },
  
  async createProduct(brandId, name) {
    const res = await utils.fetchApi(`/brands/${brandId}/products`, {
      method: 'POST',
      body: { name, selling_points: [], associations: [] }
    });
    if (res) {
      ui.toast('产品创建成功', 'success');
      ui.hideModal('modal-product');
      await this.loadProducts(brandId);
      document.getElementById('product-select').value = res.id;
      this.loadProductDetails(res);
    }
  },
  
  loadProductDetails(product) {
    if (!product) return;
    formState.sellingPoints = [...(product.selling_points || [])];
    formState.assocWords = [...(product.associations || [])];
    ui.renderChips('assoc-chips', formState.assocWords, i => {
      formState.assocWords.splice(i, 1);
      ui.renderChips('assoc-chips', formState.assocWords, formState.assocWords);
    });
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
  },
  
  async suggestAssoc() {
    const mode = document.querySelector('input[name="product-mode"]:checked').value;
    let payload = null;
    
    if (mode === 'brand') {
      const brandId = document.getElementById('brand-select').value;
      const productId = document.getElementById('product-select').value;
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
    
    const brandIdStr = mode === 'brand' ? document.getElementById('brand-select').value : 'temp';
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
        // hide the suggested chip once added
        const chips = Array.from(document.getElementById('suggest-chips').children);
        const target = chips.find(el => el.textContent === `+ ${item}`);
        if (target) target.remove();
      });
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
      asr_engine: document.getElementById('asr-engine').value
    };
    
    // Source
    const activeSourceTab = document.querySelector('.tabs [data-tab].active').dataset.tab;
    if (activeSourceTab === 'upload') {
      if (!STATE.uploadedFile) return ui.toast('请先上传视频', 'error');
      payload.source = {
        type: 'upload',
        value: STATE.uploadedFile.filename,
        uploaded_path: STATE.uploadedFile.uploaded_path
      };
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
      payload.brand_product.brand_id = document.getElementById('brand-select').value;
      payload.brand_product.product_id = document.getElementById('product-select').value;
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
        stream: false
      };
    }
    
    // ASR
    if (payload.asr_engine === 'transcript') {
      const tp = document.getElementById('transcript-path').value;
      if (tp) payload.transcript_path = tp;
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
      container.innerHTML = '';
      emptyState.classList.remove('hidden');
      return;
    }
    
    emptyState.classList.add('hidden');
    container.innerHTML = '';
    
    res.jobs.forEach(job => {
      const el = document.createElement('div');
      el.className = 'job-item';
      el.onclick = () => showJobDetail(job.id);
      
      const shortId = job.id.split('-')[0] || job.id;
      
      el.innerHTML = `
        <div class="job-info">
          <div class="job-id">${shortId}</div>
          <div class="job-meta">
            <span>${utils.formatDate(job.queued_at || job.updated_at)}</span>
            <span>${job.stage || '-'}</span>
          </div>
        </div>
        <div class="badge ${job.status}">${job.status}</div>
      `;
      container.appendChild(el);
    });
    
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
  
  ui.switchTab('.tabs', 'log', 'detail-');
  
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
    document.getElementById('video-enabled').src = url;
    document.getElementById('link-enabled').href = url;
    document.querySelector('.tab[data-detail-tab="enabled"]').style.display = 'block';
  } else {
    document.querySelector('.tab[data-detail-tab="enabled"]').style.display = 'none';
  }
  
  if (art.remix_mp4) {
    const fn = utils.getFilename(art.remix_mp4);
    const url = `/api/business/jobs/${job.id}/artifacts/${fn}`;
    document.getElementById('video-remix').src = url;
    document.getElementById('link-remix').href = url;
    document.querySelector('.tab[data-detail-tab="remix"]').style.display = 'block';
  } else {
    document.querySelector('.tab[data-detail-tab="remix"]').style.display = 'none';
  }
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
  
  // Brand & Product selection
  document.getElementById('brand-select').addEventListener('change', e => {
    apiOps.loadProducts(e.target.value);
  });
  
  document.getElementById('product-select').addEventListener('change', e => {
    const p = STATE.products.find(x => x.id === e.target.value);
    if (p) apiOps.loadProductDetails(p);
  });
  
  // Modals
  document.getElementById('btn-new-brand').addEventListener('click', () => ui.showModal('modal-brand'));
  document.getElementById('btn-new-product').addEventListener('click', () => {
    if (!document.getElementById('brand-select').value) return ui.toast('请先选择品牌', 'error');
    ui.showModal('modal-product');
  });
  
  document.getElementById('submit-new-brand').addEventListener('click', () => {
    apiOps.createBrand(document.getElementById('new-brand-name').value);
  });
  
  document.getElementById('submit-new-product').addEventListener('click', () => {
    const b = document.getElementById('brand-select').value;
    apiOps.createProduct(b, document.getElementById('new-product-name').value);
  });
  
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
  
  // Source Tabs
  document.querySelectorAll('.tabs [data-tab]').forEach(tab => {
    tab.addEventListener('click', e => {
      ui.switchTab('.tabs', e.target.dataset.tab, 'source-');
    });
  });
  
  // Detail Tabs
  document.querySelectorAll('.tabs [data-detail-tab]').forEach(tab => {
    tab.addEventListener('click', e => {
      ui.switchTab('.tabs', e.target.dataset.detailTab, 'detail-');
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
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      apiOps.uploadFile(e.dataTransfer.files[0]);
    }
  });
  
  fileInput.addEventListener('change', e => {
    if (e.target.files && e.target.files[0]) {
      apiOps.uploadFile(e.target.files[0]);
    }
  });
  
  // ASR
  document.getElementById('asr-engine').addEventListener('change', e => {
    document.getElementById('transcript-path-wrap').classList.toggle('hidden', e.target.value !== 'transcript');
  });
  
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
  bindEvents();
  if (STATE.token) {
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
