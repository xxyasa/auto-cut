# OpenSpec - 需求与提案管理

本目录用于管理 auto-cut 项目的需求提案（Proposal）。

## 目录结构

```
.openspec/
├── README.md          # 本文件
└── proposals/         # 提案目录
    └── NNNN-标题.md   # 各提案文件
```

## 提案流程

1. 在 `proposals/` 下创建新提案文件，格式为 `NNNN-简短标题.md`
2. 填写背景、目标、方案、验收标准
3. 讨论确认后标记状态为 `accepted`
4. 实现完成后标记为 `done`

## 提案模板

```markdown
# 标题

- **状态**: draft | accepted | in-progress | done | rejected
- **创建日期**: YYYY-MM-DD

## 背景

## 目标

## 方案

## 验收标准

## 备注
```
