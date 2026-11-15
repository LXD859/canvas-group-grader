# canvas自动化成绩导入

把助教的逐题扣分表转换为 Canvas 小组作业成绩和评语。在本机浏览器中选择课程和作业，导入 Excel 或 CSV，核对小组成员、扣分与已有成绩，勾选已批改小组后提交。每位助教使用自己的 Canvas Token，独立部署。

项目默认连接上海交通大学 Canvas，也可填写其他 Canvas HTTPS 站点。工具与学校及 Instructure 无隶属关系。当前实现已通过模拟 Canvas 的自动测试；真实学校账号、权限和小组评分传播行为需要按文末流程验收。

## 启动

本项目现位于 `grade-uploader/`，与旁边的 `ai-grader/` 独立运行。请先进入本目录。根目录旧启动脚本仍转到这里；整理前已经运行的旧上传器需要先关闭。若存在待迁移历史，Windows 启动脚本会先检查 8000 端口已释放，再同步保留的旧数据库。手动启动前可运行 `python scripts/migrate_history.py`。迁移会保留原文件和整理时的快照，检测到新数据库已变更时会停止自动覆盖。

Windows 用户安装 Python 3.11 或更高版本，然后双击 `start.bat`。首次启动会下载依赖。终端显示启动成功后，在浏览器打开 <http://127.0.0.1:8000>。关闭服务可在终端按 Ctrl+C。端口被占用时，先关闭之前的工具实例。

macOS、Linux 或希望手动启动的用户，在项目目录执行：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Windows PowerShell 可使用 `.venv\Scripts\python.exe` 执行上述 Python 命令。服务只支持单进程，请保持 `--workers 1`，以维持内存中的会话和预览一致性。

也可使用 Docker Compose：

```sh
docker compose up --build -d
```

打开同一个本机地址。Compose 仅将端口绑定至 `127.0.0.1`，历史保存在 Docker 卷中。运行 `docker compose down` 停止服务并保留历史。此版本定位为个人本机工具，不应直接暴露到局域网或公网；共享部署需要独立开发用户认证和会话隔离方案。

## 连接和使用

在 Canvas「账户 → 设置」中获取个人访问令牌，再在工具页面输入。Token 仅留在当前服务内存中，页面连接成功后会清空输入框；重启服务、断开连接或闲置 8 小时后需要重新连接。如果学校没有提供令牌入口，请联系学校 Canvas 管理员。工具不要求提供学校登录密码。

账号需要读取课程、作业、小组集、组员和提交详情的权限，并拥有目标课程的 `manage_grades` 权限。使用受限 Token 时，还需允许对应 API 路径和更新提交接口。工具会在预览时检查权限和作业设置。

先选择课程和具体作业，再上传 `.xlsx` 或 `.csv`。Excel 优先打开 `Assignment Grading` 工作表，可切换工作表。第一行应为列标题，默认首列是小组名、其余有标题的列是题目；页面可修改列映射。CSV 使用逗号分隔，支持 UTF-8、UTF-8 BOM 和 GB18030。单元格中含逗号或换行时，保存为标准带引号的 CSV。

仓库内的 `app/static/example.csv` 是合成示例，网页也提供下载。以下示例的作业满分若为 10 分，第一组得 8 分，第二组得 10 分：

```csv
Group,E1,E2,E3
Assignment Group 1,"-1.5, Missing justification.",,"-0.5, Incorrect notation."
Assignment Group 2,,,
```

工具按名称精确匹配作业关联小组集中的学生小组，匹配时去除首尾空白。当前仅支持已发布、按分数或百分比评分、组员共享成绩的小组作业。每位成员的提交需要已关联该小组，成员或提交信息读取不完整时会阻止该行。个人评分、匿名评分、审核评分及已发生 Canvas 自动迟交扣分的记录需要先在 Canvas 处理。

