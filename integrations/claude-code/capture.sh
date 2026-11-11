#!/usr/bin/env sh
# Claude Code 把 Hook 事件 JSON（含 transcript_path、session_id、cwd）写到 stdin。
# `zymor hook capture` 只导入上次之后新增的对话，失败时退出码仍为 0，不会打断会话。
exec zymor hook capture
