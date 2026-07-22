import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { fetchPetSettings, fetchPets } from "@/lib/api";
import type { PetInfo } from "@/lib/types";

const FALLBACK_PET: PetInfo = {
  id: "yuexinmiao",
  displayName: "月薪喵",
  description: "SJTUClaw 默认宠物",
  spriteVersionNumber: 1,
  spritesheetUrl: "/pet-spritesheet.webp",
  source: "bundled",
  readOnly: true,
};

interface PetSelectionContextValue {
  selectedPet: PetInfo;
  setSelectedPet: (pet: PetInfo) => void;
  refreshSelectedPet: () => Promise<void>;
}

const PetSelectionContext = createContext<PetSelectionContextValue>({
  selectedPet: FALLBACK_PET,
  setSelectedPet: () => {},
  refreshSelectedPet: async () => {},
});

export function PetSelectionProvider({ children }: { children: ReactNode }) {
  const [selectedPet, setSelectedPetState] = useState<PetInfo>(FALLBACK_PET);
  const selectionVersion = useRef(0);

  const setSelectedPet = useCallback((pet: PetInfo) => {
    // A local selection is newer than every refresh already in flight.
    selectionVersion.current += 1;
    setSelectedPetState(pet);
  }, []);

  const refreshSelectedPet = useCallback(async () => {
    const refreshVersion = ++selectionVersion.current;
    const [settingsResult, petsResult] = await Promise.all([
      fetchPetSettings(),
      fetchPets(),
    ]);
    const selected = petsResult.pets.find(
      (pet) => pet.id === settingsResult.settings.selectedPetId,
    );
    if (refreshVersion === selectionVersion.current) {
      setSelectedPetState(selected ?? FALLBACK_PET);
    }
  }, []);

  useEffect(() => {
    void refreshSelectedPet().catch(() => {
      // Keep the bundled fallback while the gateway is unavailable.
    });
  }, [refreshSelectedPet]);

  const value = useMemo(() => ({
    selectedPet,
    setSelectedPet,
    refreshSelectedPet,
  }), [refreshSelectedPet, selectedPet, setSelectedPet]);

  return (
    <PetSelectionContext.Provider value={value}>
      {children}
    </PetSelectionContext.Provider>
  );
}

export function usePetSelection(): PetSelectionContextValue {
  return useContext(PetSelectionContext);
}
