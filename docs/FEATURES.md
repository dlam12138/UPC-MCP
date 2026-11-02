# 功能与覆盖

> v1.3 新增唐岛湾剩余电量查询，并以逐间 detail 独立判断教室占用；search-all 只提供目录。

当前共 25 项工具；23 项只读，2 项需要明确用户目标的提交操作。

| 模块 | 工具 | 说明 |
| --- | --- | --- |
| 通用 | `upc_capabilities`、`upc_session_status` | 实现能力、本地状态、设置入口；不请求学校 |
| 校园卡 | `upc_card_balance` | 本人余额与未结算金额；自动使用本地统一账号的学号 |
| 唐岛湾电量 | `upc_power_buildings`、`upc_power_remaining` | 学校实时楼号目录；本次明确楼号 + 房间号的剩余电量（度/kWh），不保存目标 |
| 教室 | `upc_free_classrooms` | 两校区、日期、1–12 节；全部范围先给目录概览，1–2 栋逐间 `detail` 判定占用 |
| 石光 | `upc_shiguang_activities`、`upc_shiguang_activity_detail` | 活动搜索和详情 |
| 石光 | `upc_shiguang_my_participations`、`upc_shiguang_credits`、`upc_shiguang_report_card` | 本人参与、学分学时、第二课堂成绩单 |
| 石光 | `upc_shiguang_register_activity` | 普通/社会实践/邀请码报名，唯一石光写工具 |
| 图书馆 | `upc_library_filters`、`upc_library_rooms`、`upc_library_seats`、`upc_library_times` | 日期、馆区、房间、座位和可约时段 |
| 图书馆 | `upc_library_reservations`、`upc_library_book` | 本人预约查询、单次预约提交 |
| 课表 | `upc_timetable_terms`、`upc_timetable_week`、`upc_timetable_range` | 学期、指定教学周、最多 14 天自然日期范围；每个课次带真实日期/实例 ID |
| e站通 | `upc_service_search`、`upc_service_guide` | 已保存账号的完整可见目录检索与指南；无可用账号时显式退回公开目录/指南 |
| e站通 | `upc_service_my_todos`、`upc_service_my_applications` | 本人待处理队列与在办申请分页；逐页声明覆盖范围 |

## 自然语言怎么用

- “明天上什么课？”——客户端 Agent 先查当前学期与教学周，再按日期筛选。
- “下周有啥课？”——“下周”按自然日期范围（如周日至周六）传给 `upc_timetable_range`，最多 14 天；不要只查当前周几天再用 `weeks` 外推。
- “唐岛湾校区明天下午哪里有空教室？”——明确日期、校区和节次后查询；全部范围先看目录概览，再选 1–2 栋逐间读取 `detail`；占用只由 `detail` 判定。
- “东廊和西廊今晚哪里空？”——用 `buildings` 指定这两栋（`mode=full`），工具会逐间核对 `detail`。
- “学生证补办需要什么材料？”——先搜索事项，再读取官方指南；缺失材料不编造。
- “唐岛湾XX楼YY房间还有多少电？”——仅当**本次消息本身**同时明确楼号和房间号时，先取学校楼号目录，再用精确 id 查询；不从账号、旧聊天、历史或默认宿舍猜测。
- “帮我查一下宿舍电费。”——缺楼号或房间号，不调用电量查询；返回 missing/invalid input 并请用户补充，不把“宿舍”猜成任何房间。
- “本科生请销假在哪？”——已保存账号时检索该账号完整可见目录，命中后用同一账号读取指南；未配置账号时结果会标注只覆盖公开目录，不能据此断言学校没有该事项。
- “我在石光参加了哪些活动？”——使用本人参与记录。
- “帮我预约这个座位。”——先核对具体日期、座位和起止时段，再在用户明确要求下提交。

## 已知边界

