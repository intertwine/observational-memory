// Observational Memory — OpenCode plugin.
// Installed globally by `om install --opencode`.
import { spawn } from "node:child_process"

function send(event, directory) {
  const om = process.env.OM_BIN || "om"
  const child = spawn(om, ["opencode-event", "--cwd", directory || process.cwd()], {
    stdio: ["pipe", "ignore", "ignore"],
    detached: true,
  })
  child.stdin.end(JSON.stringify({ event }))
  child.unref()
}

export const ObservationalMemory = async ({ directory }) => {
  return {
    event: async ({ event }) => {
      if (!event || typeof event.type !== "string") return
      if (event.type === "message.updated" || event.type === "message.part.updated") send(event, directory)
      if (event.type === "session.idle") send(event, directory)
    },
    "experimental.session.compacting": async (_input, output) => {
      output.context.push("Use `om context --for opencode --cwd \"$PWD\"` or `om recall --query \"...\"` for Observational Memory when more project history is needed.")
    },
  }
}
