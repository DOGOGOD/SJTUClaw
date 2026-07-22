// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrandAvatar } from "@/components/BrandAvatar";
import { PetSprite } from "@/components/PetSprite";
import { PetSettingsSection } from "@/components/settings/PetSettingsSection";
import { PetSelectionProvider, usePetSelection } from "@/contexts/PetSelectionContext";

const api = vi.hoisted(() => ({
  fetchPetSettings: vi.fn(),
  fetchPets: vi.fn(),
  savePetSettings: vi.fn(),
  openPet: vi.fn(),
  closePet: vi.fn(),
  uploadPet: vi.fn(),
  deletePet: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

const yuexinmiao = {
  id: "yuexinmiao",
  displayName: "月薪喵",
  description: "默认宠物",
  spriteVersionNumber: 1 as const,
  spritesheetUrl: "/pet/pets/yuexinmiao/spritesheet",
  source: "bundled" as const,
  readOnly: true,
};

const xiaohuang = {
  id: "xiaohuang_webp",
  displayName: "线条小狗",
  description: "一只活力满满、陪你整理思路的黄色线条小狗。",
  spriteVersionNumber: 1 as const,
  spritesheetUrl: "/pet/pets/xiaohuang_webp/spritesheet",
  source: "bundled" as const,
  readOnly: true,
};

const baseSettings = {
  enabled: true,
  selectedPetId: yuexinmiao.id,
  launchOnGatewayStart: true,
  position: { x: null, y: null },
};

function SelectionHarness() {
  const { selectedPet, setSelectedPet } = usePetSelection();
  return (
    <button type="button" onClick={() => setSelectedPet(xiaohuang)}>
      {selectedPet.id}
    </button>
  );
}

beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  api.fetchPetSettings.mockResolvedValue({
    ok: true,
    settings: baseSettings,
    running: true,
  });
  api.fetchPets.mockResolvedValue({ ok: true, pets: [yuexinmiao, xiaohuang] });
  api.savePetSettings.mockResolvedValue({
    ok: true,
    settings: { ...baseSettings, selectedPetId: xiaohuang.id },
    running: true,
  });
  api.uploadPet.mockResolvedValue({
    ok: true,
    pet: xiaohuang,
    replyGeneration: { source: "llm", count: 12, warning: "" },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("selected pet artwork", () => {
  it("opens the add-pet dialog above the settings modal", async () => {
    const view = render(
      <PetSelectionProvider>
        <PetSettingsSection />
      </PetSelectionProvider>,
    );

    await waitFor(() => expect(view.getByRole("button", { name: "添加宠物" })).toBeTruthy());
    fireEvent.click(view.getByRole("button", { name: "添加宠物" }));

    const dialog = await view.findByRole("dialog", { name: "添加桌面宠物" });
    expect(dialog.className).toContain("z-[180]");
    const overlay = document.querySelector('[data-state="open"].fixed.inset-0');
    expect(overlay?.className).toContain("z-[180]");
  });

  it("uploads a ZIP pet package and refreshes the catalog", async () => {
    const view = render(
      <PetSelectionProvider>
        <PetSettingsSection />
      </PetSelectionProvider>,
    );
    await waitFor(() => expect(view.getByRole("button", { name: "添加宠物" })).toBeTruthy());
    fireEvent.click(view.getByRole("button", { name: "添加宠物" }));

    const packageFile = new File(["package"], "coding-cat.zip", { type: "application/zip" });
    fireEvent.change(view.getByLabelText(/^宠物压缩包/), { target: { files: [packageFile] } });
    fireEvent.click(view.getByRole("button", { name: "添加" }));

    await waitFor(() => expect(api.uploadPet).toHaveBeenCalledWith(packageFile));
    await waitFor(() => expect(api.fetchPets).toHaveBeenCalledTimes(3));
    await waitFor(() => {
      expect(view.getByText("新宠物已添加，并生成了 12 条专属互动台词")).toBeTruthy();
    });
  });

  it("shows package validation errors inside the open dialog", async () => {
    api.uploadPet.mockRejectedValueOnce(
      new Error(JSON.stringify({ detail: "宠物包内必须且只能包含一个 pet.json" })),
    );
    const view = render(
      <PetSelectionProvider>
        <PetSettingsSection />
      </PetSelectionProvider>,
    );
    await waitFor(() => expect(view.getByRole("button", { name: "添加宠物" })).toBeTruthy());
    fireEvent.click(view.getByRole("button", { name: "添加宠物" }));
    const packageFile = new File(["invalid"], "invalid.zip", { type: "application/zip" });
    fireEvent.change(view.getByLabelText(/^宠物压缩包/), { target: { files: [packageFile] } });
    fireEvent.click(view.getByRole("button", { name: "添加" }));

    expect((await view.findByRole("alert")).textContent).toContain("宠物包内必须且只能包含一个 pet.json");
    expect(view.getByRole("dialog", { name: "添加桌面宠物" })).toBeTruthy();
  });

  it("updates the home animation and every brand avatar after a settings switch", async () => {
    const view = render(
      <PetSelectionProvider>
        <BrandAvatar className="h-8 w-8" fullCharacter />
        <BrandAvatar className="h-9 w-9" fullCharacter />
        <PetSprite />
        <PetSettingsSection />
      </PetSelectionProvider>,
    );

    await waitFor(() => {
      expect(view.getAllByRole("img", { name: "月薪喵 宠物" })).toHaveLength(2);
    });

    fireEvent.click(view.getByRole("button", { name: /线条小狗/ }));

    await waitFor(() => {
      const avatars = view.getAllByRole("img", { name: "线条小狗 宠物" });
      expect(avatars).toHaveLength(2);
      expect(avatars.every((image) => image.getAttribute("src") === xiaohuang.spritesheetUrl)).toBe(true);

      const animation = view.getByRole("img", { name: "线条小狗 动画" });
      expect(animation.getAttribute("data-pet-id")).toBe(xiaohuang.id);
      expect(animation.firstElementChild?.getAttribute("style")).toContain(xiaohuang.spritesheetUrl);
    });

    expect(api.savePetSettings).toHaveBeenCalledWith({ selectedPetId: xiaohuang.id });
  });

  it("does not let an older refresh overwrite a newer local selection", async () => {
    let resolveSettings!: (value: {
      ok: boolean;
      settings: typeof baseSettings;
      running: boolean;
    }) => void;
    api.fetchPetSettings.mockReturnValue(new Promise((resolve) => {
      resolveSettings = resolve;
    }));

    const view = render(
      <PetSelectionProvider>
        <SelectionHarness />
      </PetSelectionProvider>,
    );

    await waitFor(() => expect(api.fetchPetSettings).toHaveBeenCalledTimes(1));
    fireEvent.click(view.getByRole("button"));
    expect(view.getByRole("button").textContent).toBe(xiaohuang.id);

    await act(async () => {
      resolveSettings({ ok: true, settings: baseSettings, running: true });
      await Promise.resolve();
    });
    expect(view.getByRole("button").textContent).toBe(xiaohuang.id);
  });
});
