# Zymor 品牌资源

## 命名规范

- `zymor-horizontal-<theme>.svg`：横向组合标（图标 + 字标），用于 README、文档页眉。
  - `light`：浅色背景（`#F4F3EE`），深色字标；适配亮色主题。
  - `dark`：深色背景（`#0B1020`），浅色字标；适配暗色主题。
- `zymor-icon-<theme>.svg`：方形图标标，用于头像、应用图标、社交预览。
- `favicon.svg`：透明背景的浏览器图标。

## README 用法

使用 `<picture>` 根据用户主题自动切换（README 默认使用）：

```html
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/zymor-horizontal-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/zymor-horizontal-light.svg">
    <img alt="Zymor Logo" src="assets/zymor-horizontal-light.svg" width="800">
  </picture>
</p>
```

如果目标本地预览器不渲染 `<picture>`，可以退回标准 Markdown 图片（代价是不会随主题切换）：

```md
![Zymor Logo](assets/zymor-horizontal-dark.svg)
```

## 备注

- 文字使用系统字体栈，避免把字体文件打进仓库。
- SVG 内保持纯矢量图形，不引用外部资源。
