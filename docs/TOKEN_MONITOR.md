# Token Monitor

Agent Sandbox can report Claude Code, Codex and OpenCode usage to a separate Token Monitor Hub. The Docker integration is off by default. It runs on the Docker host and survives ordinary sandbox destruction and recreation. The Google Colab backend has no Token Monitor lifecycle hooks.

## Setup

Use a supported Agent Sandbox Linux Docker host with systemd, rootful Docker, Python 3.10 or later, and a running Token Monitor Hub. Initial provisioning builds a small headless image with upstream Token Monitor, Node.js and its pinned Tokscale dependency. The host needs access to the upstream Docker, Debian, GitHub and npm registries for that build. Running collectors need only the host's configured Hub route.

On the Docker host, from the Agent Sandbox installation directory:

1. Create a private secret file. The command below reads the Hub's shared secret without echoing it or putting it in your shell history.

   ```bash
   (umask 077; set -o noclobber; read -rs -p 'Hub secret: ' secret; printf '%s\n' "$secret" > .token-monitor.secret)
   ```

2. Create `.token-monitor.json` with mode `0600`. Start with this configuration for a Hub bound to the same host:

   ```json
   {
     "enabled": true,
     "hub_url": "http://127.0.0.1:17321",
     "secret_file": ".token-monitor.secret"
   }
   ```

   For a remote Hub, use an HTTPS endpoint such as `https://token-monitor.example.net`. A Hub behind a VPN works when the **host** can resolve and reach it. Keep a local HTTP Hub on loopback or use a trusted encrypted transport. The shared secret rides in the Authorization header. HTTPS certificates are verified against the host's trust store.

3. Run `./scripts/token-monitor.sh setup`. This validates Hub authentication, provisions the release, installs a systemd timer, and attempts a real sync when the workspace is running. When setting up a new Agent Sandbox, `./scripts/setup.sh` performs the same initialization if the configuration is present.

4. Use `./scripts/up.sh` and confirm the device appears in the Hub or connected widget. Run `./scripts/token-monitor.sh status` to see the device ID, recent synchronization state, recovery queue and connectivity. An idle installation without a workspace waits until there is source data to scan.

