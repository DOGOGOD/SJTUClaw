import { describe, expect, it } from "vitest";
import { isSlashCommand } from "./commands";

describe("desktop pet slash command", () => {
  it("registers /pet and its subcommands", () => {
    expect(isSlashCommand("/pet")).toBe(true);
    expect(isSlashCommand("/pet open")).toBe(true);
  });
});
