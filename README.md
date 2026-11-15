# Canvas 作业批阅与成绩上传

这个仓库包含两个可以单独运行的本机工具。`ai-grader/` 负责下载作业、准备 Agent 批阅任务、人工复核和合并表格，使用端口 8001。`grade-uploader/` 保留原有成绩上传器，使用端口 8000。两个项目分别保存依赖和数据，通过 Excel／CSV 交接。

双击 `ai-grader/start.bat` 启动批阅器，然后打开 <http://127.0.0.1:8001>。第一次可以点击「打开匿名演示」，无需 Canvas Token 或模型 API 密钥。详细操作见 [批阅器说明](ai-grader/README.md)。

双击 `grade-uploader/start.bat` 启动上传器。根目录原有 `start.bat` 和 `start.ps1` 仍会转到上传器。上传器用法见 [上传器说明](grade-uploader/README.md)。

学生名单和原始成绩表保留在仓库根目录。目录整理时，旧历史数据库被运行中的进程占用，因此原始 `data/history.sqlite3` 保留，并使用 SQLite 备份机制将一致快照保存到 `grade-uploader/data/history.sqlite3`。请关闭旧上传器窗口后从新目录重新启动；首次启动会检查并同步待迁移历史，防止遗漏整理期间产生的记录。

批阅器只读取 Canvas。部分批阅表带有未批状态，供多人分题协作；已保存范围内的小组和题目经人工确认后，即可导出对应扣分表；分题批阅时请先合并各题结果，再上传整份成绩。正式成绩仍由上传器单独预览和提交。
