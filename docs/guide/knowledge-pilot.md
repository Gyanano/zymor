# 单用户知识与文章试点

试点用独立 SQLite 库记录对话原文、提案和核准版本。所有者通过本机命令审核，AI 通过本地只读 MCP 查询当前核准版本。用一条合成知识和一份 Markdown 验证从采集到纠错的完整过程。

## 从对话到知识视图

把一段测试对话保存为 UTF-8 文本 `dialogue.txt`，包含句子「示例蓝线在周三维护。」。所有命令指定同一个试点库。采集回执包含 `source_id`，提案回执包含 `proposal_id`，示例假定两个值均为 1。

```bash
zymor knowledge --db ./pilot.sqlite3 init
zymor knowledge --db ./pilot.sqlite3 capture ./dialogue.txt --key blue-line-demo
zymor knowledge --db ./pilot.sqlite3 propose blue-line --title "蓝线维护时间" --statement "示例蓝线在周三维护。" --source 1 --evidence "示例蓝线在周三维护。"
zymor knowledge --db ./pilot.sqlite3 review 1
zymor knowledge --db ./pilot.sqlite3 approve 1 --markdown-dir ./obsidian-test/Zymor
```

生成的笔记包含条目标识及核准版本。在 Obsidian 中打开测试库即可阅读。多个客户端可分别以 `zymor knowledge --db ABSOLUTE_PATH mcp` 启动本地只读 MCP，调用 `knowledge_get(item_id="blue-line")` 时读取同一个库。未核准的条目显示为未找到。

## 从文章编辑回到审核

知识视图可作为发布到网站的文稿。网站编辑可随文章维护排版、措辞和补充说明。涉及具体事实的调整，显式选中新的结论进入审核流程。保留文稿中的条目标识和基础版本号，用于发现另一端先完成的修订。

把生成的笔记复制到 `website-edit.md`，把正文里的周三改为周五，再提交待审提案。选段可和叙述性文字一起保存在编辑稿中，提案只使用所选结论。

```bash
zymor knowledge --db ./pilot.sqlite3 propose-article ./website-edit.md --statement "示例蓝线在周五维护。"
zymor knowledge --db ./pilot.sqlite3 review 2
zymor knowledge --db ./pilot.sqlite3 approve 2 --markdown-dir ./obsidian-test/Zymor
```

审核期间，两个客户端及已生成的笔记仍读取第 1 版。核准后再次查询会获得第 2 版，Obsidian 读取同路径更新的笔记。基于旧版本的另一份提案须重新比较。把网站或 Obsidian 的手工稿放在独立位置，可同时保留文章编辑和系统生成的视图。

## 使用范围

- 以独立试点库、手工选段、按 ID 查询和本地 Markdown 为当前验证范围。网站通过编辑副本模拟回传。
- 所有者运行核准命令。MCP 客户端仅获得当前核准条目的读工具。试点使用测试内容和专用笔记目录，沿用 v0.6 的用户可在各自 vault 中继续使用原命令。
- 版本提交与文件生成各自有结果。若写文件失败，已核准版本可继续查询。保留手工稿，再用 `zymor knowledge --db PATH export ITEM DIRECTORY` 重新生成。
- 站点接入时应选择可公开的条目。一个专题可关联多条知识版本，站点内容由编辑者维护。后续回传以逐条提交提案为入口。
