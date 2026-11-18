# 笔记维护入口

运行 `python maintenance/submit_note.py --help` 查看参数；必须显式提供当前上下文、已有 UTF-8 笔记文件、笔记类型与真实截图证据。未绑定会话的上下文还需传入 `--session-id`。入口使用现有 CLI 的权限、回合状态和证据检查，不改写笔记文件，不启动外部解释器。

根目录原有四个 `submit_*notes.py` 仅保留兼容入口，接受相同参数；导入不会写文件或提交笔记。原脚本逐字保存在 `archive/*.py.txt`，供历史核对，其中的旧上下文与证据编号不能用于新任务。历史 topic 文件保留原处。

## 历史文件压缩与恢复

`python maintenance/history_cleanup.py` 仅预览。`--apply` 只处理 `review_artifacts/<批次>/`、`implementation_artifacts/<批次>/` 内名称明确的旧 `validation*`、`live_<哈希>`、`engine_fixture_<哈希>`、`chrome-smoke` 目录：先按内容 SHA-256 去重压缩，再完整回读验证每个对象、核对原文件未变化，最后删除已归档目录。重解析点、工作区外路径及现有归档会被拒绝。报告、`before` 源码备份与正式 `data` 不参与此清理。

2026-11-08 归档位于 [`history-snapshots.zip`](G:/qqbot/maintenance_reports/cleanup_20261108/history-snapshots.zip)，其中的 `manifest.json` 保存原文件路径、大小、修改时间和对象哈希。它使用内容对象存储；请通过恢复命令还原原来的文件路径。删除目录及节省空间的明细保存在同目录 `history-snapshots.result.json`。

恢复仅写入一个尚不存在的新目录，可用 `--target` 选择清单中的某个快照，重复传入可选择多个；不指定则恢复全部快照。原历史报告中指向快照内部的链接需要先恢复对应快照。

```powershell
python maintenance/history_cleanup.py --restore-to 'G:\qqbot-history-restored' --target 'review_artifacts/bilingual_commands_20261104/validation_final'
```

## 构建运行包

执行 `python maintenance/build_runtime.py --output runtime-new`。构建器拒绝覆盖已有运行包，只打包程序、公开模板和必需资源。构建日志位于 `maintenance_reports/cleanup_20261108`，包内 `build-manifest.json` 保存文件哈希。部署与外部依赖见[多模块运行说明](G:/qqbot/build_support/README.md)。

## 便携分发与解压验收

`package_runtime.py` 先核对构建清单和整个文件集合，只从指定运行包及静态模板生成 ZIP，拒绝夹带未列出的账号文件。原始 QQ 配置模板保留；便携入口首次生成的配置默认使用独立 `agent` 模式。所有 BAT 共用 `QQBOT_HOME`，留空时使用解压目录内的 `data`，不覆盖已有配置。

```powershell
python maintenance/package_runtime.py --runtime runtime/releases/actor-20261109-verified/QQBot --output dist/QQBot-agent-20261109-win-x64.zip --name QQBot-agent-20261109-win-x64 --report implementation_artifacts/portable_runtime_20261109
python maintenance/verify_portable_runtime.py --archive dist/QQBot-agent-20261109-win-x64.zip --report implementation_artifacts/portable_runtime_20261109/verification
```

验收器完整回读 ZIP、解压至含中文和空格的临时路径，以仅含系统工具的 PATH 检查四类入口、首次配置与保留、GUI 离屏、CLI/MCP 和原生 Playwright 的本地网站流程。测试目录独立于正在运行的机器人；浏览器冒烟使用 localhost，不使用真实网站账号。

3.1 及以上版本验收可增加 `--reply-flow`：通过解压后的 CLI/MCP 实际执行 `speech hold` 自动结束、无需 `speech ready` 的 `reply --finish`、`commit --finish`、重复提交与缓存消费。
