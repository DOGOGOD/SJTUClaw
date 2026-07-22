import { cn } from "@/lib/utils";
import { usePetSelection } from "@/contexts/PetSelectionContext";

interface BrandAvatarProps {
  className?: string;
  fullCharacter?: boolean;
}

export function BrandAvatar({ className, fullCharacter = false }: BrandAvatarProps) {
  const { selectedPet } = usePetSelection();

  return (
    <span
      className={cn(
        "relative inline-flex shrink-0 overflow-hidden rounded-[30%] bg-transparent select-none",
        className
      )}
    >
      <span
        className="absolute inset-y-0 left-1/2 overflow-hidden -translate-x-1/2"
        style={{ width: fullCharacter ? "92.3077%" : "100%" }}
      >
        <img
          src={selectedPet.spritesheetUrl}
          alt={`${selectedPet.displayName} 宠物`}
          data-pet-id={selectedPet.id}
          draggable={false}
          className="pointer-events-none absolute left-0 top-0 h-auto w-[800%] max-w-none select-none"
        />
      </span>
    </span>
  );
}