The Hub can run independently as the upstream Node Hub, in a connected desktop widget, or as a compatible Worker. Keep its own persistent storage and backups according to the upstream deployment guide. [Token Monitor headless agent](https://github.com/Javis603/token-monitor/blob/v0.99.0/docs/headless-agent.md) · [Upstream API reference](https://github.com/Javis603/token-monitor/blob/v0.99.0/docs/API.md)

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Enables collection and lifecycle hooks. |
| `hub_url` | Required | Absolute HTTP or HTTPS Hub URL. A reverse proxy base path is allowed. |
| `secret_file` | Required | Path to the file containing the shared secret. Use `.token-monitor.secret` or an absolute path outside the checkout. Set mode `0600`. |
| `interval_seconds` | `300` | Minimum interval between scheduled attempts, from 60 to 86,400 seconds. systemd checks approximately once a minute. |
| `collector_version` | `v0.99.0` | Upstream release tag. The default also verifies the exact upstream commit. |
| `device_prefix` | `agent-sandbox` | Used when generating the persistent device ID. Changing it afterward leaves the ID intact. |
| `device_id` | Generated | Set the ID before the first setup to adopt an existing collector identity with its upstream session archive. |
| `providers` | `["claude", "codex", "opencode"]` | Usage sources enabled for this installation. |
| `timezone` | `UTC` | IANA timezone for usage windows and historical calendar dates. Keep the previous collector's timezone when migrating. |

The config and default secret are excluded from Git and Docker build contexts. `destroy.sh` leaves them and `.token-monitor/` in place. Keep the configuration and state on the host. Use a separate shared secret file for each deployment if the Hub topology requires it.

## Operation and recovery

- `./scripts/token-monitor.sh sync` starts an immediate attempt, including any queued recovery snapshot. The periodic schedule is managed by systemd. A shared lock serializes scheduled, manual and final jobs. Hub failures back off exponentially up to one hour. A separate attempt can be started manually once connectivity returns.
- `./scripts/stop.sh` captures final usage and stops the workspace. Add `--down` to remove the containers while preserving normal sandbox credentials. `./scripts/up.sh --rebuild` and `./scripts/destroy.sh` also capture final usage before container removal. Update older systemd services to call `scripts/stop.sh` on shutdown.
- During controlled shutdown the host holds a descriptor to the workspace filesystem across Docker's graceful stop. It snapshots records after writers exit, then attempts ingestion. If the Hub fails, the snapshot stays in a private queue and is retried even after container removal. If the host cannot commit the final snapshot, the removal stops with a diagnostic. A workspace already stopped by raw Compose must be resumed with `scripts/up.sh` before the final capture can be verified.
- The final hook is bounded by a four-minute timeout and a 15-second forced termination allowance. `./scripts/destroy.sh --yes --force` opts out of final accounting and proceeds with destruction. `./scripts/stop.sh --down --force` does the same for a reset that preserves credentials. The queue retains up to four generations, each limited to 8 GiB, with a 2 GiB per-file limit. The upstream container uses one CPU, 2 GiB of memory and a 120-second job timeout. A large OpenCode WAL pair must fit in the collector's 1 GiB private work area.
- If `status` reports queued work, leave `.token-monitor/` intact and correct the reported failure. Use `sudo journalctl -u agent-sandbox-token-monitor.service -n 30` for scheduled errors. For an HTTP 401 or 403, verify the Hub secret. If no ingest was sent, check the collector version and storage limits. Failures starting Docker or reading a source may require checking the Docker service and workspace state. The Docker logs for collector containers are disabled.
- Run `./scripts/token-monitor.sh uninstall` to remove the managed timer and disable lifecycle hooks. It moves the configuration into `.token-monitor/disabled-config-*.json` and keeps the local archive, snapshots, secret, collector image and Hub history for rollback. Restore the saved config to `.token-monitor.json` with mode `0600` and rerun setup to re-enable it. Remove cached collector images separately with Docker if desired.

### Device identity, deletion and restore

The upstream Hub replaces a device's snapshot when it receives the same device ID. It combines different device IDs without cross-device deduplication. This integration uses one installation ID across restarts and empty recreations, with a persistent upstream usage archive. When a session disappears, the collector can replay its last retained observation. A native observation of the same session key wins over its archived copy, so restoring the same session source to the same installation does not add a second session.

Keep a host backup of `.token-monitor/` together with its configuration and the Hub's own data. Restore the usage sources to the same installation or migrate the device ID and archive together. Restore or transfer a single lineage and keep its original collector off. Concurrently publishing a copied workspace under another ID double counts its sessions. A stale or edited version of the same transcript may report a lower current observation because Token Monitor treats currently readable data as authoritative. Use the latest usage sources for an in-place restore and keep the newest host archive.

An unsolicited container removal, a host crash before the snapshot, or provider records deleted before any successful scan can lose unobserved usage. The Hub keeps ingested history when the workspace is removed. Hub storage durability is owned by the Hub deployment. A standalone Hub batches disk writes and flushes on graceful shutdown. Preserve its data volume across restarts.

## Migration from a host collector

1. Identify the old collector's Hub URL, device ID, timezone, clients and private usage archive directory. Keep the old setup, credential store and service definitions for rollback. Do this on the Docker host without copying provider credentials.
2. Stop the external timer and any in-flight collector before switching. Avoid running both collectors during migration. Take a filesystem backup of the old archive directory after its writer exits and back up the Hub storage.
3. Set `device_id` to the old ID and source the Hub secret from a protected file. Use the same timezone and providers. Before setup, prepare root-owned `.token-monitor/` with mode `0700` and a `.token-monitor/collector/archive/` subdirectory. Copy just the upstream `session-usage-archive.sqlite` (with any required SQLite sidecars from the stopped writer), legacy `session-usage-archive.json`, `daily-history-archive.json`, and `daily-history-calendars.json` files that exist. Set the archive directory and copied files to UID/GID `65532:65532`. Keep the rest of the old state in the backup. In particular, omit authorization journals, PID files, provider auth and scanner caches. The integration requires a session archive when first adopting an explicit ID or when the local status records a previous successful upload. This protects the Hub if the archive was omitted during migration or lost from a partial host backup.
4. Run setup and compare existing all-time counts, costs and device ID with the previous Hub view. Compare after the first real upload, including previously deleted sessions. If migrating from a collector that synced titles, follow the upstream title revocation instructions with the original collector and credentials.
5. To roll back, run the native uninstall command first. Then resume the previous timer with its untouched configuration and archive. If either archive has imported new usage since the cutover, save both states and the Hub backup before publishing again under the shared ID.

## Maintenance and performance

To upgrade, back up `.token-monitor/`, drain any pending recovery queue, set the desired `collector_version`, and rerun setup. Setup reuses the image when the version and build inputs match. Test new releases against an isolated Hub before switching production, since this integration relies on upstream archive formats, scanner paths and the authenticated full-ingest protocol.

Claude and Codex records are copied only from the supported project, session and archived-session directories, including Codex's Zstandard archives. A capture preserves file bytes and modification times, and rejects a generation if a record changes during its read. The upstream scanner decides how to interpret a stable JSONL tail, including a final valid record without a newline. OpenCode collects legacy messages and SQLite databases. DB and WAL identities are checked before and after copying. Only the collector processes SQLite, on a private copy of a captured WAL pair. A WAL checkpoint during capture causes a retry. Provider-specific custom paths are outside the supported scope. Pricing is the upstream scanner's estimate using the data available offline.

A local benchmark used 1,000 Codex-shaped JSONL files covering 303,600,000 bytes on a Linux host. It measured snapshot construction, including metadata traversal and fsync:

| Snapshot | Copied | Elapsed |
| --- | ---: | ---: |
| Cold | 303,600,000 bytes | 0.758 s |
| Unchanged | 0 bytes | 0.120 s |
| One appended record | 304,612 bytes | 0.122 s |

All passes inspect file metadata to detect replacement and deletion. The unchanged pass hard-links previously copied files inside private host state. Changed files are recopied in full, and a changed SQLite database may require copying its WAL pair and materializing it in the collector. The official agent does its own scan on each synchronization. The timings exclude that scan, Hub upload and Docker startup.

## Trust boundary

The sandbox bridge, firewall and Docker mounts remain the same. No integration port is opened to the workspace. A root-owned host job reads the verified local workspace's Linux mount namespace through a file descriptor. It selects only usage paths and rejects symlinks, hard links, different mounts and special files. Transcripts are staged in a private host directory, and only the selected stage is mounted read-only.

The official collector runs as a dedicated non-root UID in a read-only Docker container with no external network, capabilities, host Docker socket, or provider credentials. It receives only the stage, its own archive, and a private Unix socket. The host relay permits a bounded Token Monitor ingest request for the configured device, checks its acknowledgement, and attaches the Hub secret on the host side. It strips error response bodies and refuses redirects. Limits, ambient OpenCode quota collection, project breakdowns and title synchronization are disabled. Token Monitor controls the remaining summary contents, estimated costs, archive reconciliation and Hub retention.

Usage files are controlled by the providers and by any agent that can write them. A sandbox can falsify its own reported usage and exhaust its snapshot budget. Clock errors can skew period selection. Raw staged transcripts are sensitive and may contain prompts or tool output. Acknowledged raw data is kept only as a cache while the workspace is active. A pending stage stays private until delivery. Keep the host state and Hub restricted to the administrators and readers who should see that data.
