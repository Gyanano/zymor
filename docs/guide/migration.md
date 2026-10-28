# 迁移与导出

Zymor 的 vault 是普通 Markdown；迁移工具用于备份、恢复和跨机器搬运。

## 导出

```bash
zymor export ./zymor-bundle
```

输出：

```text
zymor-bundle/
  manifest.json   # format/version/时间/节点数/sha256
  nodes.jsonl     # 每行一个 {id, markdown} 节点
```

- 导出包含所有层级的节点 Markdown，跳过 `_catalog.md` 等生成物；
- `manifest.json` 记录 `nodes.jsonl` 的 sha256；
- 导入前先校验全部节点、层-库约束、重复 id 与敏感信息，再开始写入；
- 不导出索引、审计、L0 原始事件、dream 状态。

## 导入

```bash
zymor import ./zymor-bundle
zymor import ./zymor-bundle --mode overwrite
```

冲突模式：

- `skip`（默认）：目标库已有相同 id 时跳过；
- `overwrite`：用 bundle 中的节点覆盖同 id 节点。

导入前会校验：

- `format` / `version`；
- `nodes_sha256` 校验和；
- 节点 frontmatter 与必需小节。

导入不会复制 L0、索引或审计；导入后按需运行：

```bash
zymor index rebuild
```

## MemoryAdapter 接口

第三方记忆系统只实现接口，不在本仓库内置适配器：

```python
from collections.abc import Iterable
from pathlib import Path

from zymor.adapters import AdapterNode, MemoryAdapter


class MyAdapter:
    name = "my-system"

    def read(self, source: Path) -> Iterable[AdapterNode]:
        ...
```

每个 `AdapterNode` 提供：

- `external_id`：外部系统 id，用于报错定位；
- `markdown`：Zymor 节点 Markdown 全文。

通过 `import_from_adapter(vault, adapter, source)` 导入。真实第三方适配器由使用方自行实现，本仓库不引入对应依赖。

## 边界

- 不实现 `.zym` 市场、交易、签名市场；
- 不承诺跨大版本 bundle 兼容；
- 不做云端同步；
- 不导出敏感审计与 L0 原始事件。
