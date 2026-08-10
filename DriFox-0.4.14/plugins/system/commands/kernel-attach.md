---
name: kernel-attach
description: UI attach RLM 常驻服务——连接 SSE 事件流回放，关闭窗口仅 detach 不杀服务（L4 后台保活）
usage: /kernel-attach [--port N] [<任务ID> | replay <任务ID> | detach]
tags: [kernel, serve, attach, rlm, l4]
---

# /kernel-attach — UI attach RLM 常驻服务（L4）

连接 --serve 常驻服务（默认端口 8765），查看/订阅 RLM 内核任务事件流。

## 用法

| 命令 | 说明 |
|---|---|
| `/kernel-attach` | 查看服务状态 + 最近任务列表 |
| `/kernel-attach <任务ID>` | attach 任务事件流（SSE 回放+实时，事件记入日志） |
| `/kernel-attach replay <任务ID>` | 回放任务历史事件（快照+事件列表） |
| `/kernel-attach detach` | 断开连接（服务端任务继续执行） |
| `/kernel-attach --port 9000` | 指定服务端口（默认 8765） |

## 行为契约

- **关闭窗口仅 detach**：窗口关闭时自动断开 SSE 连接，不杀服务进程/内核/任务。
- **服务独立**：python main.py --serve 启动常驻进程（无 UI），内核挂在服务进程内；
  任务在 UI 关闭后继续执行，重开 UI 后可重新 attach 回放。
- **重启恢复**：服务重启时自动重载内核快照（.dill）+ 未完成任务继续。