题目单元格可填写不扣分的文字建议，或扣分及说明，例如 `-1.5, Missing justification.`、`-1 for ii`。同格多笔明确扣分累加，例如 `-1.5 for i; -0.5 for ii; -0.5 for iii` 共扣 2.5 分。支持整数、小数、中英文逗号、分号、换行和常见负号，采用十进制精确计算。扣分条目从单元格开头或上述分隔符之后开始，数字后须接逗号及说明，或 `for` 及说明。正文中的 `x = -2` 不参与计分；边界处含义不明确的负数要求人工修正。单独数字（如 `2`）、以正数开头的内容、公式、缺少说明、无效扣分及累计扣分超过满分会标记到具体单元格并阻止该行提交。完全空行忽略，同名重复行不合并。

预览时所有小组默认不勾选。请只选已完成批改的小组；选中的全空小组会获得作业满分且不发送评语。有文字建议但未扣分的小组会提交满分和评论。非空评语按表格列顺序分段，以列标题加中文冒号单独一行开头，下一行保留单元格原文和内部换行，题目之间留一个空行。原文已有的题号也保留，不附加总分或自动改写原因。错误行仍显示原始评论，便于回到表格修正。

兼容补充：支持 `-2: i) (-1) ...; ii) (-1) ...` 的冒号总扣分、`-1 for 4` 的数字题号和 `-0.5 (1), 原因`。括号内分项保留为说明，坐标或集合末尾的负数不会作为独立扣分。支持明确的 `-2 each for missing v) and vi)`，按两题各扣 2 分处理。其他无法明确计数的 each 写法仍需修正。只有 `not submitted` 而没有金额时阻止提交；同时含 Total/总扣分等标记与多笔负数时要求人工确认。评语仍发送原文。导入时只选择 E1、E2 等题目列，不选择 summary、score；Rubric 说明行不要选中提交。

例如，最终评语为：

```text
E1：
Please add a more detailed citation.

E2：
-1.5 for i; -0.5 for ii, The final steps are missing.
```

已有记录需要变更时，逐组勾选「确认更新」。完全相同的分数和新格式评论会跳过；历史旧格式评论继续保留，改用新格式时会在确认更新后追加一条评论。提交后学生的可见性遵循 Canvas 现有发布政策，自动发布的作业可能立即显示成绩和评语。

## 中断、重试与历史

预览工具栏提供「一键全选」「取消全选」和「确认更新已选小组」。全选只选择有效行，错误行会跳过；更新已有成绩的确认仍是独立操作。请先确认整份表格已经批改完成，再使用全选，避免将尚未批改的空白行记为满分。

回读优先只请求当前小组成员的提交，批量列表每页读取 100 条。写入直接调用更新接口，省去写入前重复读取课程、作业及提交的请求。若学校版本的单人提交接口不返回小组信息，会在当前连接中自动改用列表接口。组员、作业变化检查和全组回读验证继续保留，具体耗时取决于 Canvas 响应与小组规模。

提交使用服务端保存的预览。开始前检查全部选中小组，逐组写入前再次核对作业、小组成员、分数和评语。预览后有变化时要求重新预览。Canvas 未提供跨请求事务锁，因此仍建议同一作业由助教协调操作，避免同时修改。

每组只向一名代表成员发送成绩更新请求，再回读全部成员确认 Canvas 已传播相同分数。评语使用 `group_comment=true` 发送一次，再确认全部成员可见同一评论。未确认传播时暂停批次，保留已完成步骤；工具不会自动对每位成员补发评语。

成绩更新和评论发送分别记入 SQLite。若网络超时，先回读结果；分数已相同，或者当前助教的相同评论已对全组可见，就跳过重复操作。变更后的评论追加，保留历史。相同文本若仅属于其他助教的评论，当前工具仍会作为本次助教反馈提交。

遇到「待核验」，先恢复网络并重新导入同一文件、生成预览。预览中的「核验分数」或「核验评语」会再次读取 Canvas。只有你在 Canvas 人工检查全部组员并确认操作没有生效时，才应解除重试保护。正常成功的重试不会重复评论。存在失败或未决结果时，批次停止，结果中尚未列出的小组保持未提交。

