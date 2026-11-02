# Changelog

## Unreleased — 2026-11-02

This workspace-only change adds Tangdaowan remaining electricity and supersedes
the v1.2 classroom contract. Version stays `1.2`; no tag, release asset, push or
consumer upgrade is made here. The workspace now exposes 25 tools (23 read-only
and 2 write); the v1.2 release remains 23.

### Added

- `upc_power_buildings` reads only the current Tangdaowan school form's `lou`
  option values. It does not query rooms, accept a campus/account/URL/header or
  maintain a static building mapping. The live directory contains separate
  normal and “空调” meter options; the tools do not choose between them or map a
  short name to either one.
- `upc_power_remaining` requires both `building` and `room` in the tool schema.
  It queries exactly one explicitly named room and returns the school page's
  `当前剩余电量` in kWh (`度`), not a money balance and not an arrears amount.
  Missing/blank input is `missing_input`; invalid values are `invalid_input`;
  fuzzy or multiple candidates are `ambiguous` without a reading.
- Precision/ownership guard: a reading is returned only for a unique exact
  building + room + school system-number match. A candidate table's first-room
  reading or a grid reading for another room is never adopted.
- The service is stateless: no state directory, credential store, default room,
  history, cache, receipt, logging, telemetry or room enumeration. Only an
  in-memory concurrency gate exists.
- The transport uses fixed host/path, one GET plus one POST per query, 10s
  connect/20s read timeouts, a 1 MiB decoded-response limit, `trust_env=False`,
  no redirect following, no retry, no keep-alive pool and no arbitrary
  URL/header/cookie input. The observed school endpoint is HTTP, so there is no
  TLS layer for it; auth redirects/network/schema failures never become 0 kWh.
- Live boundary evidence: the public form GET was re-checked on 2026-11-02 and
  returned 39 building options; one synthetic invalid-room POST observed the
  no-result page. No real room reading was queried. Protocol parsing and
  candidate/ownership paths are validated with synthetic HTML and HTTP fixtures
  only.

### Changed

- Free classrooms now derive occupancy only from the per-room `detail`
  endpoint. `search-all` supplies only the `d.all` room directory and
  `d.listLh` building options. `d.js` no longer vetoes a detail-free room and is
  never used as an occupied fallback when detail fails; missing, switched or
  malformed `d.js` is retained only as an ignored diagnostic.
- Every room in the selected scope is checked through `detail`, including rooms
  that `d.js` marked occupied. All non-empty blocks for the query date
  contribute their `lessArr`; a non-empty intersection with the selected
  periods is `occupied`, an empty intersection is `available`. Detail failure,
  missing date or malformed structure is `occupancy=unknown`; if every planned
  detail request fails the query returns `availability_unverified`.
- Per-room output separates period occupancy from access hints:
  `occupancy` (`available`/`occupied`/`unknown`), `availability`
  (`available`/`available_restricted`/`occupied`/`unknown`), and
  `access_restriction` (name marker, `basis=room_name`, `proves_access=false`).
  `reserved` is retained as a deprecated name-only alias;
  `ordinary_self_study` is false for restricted rooms even when detail shows no
  occupancy.
- `only_available` now means ordinary self-study recommendation: detail-free
  and unrestricted. Restricted but detail-free rooms remain visible in
  `restricted_rooms` with `availability=available_restricted`, and are not
  returned by `only_available`.
- `counts` now distinguishes `available` (all detail-free, including
  restricted), `ordinary_available`, `restricted_available`, `occupied`,
  `unknown`, `restricted`, `matched` and `returned`.
- `overview` no longer exports `search_all_occupied`, `candidates`,
  `candidate_ratio` or `requires_narrowing`. It returns directory totals and
  `pending_detail` only, with `rooms=[]` and `next_action=choose_buildings`.
- `availability_certainty` now reflects detail coverage only. Access
  restriction does not make an otherwise fully checked room `partial`.

### Deprecated

- `include_conflicts` has no effect. `search_all_conflict` is always false and
  `conflict_candidates` is always empty; the old two-endpoint conflict model is
  removed from availability decisions.

### Fixed

- A room whose detail record shows no selected-period occupancy is no longer
  hidden because `d.js` marked it occupied. This covers the 2026-11-02 sampled
  ZY-B116 case (detail: periods 10–12 on the correct weekday key; selected
  afternoon free).
- A detail-occupied room is no longer cleared by a free `d.js` mark (sampled
  ZY-B218 and ZY-B233 cases).

### Migration from the v1.2 classroom contract

- Read `occupancy` for period occupancy instead of assuming `occupied` came
  from either endpoint. `occupied` remains a bool/null projection of
  `occupancy`.
