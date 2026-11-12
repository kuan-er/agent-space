# Windows native bridge

ANet launches a native executable and supplies the Codex CLI argv contract. This small Go bridge starts the local Python adapter with an argv array and inherited stdin/stdout/stderr, without cmd.exe or handwritten quoting. It reads only `harness-launcher.json` beside itself (Python and adapter paths; no credentials).

Operator build: `python3 agent-portal/native-launcher/build.py <output-directory>`. Publish the Windows amd64/arm64 binaries beside server.py under native-launcher. Their SHA-256 values are pinned in the signed connector. Existing user-built .exe launchers without this metadata remain intact. Binaries have been cross compiled; the host smoke checks argv/stdin on macOS and does not claim Windows runtime coverage.