本地历史默认位于 `data/history.sqlite3`，包含导入文件名、工作表、行数，以及提交的小组名、得分、评语和结果，不含 Token 和原始上传文件。可用环境变量 `CANVAS_DATA_DIR` 调整目录。网页「清除本地记录」删除当前账号的记录内容及上传缓存；未决操作仅保留不可逆摘要、阶段和状态，避免清除记录后误发重复评论。它不会删除 Canvas 中的数据。电脑使用者应对本地数据目录设置适当访问权限。

## 开发和测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
```

`app/parsing.py` 负责输入和十进制计算，`app/canvas_gateway.py` 封装 CanvasAPI，`app/grading.py` 处理预览与提交，`app/store.py` 保存操作记录。FastAPI 提供本机 API，静态中文界面无需 Node.js 构建步骤。上传上限为 10 MB、5000 行、200 列，Excel 解压数据上限为 50 MB。

自动测试使用合成数据及模拟 HTTP，覆盖 Excel 导出范围错误、CSV 编码、扣分精度、空白、重复组名、权限、限流、组员变更、成绩与评论部分成功、超时回读、重复提交、重启恢复和本机访问限制。测试不会连接学校或写入真实成绩。项目 CI 在 Windows 和 Linux 的 Python 3.11、3.12 上运行同一套测试。

### 本机 API

所有 `/api/` 请求需要 `X-Canvas-Local: 1`，且只接受同源请求；连接后使用 HttpOnly、SameSite=Strict 会话 Cookie。无需在请求中重复发送 Token。API 错误不会回显原始参数。

| 接口 | 用途 |
| --- | --- |
| `POST /api/connect` | 使用 `base_url`、`token` 连接 |
| `GET /api/session`、`POST /api/disconnect` | 恢复界面、断开连接 |
| `GET /api/courses/{id}/assignments` | 读取课程作业 |
| `POST /api/upload` | multipart 文件字段 `file` 和可选 `encoding` |
| `POST /api/sheet` | 选择 `sheet`、`encoding` |
| `POST /api/preview` | 提交课程/作业 ID、从 0 开始的 `group_column` 和 `question_columns` |
| `POST /api/previews/{id}/submit` | 提交源文件行号 `rows` 和 `confirmed_updates`，返回 `job_id` |
| `GET /api/jobs/{id}`、`GET /api/history` | 查询执行进度和历史 |
| `POST /api/previews/{id}/resolve` | 核验指定 `row` 和 `phase`；人工确认未写入时另传 `confirmed_absent_in_canvas` |
| `DELETE /api/history` | 清理本地记录 |

上传文件留在当前会话内存中。预览保存完整来源行、小组、逐题扣分、计算分数、英文评语和 Canvas 快照，提交接口不接受客户端自定义分数覆盖预览。新上传或新预览会使此前预览失效。断开会话或服务重启后需要重新导入。

## 真实课程验收

1. 在个人账号中创建 Token，连接后确认课程、作业、小组及成员读取正确。接口权限不足时先解决权限问题。
2. 使用专门的测试作业和知情测试成员，设置小组提交及共享评分。核对满分和发布政策，上传只含一个测试小组的文件。
3. 提交后到 Canvas 检查所有组员成绩相同、评语出现一次。重复导入相同内容，确认没有新增评论。再测试一次改分和追加评语。
4. 测试全对小组只写成绩，以及网络恢复后核验流程。以上通过后再用于正式批次。

## 开源与隐私

本项目使用 MIT 许可证。Canvas 接入依赖 [ucfopen/canvasapi](https://github.com/ucfopen/canvasapi)，许可证保存在 `THIRD_PARTY_NOTICES.md`。接口参考[上海交通大学 Canvas Submissions API](https://oc.sjtu.edu.cn/doc/api/submissions.html)。

`.gitignore` 排除原始表格、数据库、凭据和日志，Docker 构建也排除这些文件。发布源码可运行 `python scripts/package_release.py`，它只打包明确列出的源码、测试、配置、说明和合成示例，输出 `dist/canvas-group-grader.zip`。请勿把真实学生表格作为 issue 附件或测试用例提交。