- Read `access_restriction`/`ordinary_self_study` for recommendation and access
  hints. `reserved` is a deprecated alias.
- `only_available=true` now excludes `available_restricted` rooms; read
  `restricted_rooms` for their records.
- `overview.counts.candidates` and per-building candidate counts are gone.
  Use `pending_detail` and select 1–2 buildings for a full detail query.
- `conflict_candidates` is always empty; do not wait for conflict resolution
  between `search-all` and `detail`.

## 1.2 — 2026-11-01

> 空闲教室部分已被上方 Unreleased 取代；以下保留 v1.2 已发布历史记录。

本版汇总 Bug 1–4：修复空闲教室占用判断、余额学号绑定、课表日期与漏查风险、e站通登录目录检索。共 23 个工具（21 只读、2 写操作）。

升级后需重启 MCP 并刷新工具 schema；固定旧 wheel/hash/catalog 的宿主适配器需显式升级。不会覆盖 `v0.1.0` 资产，也不会自动更新宿主配置。升级步骤见 [配置指南](docs/CONFIGURATION.md#从-010-升级到-12)。

### Added

- Timetable `upc_timetable_week` output now carries `week_start`/`week_end`,
  per-instance `instance_id`, `weekday_label`, `teaching_day_index`,
  `time_range`, stable date/period ordering, and `counts.total/returned/
  complete`. `upc_timetable_range` adds a bounded natural-date range
  (maximum 14 days and three teaching weeks) with every course instance across
  the required weeks; this is one new read-only tool (release total 23).
- Free classrooms expose an explicit scope: `mode=auto|overview|full`,
  `buildings=[1–2 ids]`, and per-room `availability`. `overview` returns
  per-building totals and candidates from one `search-all`, without returning
  a room-level free list. `full` checks every room in the selected scope
  through the official `detail` endpoint; `building="全部"` is accepted as a
  deliberately heavy explicit request.
- `availability_certainty` (`records_verified`, `partial`, `unverified`),
  `request_complete`, `request_budget`, and `evidence` make record coverage
  and availability certainty explicit. `candidates`/`candidate_count` are not
  free rooms.
- `full` emits `conflict_candidates` for rooms where `search-all` says
  occupied but `detail` shows no occupancy. These rooms remain
  `occupancy_unknown=true` with `availability=conflict` (or `reserved`) and
  are never returned by `only_available`.
- `upc_capabilities` reports scope modes, supported periods, and the
  availability-certainty vocabulary.

### Changed

- `upc_free_classrooms` with no scope (`building="全部"`) now defaults to a
  per-building overview instead of a campus-wide room list. Callers that need
  room-level records should select 1–2 buildings or explicitly use
  `mode=full`.
- `verification.complete` is now a deprecated alias for
  `request_complete`. It describes request completion, not availability
  certainty; use `availability_certainty` and per-room `availability`.
- Per-room output adds `availability` (`available`, `occupied`, `unknown`,
  `conflict`, `reserved`); `occupied` is `null` when the room is uncertain, so
  a conflict cannot be read as `occupied=false`. `only_available` never
  returns it.
- `include_conflicts` is deprecated. It no longer upgrades conflicts to
  available; conflicts are only exposed through `conflict_candidates`.

### Fixed

- e站通 `upc_service_search` now reads the complete catalog visible to the
  saved account (386 rows in the verified snapshot) instead of an anonymous-only
  lookup (196 rows), then matches and paginates locally. The verified
  “本科生请销假” item now appears for the saved account. Without a usable
  account the tool returns the public catalog with
  `public_catalog_only=true`, `coverage.scope=public_catalog`, explicit
  warnings and `next_action`; an anonymous miss is no longer presented as
  proof that the service does not exist. Upstream 5xx/schema failures are
  surfaced instead of being replaced by a public empty result.
- e站通 `upc_service_guide` reuses the saved account session when available so
  login-only guides can be read, while public guides remain available without
  an account. It still calls only the official detail endpoint and never
  starts a form, attachment download or application submission.

- Campus-card balance now uses the locally saved unified account's student ID
  automatically. Rebinding follows the new account; clearing credentials blocks
  fallback to a stale standalone profile. In-flight results are discarded after
  credential changes. The anonymous balance endpoint receives no password or
  CAS session. Standalone configuration remains available for balance-only users
  who have never configured unified credentials.

- Timetable: the original release selected a course date with
  `weekdays[ISO weekday - 1]` even though the school array starts on Sunday.
  Monday courses could receive Sunday's date; the parser now matches each
  course's ISO weekday to the explicit weekdays date and refuses unknown
  conflicts. This is the source-level date fix previously worked around by the
  personal-assistant host adapter.
- Timetable: a partial date-by-date query could infer a later week from
  `weeks` and omit courses that start in that later week. The new range query
  reads every required week, returns all instances with real dates, and fails
  as a whole if any required week fails; it never returns a partial empty
  timetable as complete.

- Free classrooms: `search-all`'s `d.js` is not a reliable union. In tested
  samples, multiple periods behaved as an intersection and `d.js` omitted
  temporary "借用" records, so a room occupied in only some selected periods
  could be reported free. The tool now treats `d.js` only as a conservative
  pre-filter and checks the remaining candidates through the official
  per-room `detail` endpoint, using each block's `lessArr`. This is a tested
  sample conclusion, not a claim about the school's general server
  implementation. Detail failures/timeouts/rate limits mark the room unknown;
  if every planned detail request fails, the query returns
  `availability_unverified`. Fixes the 2026-11-01 卓越 B233 false-free report.
- Period 13 is now rejected with `unsupported_period` instead of returning a
  warning and potentially treating empty `c_13` as available. Mixed queries
  containing 13 are rejected as well; the tool does not guess a 1–12 rule.
- `fetch_details` now cancels and drains all child tasks in a `finally` block
  when the parent call is cancelled, so no orphan HTTP task survives into a
  later query.

### Known limits

- `available` means both school records show no occupancy for the selected
  periods. It is not proof of a live empty seat, reservation, or admission.
- The `d.js` intersection semantics are based on tested samples, not a
  documented server contract.
- Conflict and reserved rooms are uncertain, not available. The name-marker
  rule (`研究生专用`, `教师专用`, `专用教室`) is only a known-limit hint, not
  complete admission classification.
- Per-room checks are bounded to 4 concurrent requests, 60 seconds for full
  mode, 300 detail slots, and 303 total planned requests per scope. Partial
  results report `detail_unqueried` and `availability_certainty=partial`.
- The legacy candidate path still exists for internal compatibility; the
  public MCP tool uses the shared full/overview path.

### Migration from the published v0.1.0 behavior

- e站通 search now uses the saved account's full visible catalog by default;
  without a usable account, expect `public_catalog_only=true` and a
  `public_catalog` coverage scope. `catalog.complete` means only that the source
  catalog was fetched completely, not that every identity/school system was
  covered. Per-page completeness is now `complete_for_page`/
  `complete_for_scope`; the old ambiguous `complete` field is not returned by
  the search or personal-list payloads.
- e站通 todo/application results describe one page at a time. Read
  `total`, `has_more`, `complete_for_page` and `complete_for_scope` before
  claiming the whole pending queue or application history was read.

- If a caller relied on `building="全部"` returning rooms, it now receives
  `status="overview"`, `rooms=[]`, and `next_action="choose_buildings"`.
  Pass `buildings=[...]` or `mode="full"` for room-level records.
- If a caller read `verification.complete` as "availability is certain", use
  `availability_certainty` and per-room `availability` instead.
- If a caller passed `include_conflicts=true` expecting conflict rooms in
  `available`, they are no longer upgraded; read `conflict_candidates` and
  verify on site.
- If a caller used `occupied=false` to infer availability, also read
  `availability`; uncertain rooms now return `occupied=null`.
- Period 13 calls now fail with `unsupported_period` instead of returning a
  warning.

## 0.1.0 — 2026-11-01

First public release for Windows and Python 3.12+.

- 22 MCP tools: campus-card balance, free classrooms, Shiguang second classroom,
  library seats, campus-app timetable, e站通 and implementation/session status.
- Two explicit school write operations: Shiguang activity registration and
  library seat booking. They do not transparently retry a submitted request.
- Unified local CAS credentials and separate business sessions for four systems;
  persistent generation checks, recovery locks and failure backoff.
- A local browser settings page with encrypted credential configuration,
  connection checks and MCP configuration copying.
- Windows setup scripts and a password-free `mcp-config` command for user- or
  agent-assisted installation.
- Synthetic contract/concurrency tests, Windows CI, portable documentation and
  release-content auditing.

### Known limits

- Windows DPAPI storage is required for authenticated functions.
- CAPTCHA/second-factor challenges need normal official interaction.
- Timetable access is weekly; full-semester access is not exposed.
- e站通 personal lists currently cover page 1 with the verified `status=1` filter.
- Missing school progress/credit requirements remain unknown.
- Live registration and booking submissions have not been exercised as release
  tests. No real registration or booking is submitted by installation or CI.
- Campus-card transactions are retired. Check-in, cancellation and automatic
  seat grabbing are not offered.
