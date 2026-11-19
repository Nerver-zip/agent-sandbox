// Notifies the SSH client when an OpenCode turn finishes by calling agent-notify.
// agent-notify emits an OSC sequence through the tmux pane TTY (see scripts/agent-notify.sh).
// OpenCode reports a finished turn as `session.idle` (older releases) or as
// `session.status` with an idle status (newer ones); both are handled and de-duplicated.
export const AgentNotify = async ({ client, directory }) => {
  const lastNotified = new Map();

  const notify = async (sessionID) => {
    const now = Date.now();
    if (now - (lastNotified.get(sessionID) ?? 0) < 3000) return;
    lastNotified.set(sessionID, now);

    let message = "";
    try {
      const res = await client.session.get({ path: { id: sessionID } });
      message = res?.data?.title ?? res?.title ?? "";
    } catch (_) {
      // The title is optional; the notification still carries the project name.
    }

    try {
      const proc = Bun.spawn(["agent-notify", "opencode"], {
        stdin: new Blob([JSON.stringify({ cwd: directory, last_assistant_message: message })]),
        stdout: "ignore",
        stderr: "ignore",
      });
      await proc.exited;
    } catch (_) {
      // Notifications must never interrupt the session.
    }
  };

  return {
    event: async ({ event }) => {
      if (event?.type === "session.idle") {
        await notify(event.properties?.sessionID ?? "unknown");
      } else if (event?.type === "session.status" && event.properties?.status?.type === "idle") {
        await notify(event.properties?.sessionID ?? "unknown");
      }
    },
  };
};

export default AgentNotify;
