# 安装与配置

## 环境

- Windows 10/11；Python 3.12 或更新版本。
- 能访问学校相应服务的网络。
- 支持本地 stdio MCP 服务的 AI 客户端。

项目使用当前 Windows 用户的 DPAPI 加密凭据。当前版本没有 macOS/Linux 的认证存储实现，也不捆绑 Python。

## 方式一：下载源码包

1. 在本仓库 **Releases** 下载 `UPC-MCP-v1.2-source.zip`，解压到自己的目录，例如 `E:/CampusMCP`。
2. 双击 `E:/CampusMCP/安装.cmd`。脚本检查 Python、在项目内创建 `.venv` 并安装依赖；不会替换全局 Python。
3. 在自动打开的本地页面填写学校账号、密码和确认密码，点击“保存账号”。
4. 在“服务连接”中按需检查平台；打开页面和保存凭据不会自动请求学校。
5. 在页面下方复制 MCP 配置，添加到 AI 客户端的 MCP 设置中，重启或重新连接该 MCP 服务。

以后双击 `E:/CampusMCP/打开本地设置.cmd` 即可更新账号、移除凭据或重新复制配置。安装脚本运行时需要下载 Python 包；填写密码时不要把终端输出、聊天或 MCP 配置当成密码输入框。

## 方式二：命令行或 Agent 安装

把仓库克隆到自己选择的目录后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:/CampusMCP/scripts/setup.ps1
```

若只安装和生成配置、暂不打开浏览器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:/CampusMCP/scripts/setup.ps1 -NoBrowser
```

脚本的 ExecutionPolicy 只针对本次 PowerShell 进程，不修改持久执行策略。

