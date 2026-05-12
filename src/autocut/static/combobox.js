/* src/autocut/static/combobox.js
   轻量 combobox 组件，零依赖。挂在 window.Combobox。
   API:
     const combo = new Combobox(rootEl, {
       options: [{value, label}, ...],
       placeholder: '-- 选择 --',
       searchable: true,
       disabled: false,
       onChange: (value, option) => {},
     });
     combo.setOptions(list);    // 重设选项
     combo.setValue(value);     // 程序化选中（不触发 onChange）
     combo.getValue();          // 当前 value
     combo.setDisabled(bool);
     combo.destroy();
*/

(function () {
  'use strict';

  function buildDom(root, opts) {
    root.classList.add('combobox');
    root.dataset.state = 'closed';
    const searchableAttr = opts.searchable !== false ? 'true' : 'false';
    root.innerHTML = `
      <button type="button" class="combobox-trigger">
        <span class="combobox-label placeholder"></span>
        <span class="combobox-arrow">▾</span>
      </button>
      <div class="combobox-panel" hidden>
        ${searchableAttr === 'true'
          ? '<input class="combobox-search" type="text" placeholder="搜索..." autocomplete="off">'
          : ''}
        <ul class="combobox-list" role="listbox"></ul>
        <div class="combobox-empty" hidden>无匹配</div>
      </div>
    `;
    return {
      trigger: root.querySelector('.combobox-trigger'),
      label: root.querySelector('.combobox-label'),
      panel: root.querySelector('.combobox-panel'),
      search: root.querySelector('.combobox-search'),
      list: root.querySelector('.combobox-list'),
      empty: root.querySelector('.combobox-empty'),
    };
  }

  class Combobox {
    constructor(root, opts) {
      if (!root) throw new Error('Combobox: root element required');
      this.root = root;
      this.opts = Object.assign(
        {
          options: [],
          placeholder: '-- 请选择 --',
          searchable: true,
          disabled: false,
          onChange: null,
        },
        opts || {},
      );

      this.value = null;
      this.options = [];
      this.filteredIndices = []; // 当前展示的 options 下标
      this.activeIndex = -1;     // 高亮项在 filteredIndices 中的位置
      this.isOpen = false;

      this.dom = buildDom(this.root, this.opts);
      this.setOptions(this.opts.options);
      this.setDisabled(this.opts.disabled);
      this._renderLabel();
      this._bindEvents();
    }

    // ---------- public API ----------

    setOptions(list) {
      this.options = Array.isArray(list)
        ? list.map(o => ({ value: String(o.value), label: String(o.label) }))
        : [];
      // 若当前值已不存在则清空
      if (this.value !== null && !this.options.some(o => o.value === this.value)) {
        this.value = null;
      }
      this._applyFilter('');
      this._renderLabel();
    }

    setValue(value) {
      const v = value === null || value === undefined ? null : String(value);
      if (v !== null && !this.options.some(o => o.value === v)) return;
      this.value = v;
      this._renderLabel();
      this._renderActiveSelected();
    }

    getValue() {
      return this.value;
    }

    setDisabled(disabled) {
      this.opts.disabled = !!disabled;
      if (this.opts.disabled) {
        this.dom.trigger.setAttribute('disabled', '');
        this.close();
      } else {
        this.dom.trigger.removeAttribute('disabled');
      }
    }

    open() {
      if (this.isOpen || this.opts.disabled) return;
      this.isOpen = true;
      this.root.dataset.state = 'open';
      this.dom.panel.hidden = false;
      // 重置搜索 + 过滤
      if (this.dom.search) {
        this.dom.search.value = '';
        this._applyFilter('');
        // 异步 focus，确保 panel 已绘制
        setTimeout(() => this.dom.search && this.dom.search.focus(), 0);
      }
      // active 默认指向当前选中项；否则第 0 个
      this.activeIndex = this._indexOfValueInFiltered(this.value);
      if (this.activeIndex < 0 && this.filteredIndices.length > 0) {
        this.activeIndex = 0;
      }
      this._renderActiveSelected();
      this._scrollActiveIntoView();
    }

    close() {
      if (!this.isOpen) return;
      this.isOpen = false;
      this.root.dataset.state = 'closed';
      this.dom.panel.hidden = true;
    }

    destroy() {
      this.root.replaceChildren();
      delete this.root.dataset.state;
      this.root.classList.remove('combobox');
    }

    // ---------- internal ----------

    _renderLabel() {
      const selected = this.options.find(o => o.value === this.value);
      if (selected) {
        this.dom.label.textContent = selected.label;
        this.dom.label.classList.remove('placeholder');
      } else {
        this.dom.label.textContent = this.opts.placeholder;
        this.dom.label.classList.add('placeholder');
      }
    }

    _applyFilter(keyword) {
      const kw = (keyword || '').trim().toLowerCase();
      const indices = [];
      this.options.forEach((o, i) => {
        if (!kw || o.label.toLowerCase().includes(kw)) indices.push(i);
      });
      this.filteredIndices = indices;
      this._renderList();
      this.dom.empty.hidden = indices.length > 0;
      // 过滤后重置 active 到第一项
      this.activeIndex = indices.length > 0 ? 0 : -1;
      this._renderActiveSelected();
    }

    _renderList() {
      const frag = document.createDocumentFragment();
      this.filteredIndices.forEach((optIdx, posIdx) => {
        const o = this.options[optIdx];
        const li = document.createElement('li');
        li.className = 'combobox-option';
        li.dataset.value = o.value;
        li.dataset.index = String(posIdx);
        li.textContent = o.label;
        frag.appendChild(li);
      });
      this.dom.list.replaceChildren(frag);
    }

    _renderActiveSelected() {
      const items = this.dom.list.querySelectorAll('.combobox-option');
      items.forEach((el, i) => {
        if (i === this.activeIndex) el.dataset.active = 'true';
        else delete el.dataset.active;
        if (el.dataset.value === this.value) el.dataset.selected = 'true';
        else delete el.dataset.selected;
      });
    }

    _indexOfValueInFiltered(value) {
      if (value === null) return -1;
      for (let i = 0; i < this.filteredIndices.length; i++) {
        if (this.options[this.filteredIndices[i]].value === value) return i;
      }
      return -1;
    }

    _scrollActiveIntoView() {
      const items = this.dom.list.querySelectorAll('.combobox-option');
      if (this.activeIndex < 0 || !items[this.activeIndex]) return;
      items[this.activeIndex].scrollIntoView({ block: 'nearest' });
    }

    _selectByActive() {
      if (this.activeIndex < 0) return;
      const optIdx = this.filteredIndices[this.activeIndex];
      const o = this.options[optIdx];
      if (!o) return;
      const changed = o.value !== this.value;
      this.value = o.value;
      this._renderLabel();
      this.close();
      if (changed && typeof this.opts.onChange === 'function') {
        this.opts.onChange(o.value, o);
      }
    }

    _bindEvents() {
      this.dom.trigger.addEventListener('click', e => {
        e.preventDefault();
        if (this.opts.disabled) return;
        this.isOpen ? this.close() : this.open();
      });

      this.dom.trigger.addEventListener('keydown', e => {
        if (this.opts.disabled) return;
        if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          if (!this.isOpen) this.open();
        } else if (e.key === 'Escape') {
          this.close();
        }
      });

      if (this.dom.search) {
        this.dom.search.addEventListener('input', e => {
          this._applyFilter(e.target.value);
        });
        this.dom.search.addEventListener('keydown', e => this._onListKeydown(e));
      }

      this.dom.list.addEventListener('mouseover', e => {
        const li = e.target.closest('.combobox-option');
        if (!li) return;
        this.activeIndex = Number(li.dataset.index);
        this._renderActiveSelected();
      });

      this.dom.list.addEventListener('click', e => {
        const li = e.target.closest('.combobox-option');
        if (!li) return;
        this.activeIndex = Number(li.dataset.index);
        this._selectByActive();
      });

      // 失焦自动收起：先标记是否有 mousedown 在面板内，避免点选项时误关
      this._panelMousedown = false;
      this.dom.panel.addEventListener('mousedown', () => {
        this._panelMousedown = true;
      });

      // 失焦自动收起：focusout 后等下一个 tick，若焦点不在组件内则关闭
      this.root.addEventListener('focusout', () => {
        setTimeout(() => {
          if (this._panelMousedown) {
            this._panelMousedown = false;
            return; // mousedown 在面板内，由 click 事件处理，不提前关
          }
          if (!this.root.contains(document.activeElement)) this.close();
        }, 0);
      });

      // 点击组件外区域也关闭
      this._docClickHandler = e => {
        if (!this.root.contains(e.target)) this.close();
      };
      document.addEventListener('mousedown', this._docClickHandler);
    }

    _onListKeydown(e) {
      const len = this.filteredIndices.length;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (len === 0) return;
        this.activeIndex = (this.activeIndex + 1) % len;
        this._renderActiveSelected();
        this._scrollActiveIntoView();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (len === 0) return;
        this.activeIndex = (this.activeIndex - 1 + len) % len;
        this._renderActiveSelected();
        this._scrollActiveIntoView();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        this._selectByActive();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        this.close();
        this.dom.trigger.focus();
      } else if (e.key === 'Tab') {
        this.close();
      }
    }
  }

  window.Combobox = Combobox;
})();
