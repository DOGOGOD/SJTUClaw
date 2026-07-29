import { describe, expect, it } from "vitest";
import { isPetSelectionCommand, isSlashCommand } from "./commands";

describe("desktop pet slash command", () => {
  it("registers /pet and its subcommands", () => {
    expect(isSlashCommand("/pet")).toBe(true);
    expect(isSlashCommand("/pet open")).toBe(true);
  });

  it("routes /pi help and explicit backend switching through the command endpoint", () => {
    expect(isSlashCommand("/pi")).toBe(true);
    expect(isSlashCommand("/pi on")).toBe(true);
    expect(isSlashCommand("/pi status")).toBe(true);
    expect(isSlashCommand("/pi off")).toBe(true);
  });

  it("routes Claude Code backend commands through the command endpoint", () => {
    expect(isSlashCommand("/claude")).toBe(true);
    expect(isSlashCommand("/claude on")).toBe(true);
    expect(isSlashCommand("/claude status")).toBe(true);
    expect(isSlashCommand("/claude off")).toBe(true);
  });

  it("routes sandbox commands through the command endpoint", () => {
    expect(isSlashCommand("/sandbox")).toBe(true);
    expect(isSlashCommand("/sandbox on")).toBe(true);
    expect(isSlashCommand("/sandbox status")).toBe(true);
    expect(isSlashCommand("/sandbox off")).toBe(true);
  });

  it("recognizes only pet selection commands as artwork changes", () => {
    expect(isPetSelectionCommand("/pet select xiaohuang_webp")).toBe(true);
    expect(isPetSelectionCommand("  /PET SELECT xiaohuang_webp  ")).toBe(true);
    expect(isPetSelectionCommand("/pet open")).toBe(false);
    expect(isPetSelectionCommand("/pet selection xiaohuang_webp")).toBe(false);
  });
});
