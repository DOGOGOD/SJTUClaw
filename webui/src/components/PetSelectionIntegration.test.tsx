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
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("selected pet artwork", () => {
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
