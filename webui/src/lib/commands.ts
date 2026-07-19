const SLASH_COMMANDS = new Set([
  "/session",
  "/memory",
  "/compact",
  "/exit",
  "/cron",
  "/pet",
  "/workspace",
  "/rollback",
  "/approve",
  "/reject",
  "/approvals",
  "/reflect",
  "/skill",
  "/help",
  "/auto",
  "/unlimited",
  "/stop",
]);

/** Return true when the first whitespace-delimited token is a UI command. */
export function isSlashCommand(input: string): boolean {
  const command = input.trim().split(/\s+/, 1)[0]?.toLowerCase();
  return !!command && SLASH_COMMANDS.has(command);
}
