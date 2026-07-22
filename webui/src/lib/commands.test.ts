import { describe, expect, it } from "vitest";
import { isPetSelectionCommand, isSlashCommand } from "./commands";

describe("desktop pet slash command", () => {
  it("registers /pet and its subcommands", () => {
    expect(isSlashCommand("/pet")).toBe(true);
    expect(isSlashCommand("/pet open")).toBe(true);
  });

  it("recognizes only pet selection commands as artwork changes", () => {
    expect(isPetSelectionCommand("/pet select xiaohuang_webp")).toBe(true);
    expect(isPetSelectionCommand("  /PET SELECT xiaohuang_webp  ")).toBe(true);
    expect(isPetSelectionCommand("/pet open")).toBe(false);
    expect(isPetSelectionCommand("/pet selection xiaohuang_webp")).toBe(false);
  });
});
