# 功能与覆盖

当前共 22 项工具；20 项只读，2 项需要明确用户目标的提交操作。

| 模块 | 工具 | 说明 |
| --- | --- | --- |
| 通用 | `upc_capabilities`、`upc_session_status` | 实现能力、本地状态、设置入口；不请求学校 |
| 校园卡 | `upc_card_balance` | 本人余额与未结算金额；独立学号配置 |
| 教室 | `upc_free_classrooms` | 两校区、日期、1–12 节；全部范围先给按楼概览，1–2 栋可逐间对照学校记录 |
| 石光 | `upc_shiguang_activities`、`upc_shiguang_activity_detail` | 活动搜索和详情 |
| 石光 | `upc_shiguang_my_participations`、`upc_shiguang_credits`、`upc_shiguang_report_card` | 本人参与、学分学时、第二课堂成绩单 |
| 石光 | `upc_shiguang_register_activity` | 普通/社会实践/邀请码报名，唯一石光写工具 |
| 图书馆 | `upc_library_filters`、`upc_library_rooms`、`upc_library_seats`、`upc_library_times` | 日期、馆区、房间、座位和可约时段 |
| 图书馆 | `upc_library_reservations`、`upc_library_book` | 本人预约查询、单次预约提交 |
| 课表 | `upc_timetable_terms`、`upc_timetable_week` | 学期和指定教学周课表 |
| e站通 | `upc_service_search`、`upc_service_guide` | 匿名事项搜索与官方办事指南 |
| e站通 | `upc_service_my_todos`、`upc_service_my_applications` | 本人待办和申请状态/可用进度 |

## 自然语言怎么用

- “明天上什么课？”——客户端 Agent 先查当前学期与教学周，再按日期筛选。
- “下周有哪些课？”——自然周可能跨两个教学周，Agent 应分别读取再合并，不能假定学校从周一开始。
- “唐岛湾校区明天下午哪里有空教室？”——明确日期、校区和节次后查询；全部范围先看按楼概览，再选 1–2 栋逐间校验。
- “东廊和西廊今晚哪里空？”——用 `buildings` 指定这两栋（`mode=full`），工具会逐间核对 `detail`。
- “学生证补办需要什么材料？”——先搜索事项，再读取官方指南；缺失材料不编造。
- “我在石光参加了哪些活动？”——使用本人参与记录。
- “帮我预约这个座位。”——先核对具体日期、座位和起止时段，再在用户明确要求下提交。

## 已知边界

- 课表按教学周提供，学校网格从周日到周六；没有整学期工具和专用自然语言日期工具。
- e站通个人列表当前固定第 1 页、已验证的 `status=1`。没有提供的节点或进度保留 `null`，不把第三方入口视为已办理。
- 学分与学时区分返回；学校未给出的要求、剩余学分等保持未知。
- 图书馆真实可约时段正例、非空预约、真实预约提交，以及真实石光报名尚未作为发布验收执行。相关写契约有合成测试，不等于所有线上分支均已验证。
- CAS 正常密码登录与已有 SSO 恢复有真实证据；CAS 自然失效后再密码重登目前只有合成覆盖，不能承诺永久免交互。
- 部分系统暂无独立本人身份字段，状态如实返回 `identity_verified=false`；图书馆有独立本人校验。
- 空闲教室支持 `mode=auto|overview|full`：`building="全部"` 默认只返回按楼汇总（`overview`），不返回房间级空闲列表；选定 1–2 栋后走 `full`，对该范围所有房间逐间对照 `detail` 记录。
- 用户已明确 1–2 栋教学楼时，直接按该范围查询，不要重复追问；只有“全部教学楼”才先返回概览并请用户收窄。
- `available` 仅表示 `search-all` 与 `detail` 的学校记录在所选节次均未显示占用，不是现场空位、预约或准入证明；`availability_certainty`/`request_complete` 分别表示学校记录覆盖度和可用性确定程度。
- `overview` 的 `counts.candidates` 只是 `search-all` 的候选数，不是最终空闲数；必须再选楼对照。
- 房间级 `occupied` 只在记录确定时为 `true/false`；`occupancy_unknown=true` 时为 `null`，不能用 `false` 推断可用。`availability` 明确区分 `available/occupied/unknown/conflict/reserved`。
- `full` 中 `search-all` 标占用、`detail` 未显示占用的房间是证据冲突：保留 `occupancy_unknown=true`、`availability=conflict/reserved`，绝不升级为 `available`；另列 `conflict_candidates`。`include_conflicts` 已弃用，不再改变可用性。
- 名称含“研究生专用/教师专用/专用教室”等标记只是已知限制线索，不是完整准入识别；这些房间保持 uncertain。
- `detail` 校验失败、超时或未查询的房间标记 uncertain，不计入 `available`；全部计划请求都失败时返回 `availability_unverified`，不返回空闲结论。
- `search-all` 的多节次交集行为只来自已测样本，不能证明学校服务端的一般实现；工具仍以逐房间 `detail` 记录为准，并明确记录覆盖缺口。
- 第 13 节未验证：`search-all` 对 0/13/14 返回全部占用，`detail` 的 `c_13` 为空；本工具直接返回 `unsupported_period`，不猜成 1–12，也不把它当作可用。
- 逐间对照有硬预算：最多 300 个 detail 槽位、303 个计划请求/范围、4 并发、full 60 秒 detail 截止；超限或未查询数量在 `request_budget`/`detail_unqueried` 中明确返回。
- 余额的现有学校端点为 HTTP；不会通过代理或关闭 TLS 校验来扩展其他认证路线。

不提供校园卡流水、签到/补签、取消报名、图书馆取消/签到、自动抢座、审批或代提交 e站通申请。