需要把状态放到指定目录时，首次安装显式指定：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:/CampusMCP/scripts/setup.ps1 -StateDirectory E:/CampusMCP-data
```

选择会记在项目内被 Git 忽略的 `.local/run-config.json`，以后设置入口继续使用同一状态目录。不要把这个本机文件上传。

默认状态目录为 `%LOCALAPPDATA%/UPC-MCP`，也支持已有的 `UPC_MCP_STATE_DIR`。无论选哪个位置，设置页、CLI 和 MCP 启动参数必须保持一致。不要把密码存进环境变量。

## 方式三：安装 wheel

从 Releases 下载 `upc_mcp-1.2-py3-none-any.whl`。以下示例假定已创建 Python 虚拟环境 `E:/CampusMCP/.venv`：

```powershell
& E:/CampusMCP/.venv/Scripts/python.exe -m pip install E:/Downloads/upc_mcp-1.2-py3-none-any.whl
& E:/CampusMCP/.venv/Scripts/upc-mcp.exe settings
& E:/CampusMCP/.venv/Scripts/upc-mcp.exe mcp-config
```

wheel 已包含设置页面和字体，不需要另装 Node.js 或启动前端开发服务器。

## 接入 MCP 客户端

设置页里的配置来自本机实际安装目录；也可通过命令生成：

```powershell
& E:/CampusMCP/.venv/Scripts/upc-mcp.exe mcp-config
```

使用自定义状态目录时：

```powershell
& E:/CampusMCP/.venv/Scripts/upc-mcp.exe --state-dir E:/CampusMCP-data mcp-config
```

生成结果是标准 `mcpServers.upc` stdio 配置，包含绝对路径、启动参数和 `serve` 子命令，不含账号密码。把该条目合并进客户端已有配置，保留其他 MCP 服务；不要直接覆盖整个配置文件。

[示例配置](../examples/mcp-config.example.json) 的路径只是示例，不能直接套用到每台电脑。Lody、Claude Desktop、Cursor 等客户端的配置入口和顶层格式可能不同，以客户端实际要求为准；本项目没有修改 Harness 内部的账号密码表单。

## 首次配置账号

图形方式：运行 `settings`，只在本机页面填写。命令行方式：

```powershell
& E:/CampusMCP/.venv/Scripts/upc-mcp.exe credential configure
& E:/CampusMCP/.venv/Scripts/upc-mcp.exe credential status
```

CLI 输入隐藏。更换账号或密码使用 `credential configure --rebind`。已明确绑定另一个账号的业务会话会拒绝静默覆盖，需要先对相应系统执行 `shiguang clear`、`timetable clear`、`library clear` 或 `service-portal clear`，再查询。清除统一凭据用 `credential clear`。

校园卡余额优先使用统一账号中的学号（8–16 位数字），无需重复填写。
账号更换后立即跟随新学号；清除统一凭据后不会退回旧的余额学号。
密码不会发送到余额接口，查询不触发 CAS 登录。已有统一账号时，不允许单独配置其他余额学号。
仅从未配置统一账号、只使用余额的安装仍可单独配置：

```powershell
& E:/CampusMCP/.venv/Scripts/upc-mcp.exe card-balance configure
```

空闲教室无需账号；e站通未配置账号时只能读取公开目录/公开指南，结果会标注 `public_catalog_only`，不代表本人完整可见内容。设置页显示“已保存会话”只代表本地有记录，点击“检查连接”才会在线核验。设置窗口可以关闭，MCP 仍由 AI 客户端独立启动。

## 唐岛湾剩余电量

电量查询**不需要账号，也没有设置项、默认宿舍或历史记录**。每次自然语言查询必须由本次用户请求同时明确给出楼号和房间号；工具 schema 把两者都设为必填。缺任一项或只有“宿舍/我们寝室”这类模糊说法时，不调用工具，返回 `missing_input` 并请用户补充。

1. 先调用 `upc_power_buildings` 读取学校当前表单下发的楼号目录。返回的 `id` 是学校 `option value`，本项目没有静态楼号映射，不要自行构造或从旧资料复制。目录可能同时有普通表计和“空调”表计；工具不判断用户要哪一个，也不把简称自动映射到任一 id。
2. 调用 `upc_power_remaining` 时传 `building=<当前 id>`、`room=<本次用户明确给出的房间号>`。单次只查一间；不要枚举房间、批量试号或复用上次房间。
3. 学校可能模糊匹配房间；只有唯一精确的楼号 + 房间号 + 系统编号归属才返回读数。候选一律返回 `ambiguous`，由用户确认；本工具没有按系统编号二次选择的契约。

返回值是学校页面的“当前剩余电量”，单位度（kWh），不是人民币余额，也不是欠费金额。学校页面没有提供欠费、金额或电表更新时间；相关字段为 `null` 并标注 `not_provided_by_source`。`fetched_at` 只是本次读取时间。`0 度`是有效读数，不是欠费。

学校端点当前为 HTTP，楼号/房间号在传输中不受 TLS 保护；工具不提供关闭证书校验或任意 URL/header/cookie 的入口。固定主机/路径、连接 10 秒/读取 20 秒、解压后 1 MiB 上限、禁用环境代理、不跟随重定向、不自动重试/保活/轮询。认证跳转、网络、HTTP 或页面结构错误不会变成 0 度。楼号/房间号不写入状态目录、设置、日志、缓存、会话或回执；MCP 客户端和宿主聊天仍可能按自己的策略记录参数。仅支持唐岛湾，古镇口未接入；不提供充值/缴费。真实房间读数尚未作为发布验收。

## 从 0.1.0 升级到 1.2

1. 下载同一 `v1.2` Release 的源码 ZIP 或 wheel，以及 `SHA256SUMS`。用 `Get-FileHash` 查看下载包的 SHA256，与清单对应行比较后安装。
2. 停止客户端启动的旧 UPC-MCP 进程，保留原状态目录。不要清除凭据，也不要复制别人的会话。默认状态目录仍为 `%LOCALAPPDATA%/UPC-MCP`；自定义目录必须继续传入原来的绝对路径。
3. 源码安装推荐解压到新目录，在其中运行 `scripts/setup.ps1 -StateDirectory <原状态目录>`。wheel 安装可在原虚拟环境运行：

```powershell
& E:/CampusMCP/.venv/Scripts/python.exe -m pip install --upgrade E:/Downloads/upc_mcp-1.2-py3-none-any.whl
& E:/CampusMCP/.venv/Scripts/python.exe -c "from importlib.metadata import version; print(version('upc-mcp'))"
& E:/CampusMCP/.venv/Scripts/upc-mcp.exe --state-dir E:/CampusMCP-data mcp-config
```

以上路径为示例；`E:/CampusMCP-data` 只有在它确实是原状态目录时才使用。源码安装更换目录后，用新安装的 `mcp-config` 输出更新客户端命令路径。设置页与 MCP 使用同一个状态目录。

4. 重新连接 MCP、刷新工具 schema，确认包版本为 `1.2`。v1.2 发布物为 **23 项**工具；当前工作区 Unreleased 新增 2 项唐岛湾电量工具，共 **25 项**。自建 harness 若固定 wheel 哈希、工具清单或白名单，需一并更新，不能只修改版本字符串。
5. 先验证 `upc_capabilities` 和本地会话状态。正常升级不需要重新填写已保存账号；需要在线验证时只做具体只读查询，不测试真实报名或预约提交。

### Unreleased 电量与教室契约（尚未发布）

当前工作区新增唐岛湾剩余电量只读工具 `upc_power_buildings` / `upc_power_remaining`：楼号来自学校当前目录，楼号与房间号每次必须由本次请求同时给出；不保存、不缓存、不设默认宿舍，不查古镇口，不提供充值/欠费金额。工具返回度（kWh），不是金额余额。详见上方“唐岛湾剩余电量”。

空闲教室改为：`search-all` 只取目录/楼宇，占用完全由逐间 `detail` 判定；`d.js` 不参与。`overview` 只给目录数和待校验数，选定 1–2 栋后逐间读取；`unknown` 不回退占用，名称受限但详情空闲的房间在 `restricted_rooms`，`only_available` 只给普通自习推荐。该行为尚未进入 v1.2 发布物。

### 调用行为变化

- v1.2 时：空闲教室 `building="全部"` 默认返回按楼概览，不是房间列表；选定 1–2 栋后查询详情。第 13 节返回 `unsupported_period`。冲突/未验证房间的 `occupied=null`，`only_available` 不包含它们。（现已被上方 Unreleased 契约取代）
- 余额自动跟随设置页学号，更换账号会跟随新学号，清除统一凭据后不退回旧学号。
- 电量查询没有账户绑定或默认目标：每次必须显式给楼号 + 房间号；`ambiguous` 时不要自动选候选。
- `upc_timetable_range` 支持最多 14 天，按自然日期跨教学周查询。不要从某一周的课程周次外推下一周；课程实例可能同名但时间不同。
- e站通默认查已保存账号的可见目录；`public_catalog_only=true` 表示仅公开目录。待办和在办申请支持分页，必须检查 `has_more`，不能把单页当完整列表。

### 反馈问题

记录 Release `v1.2`、包版本、安装资产 SHA256、Windows/Python 版本、宿主与适配器版本、具体工具和脱敏参数、预期及实际错误码。若经过自建适配器，先用原版 MCP 直连复现；不要上传密码、Cookie、ticket、token、个人课表/申请正文或完整私密日志。

## 排查

| 现象 | 处理 |
| --- | --- |
| 找不到 Python | 安装 Python 3.12+，或安装脚本传入 `-PythonExecutable` 的绝对路径 |
| 客户端无法启动 MCP | 核对生成配置中的 executable 是否存在；保留 `serve` 参数；重新连接服务 |
| 网页打不开或提示启动授权失效 | 再次运行 `settings`，使用新打开的默认浏览器页面；闲置 30 分钟会关闭设置服务 |
| 配置后仍提示没账号 | 检查设置页与 MCP 的 `--state-dir` 是否相同 |
| 学校拒绝凭据 | 更新账号密码；程序不会连续试错 |
| 验证码/二次验证 | 按官方流程处理，不自动绕过；后续状态处理见 CLI `credential --help` |
| 申请进度或学分要求为空 | 学校没有提供对应字段，项目保留未知，不编造结果 |
| 电量返回 `missing_input`/`invalid_input` | 本次请求必须同时给出学校当前楼号 id 和房间号；不要从账号、旧聊天或默认宿舍补全 |
| 电量返回 `invalid_building` | 调用 `upc_power_buildings` 取当前目录，使用返回的 `id`；项目没有静态映射 |
| 电量返回 `ambiguous` | 学校给出多个/模糊匹配或读数归属不一致；请用户确认具体楼号、房间号和系统编号，不要自动选第一项 |
| 电量返回 `auth_required`/`schema_changed` | 学校页面要求微信授权或结构变化；不绕过、不猜字段、不当成 0 度 |
| 电量网络/TLS 提示 | 当前学校端点是 HTTP，没有 TLS 层；工具不会关闭证书校验或使用环境代理。检查网络后由用户重新发起 |
| 报名/预约结果未知 | 先核对本人参与/预约记录，不能直接要求 Agent 自动重发 |
