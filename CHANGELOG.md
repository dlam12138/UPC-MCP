# Changelog

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
