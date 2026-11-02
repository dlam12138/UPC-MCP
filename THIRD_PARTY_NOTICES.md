# Third-party notices

## Protocol reference

The campus-card balance, free-classroom, Tangdaowan remaining-electricity and
library-seat protocol behavior was cross-checked against the MIT-licensed
UPCpocket Android project. Its copyright notice (Copyright © 2026 3beuml) is
retained in this project's MIT license.

Public website paths, public frontend configuration and request signatures are
protocol implementation details. They are not a user's password or login token;
authenticated tools still require the user's own locally configured account.

## Fonts

The local settings page bundles subsets of **Noto Sans SC** and **Noto Serif SC**,
distributed under the SIL Open Font License 1.1. Their complete license notices
are included alongside the fonts:

- `src/upc_mcp/settings_assets/notosanssc-OFL.txt`
- `src/upc_mcp/settings_assets/notoserifsc-OFL.txt`

Upstream: [Noto Sans SC](https://github.com/google/fonts/tree/main/ofl/notosanssc)
and [Noto Serif SC](https://github.com/google/fonts/tree/main/ofl/notoserifsc).

## Runtime dependencies

Dependencies such as MCP, HTTPX, Pydantic, cryptography, websockets and pywin32
remain under their respective upstream licenses. They are installed separately;
the release does not bundle a Python interpreter or third-party executables.
