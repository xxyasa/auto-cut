# 自研轻量 Combobox 替换 business.html 原生 select

- **状态**: done
- **创建日期**: 2026-05-12
- **作者**: IP&AI Team

## 背景

`business.html` 当前的品牌 / 产品 / ASR 引擎三个表单控件使用原生 `<select>`。在 Windows 浏览器上点击下拉时弹层调用系统级窗口绘制，存在明显感知延迟（典型 100~300ms），用户主观体感"卡"。此外原生 `<select>` 无法搜索过滤，品牌/产品数量增长后会更不顺手。

`index.html`（审核台）不含原生 `<select>`，无需改动。

## 目标

1. 用一套轻量、零依赖、可复用的 Combobox 组件替换 `business.html` 中的三处原生 `<select>`。
2. 视觉与现有 `business.css` 设计变量保持一致（沿用 `--primary` / `--border` / `--surface`）。
3. 行为：点击触发器即时展开（无系统窗口开销）；可选地启用顶部搜索过滤；支持键盘 ↑↓ / Enter / Esc 导航；点选自动收起；失焦自动收起。
4. 不引入第三方 UI 库，避免无谓依赖与体积膨胀。
5. 不破坏现有业务行为：默认选中第一个品牌 / 产品的逻辑、品牌切换联动产品的逻辑、新建产品 modal 的交互全部保持。

## 非目标

- 不改 `index.html` / 审核台。
- 不重写 `business.html` 的整体表单结构。
- 不支持多选（当前业务不需要）。
- 不做无障碍 ARIA 全套（仅基本 keyboard 导航 + focus 反馈，符合自助场景需求即可）。

## 方案

### 1. 新增文件

```
src/autocut/static/
├── combobox.css      # ~60 行
└── combobox.js       # ~200 行，挂在 window.Combobox
```

### 2. Combobox 公共 API

```js
const combo = new Combobox(rootEl, {
  options: [{ value: 'id1', label: '品牌A' }, ...],
  placeholder: '-- 选择品牌 --',
  searchable: true,       // 三选项的 ASR 引擎可设 false
  disabled: false,
  onChange: (value, option) => {},
});

combo.setOptions(list);     // 重设选项
combo.setValue(value);      // 程序化选中（不触发 onChange）
combo.getValue();           // 当前 value
combo.setDisabled(bool);
combo.destroy();
```

### 3. DOM 结构（每个实例）

```html
<div class="combobox" data-state="closed">
  <button type="button" class="combobox-trigger">
    <span class="combobox-label">显示文本 / placeholder</span>
    <span class="combobox-arrow">▾</span>
  </button>
  <div class="combobox-panel" hidden>
    <input class="combobox-search" placeholder="搜索...">  <!-- searchable=true 时 -->
    <ul class="combobox-list">
      <li class="combobox-option" data-value="id1">品牌A</li>
      ...
    </ul>
    <div class="combobox-empty" hidden>无匹配</div>
  </div>
</div>
```

### 4. 行为规范

- 点击 trigger → 切换面板可见性，state 在 `data-state` 上反映（closed/open）
- 面板打开时自动 focus 到搜索框（若可搜索），并把当前选中项滚到可见区
- 键盘：↓/↑ 移动 `aria-active` 高亮项；Enter 选中并收起；Esc 收起；Tab 收起
- 失焦（focusout 跳出 combobox 子树）自动收起
- 搜索：大小写不敏感 substring 过滤 `label`；命中项高亮（可选）；无结果显示 empty 区
- 选项数 ≤ 200 时直接渲染列表，不做虚拟滚动（当前数据规模够用）

### 5. business.html 改造

把三处 `<select>` 替换为占位 `<div>`：

```html
<!-- 品牌 -->
<div class="combobox" id="brand-combobox"></div>
<!-- 产品 -->
<div class="combobox" id="product-combobox"></div>
<!-- ASR 引擎 -->
<div class="combobox" id="asr-engine-combobox"></div>
```

加入新静态资源引用：

```html
<link rel="stylesheet" href="/combobox.css">
<script src="/combobox.js" defer></script>
```

### 6. business.js 改造

- 在 `init()` 阶段实例化三个 combobox 并存到模块级变量（`brandCombo` / `productCombo` / `asrCombo`）
- `loadBrands` / `loadProducts` 改成调用 `combo.setOptions(...)` 而非操作 `<option>` DOM
- 所有 `document.getElementById('brand-select').value` 等替换为 `brandCombo.getValue()`
- 品牌切换的 `change` 监听器迁移为 `onChange` 回调
- ASR 引擎的 `change` 监听器同理

### 7. 后端 api.py 路由

参照现有 `/business.css` / `/business.js` 路由的写法，再加两条：

```python
@app.get("/combobox.css")
def combobox_css():
    return FileResponse(static_root / "combobox.css", media_type="text/css")

@app.get("/combobox.js")
def combobox_js():
    return FileResponse(static_root / "combobox.js", media_type="application/javascript")
```

不引入 `StaticFiles` 挂载（保持与项目现有风格一致）。

## 验收标准

### A. 功能正确性

- A1: business.html 加载后，品牌下拉自动选中第一个，并触发产品下拉填充与默认选中（与现有行为等价）
- A2: 点击品牌触发器 ≤ 50ms 内可见面板展开（DevTools Performance 测量 trigger 点击到 panel 可见的差值）
- A3: 品牌、产品下拉可在搜索框输入关键字即时过滤
- A4: ASR 引擎下拉的 `searchable=false`，无搜索框
- A5: 键盘 ↑↓ 可移动高亮项；Enter 选中并收起；Esc 收起；Tab 关闭
- A6: 选中后 trigger 显示选中项 label；`getValue()` 返回正确 value
- A7: 新建产品 modal 内的"卖点 / 联想词"添加与提交不受影响
- A8: 创建任务（POST /api/business/jobs）的 payload 中 brand_id / product_id / asr_engine 三字段值与下拉所选一致

### B. 兼容性 / 不破坏现有

- B1: index.html 审核台不受影响
- B2: 后端 API、CLI、jobs/worker 行为不变
- B3: `business.css` 现有类不被新组件污染（新组件 CSS 全部以 `.combobox` 为根选择器）

### C. 代码质量

- C1: `combobox.js` 不挂任何全局对象，除了一个 `window.Combobox = class Combobox {...}`
- C2: 无第三方依赖；不引入 npm/CDN
- C3: 单一文件 ≤ 250 行（含注释空行）

## 备注

- 后续若需要把品牌/产品下拉扩展为"远程搜索"（即输入时调用后端 /brands?q= 而非客户端过滤），只需在 `Combobox` 上再加一个 `loadOptions(query)` 异步钩子，不影响其他调用方。
- 若未来引入 Vue/React 重写整个前端，本组件可作为参考实现退役，不会形成历史包袱。
