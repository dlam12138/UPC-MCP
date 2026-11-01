# Changelog

## Unreleased

These changes are workspace-only. `v0.1.0` remains the published release and
the consumer continues to pin that artifact; no tag, release asset, or
consumer version is changed here.

### Added

- Timetable `upc_timetable_week` output now carries `week_start`/`week_end`,
  per-instance `instance_id`, `weekday_label`, `teaching_day_index`,
  `time_range`, stable date/period ordering, and `counts.total/returned/
  complete`. `upc_timetable_range` adds a bounded natural-date range
  (maximum 14 days and three teaching weeks) with every course instance across
  the required weeks; this is one new read-only tool (workspace total 23).
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