- 课表按教学周提供，学校网格从周日到周六；`upc_timetable_range` 支持最多 14 天/3 个教学周的自然日期范围，没有整学期工具；每个课次给真实日期、ISO weekday、节次、时间、地点/教师和唯一 `instance_id`，必需周失败不伪装空课表。
- e站通目录以当前来源、当前身份可见范围为准：匿名公开目录（实测 196 项）不等于登录后本人目录（实测 386 项）。登录目录中的 folder 行会被排除，但 `folder_count`、`duplicate_id_count`、`searchable_total` 会如实返回；不能把匿名 0 结果当成全校不存在。
- e站通待办/在办申请按 `page`/`page_size` 返回，`total`、`has_more`、`complete_for_page`、`complete_for_scope` 分别表示总数、是否还有下一页、本页完整性和本次是否覆盖整个筛选范围；只读一页不会声称全部已查。待办不含未读消息；在办申请可能与“待本人处理”不同，且可能有等待他人的记录。
- e站通指南在已保存账号可用时复用同一会话读取登录后内容；无可用账号时退回公开指南并标注范围。只读官方详情，不调用 start-info、表单、附件或提交申请。
- 学分与学时区分返回；学校未给出的要求、剩余学分等保持未知。
- 图书馆真实可约时段正例、非空预约、真实预约提交，以及真实石光报名尚未作为发布验收执行。相关写契约有合成测试，不等于所有线上分支均已验证。
- CAS 正常密码登录与已有 SSO 恢复有真实证据；CAS 自然失效后再密码重登目前只有合成覆盖，不能承诺永久免交互。
- 部分系统暂无独立本人身份字段，状态如实返回 `identity_verified=false`；图书馆有独立本人校验。
- 唐岛湾剩余电量只接入学校公开 WebForms 表单。`upc_power_buildings` 只读取当前 `lou` 下拉的 `value`，项目不维护静态楼号映射；`upc_power_remaining` 要求 `building` 和 `room` **都必填**，且 `building` 必须是当前目录中的精确 id。
- 学校目录同时存在普通与“空调”表计（例如同一楼栋有普通项和“空调”项）。工具不判断用户要照明还是空调，也不把简称自动映射到任一 id；用户没有给出精确目录项时先返回 `invalid_building` 并列出当前目录，不能猜表计。
- 电量查询只在本次用户请求明确给出楼号和房间号时调用。缺任一项返回 `missing_input`，格式错误返回 `invalid_input`；不从账号、旧聊天、其他房间、默认宿舍、历史、缓存或桌面组件目标推断。单次只查一间，不枚举房间、不批量试号。
- 学校可能按房间字符串模糊匹配，候选表还可能同时带第一间房的电量。本工具只接受唯一精确的“楼号 + 房间号 + 系统编号”归属；候选、其他房间读数或归属不一致一律返回 `ambiguous`，不自动选第一项，也不把候选读数当成本房间结果。
- 数值字段是学校页面的“当前剩余电量”，单位度（kWh），不是人民币余额，也不是欠费金额。页面没有提供欠费、金额或电表更新时间；`arrears`、`money_balance`、`upstream_updated_at` 保持 `null` 并标注 `not_provided_by_source`。`fetched_at` 只是本次读取时间。`0 度`是有效读数，不等同于欠费。
- 学校端点当前为 HTTP，楼号/房间号在传输中不受 TLS 保护；工具不提供 HTTPS 降级或关闭证书校验的选项。固定主机/路径、连接 10 秒/读取 20 秒、解压后 1 MiB 上限、`trust_env=false`、不跟随重定向、不自动重试、不保活、不轮询。
- 认证跳转、网络错误、超时、HTTP 错误和页面结构变化分别返回 `auth_required`、`network_error`、`timeout`、`upstream_error`、`schema_changed`，绝不当作 0 度。若页面要求微信授权，不自动登录、不循环尝试参数。
- 楼号/房间号不写入设置、状态目录、日志、遥测、缓存、会话、回执或测试 fixture；工具没有默认宿舍、历史或“上次查询”。MCP 调用参数与宿主聊天仍可能由外部宿主记录，项目不能控制宿主留存。
- 电量仅支持唐岛湾；古镇口未接入。2026-11-02 按用户指定目标验证了两次真实读数、一次模糊匹配拒绝，并验证公开表单的 39 个楼号选项；真实楼房号和读数未加入发布物。其余候选及异常分支使用合成 HTML/HTTP 测试。
- 空闲教室支持 `mode=auto|overview|full`：`building="全部"` 默认只返回目录概览（`overview`），只给楼宇、教室总数和待逐间校验数，不返回房间级可用性，也不导出 `d.js` 候选空闲数/比例；选定 1–2 栋后走 `full`，对该范围每间教室读取 `detail`。
- 用户已明确 1–2 栋教学楼时，直接按该范围查询，不要重复追问；只有“全部教学楼”才先返回概览并请用户收窄。
- `search-all` 只提供 `d.all` 教室目录和 `d.listLh` 楼宇选项。`d.js` 不参与占用判定：它既不否决 `detail` 的空闲记录，也不在 `detail` 失败时回退成占用；缺失、畸形、切换都不阻断有效目录，只保留为忽略诊断。
- `available`/`occupied`/`unknown` 只表示所选节次与逐间 `detail` 记录中当天全部非空块 `lessArr` 的交集：有交集为 `occupied`，无交集为 `available`，`detail` 失败/缺日期/结构异常为 `unknown`。不是现场空位、预约或准入证明。
- `overview` 只返回目录楼宇/教室总数/`pending_detail`；`rooms=[]`，不调用 `detail`，不返回候选空闲数，`next_action=choose_buildings`。
- `only_available` 只返回普通自习推荐：`occupancy=available` 且无名称限制线索。名称含“研究生专用/教师专用/专用教室”等只作为 `access_restriction` 名称线索，不证明准入；这类房间即使 `detail` 空闲也保留 `occupancy=available`、`availability=available_restricted`，放在 `restricted_rooms`，不计入 `only_available`。
- `counts.available` 包含 detail 空闲的受限房间；`counts.ordinary_available` 是不受限的普通推荐数；`counts.restricted_available` 是受限空闲数；`counts.restricted` 是全部受限房间数。`counts.matched` 是应用 `only_available` 后参与分页的数量。
- `detail` 失败、超时、未查询、日期缺失或结构异常的房间 `occupancy=unknown`、`occupied=null`，不能当作空闲，也不回退成 `d.js` 占用；全部计划请求失败时返回 `availability_unverified`。`availability_certainty` 只表示 detail 覆盖，不把名称限制当作占用未验证。
- `include_conflicts`、`search_all_conflict`、`conflict_candidates` 仅为兼容保留：`include_conflicts` 不影响判定，`search_all_conflict` 恒为 `false`，`conflict_candidates` 恒为空。
- 第 13 节未验证：本工具直接返回 `unsupported_period`，不猜成 1–12，也不把它当作可用。
- 逐间校验有硬预算：最多 300 个 detail 槽位、303 个计划请求/范围、4 并发、full 60 秒 detail 截止；超限或未查询数量在 `request_budget`/`detail_unqueried` 中明确返回。`restricted_rooms` 单列且不分页。
- 校园卡余额与唐岛湾电量的现有学校端点为 HTTP；不会通过代理或关闭 TLS 校验来扩展其他认证路线。电量端点没有可用的 TLS 层，不能把“HTTP”误解为已验证 HTTPS。

不提供校园卡流水、电费充值/欠费、签到/补签、取消报名、图书馆取消/签到、自动抢座、审批或代提交 e站通申请。
