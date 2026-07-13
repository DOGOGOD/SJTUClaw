import { cn } from "@/lib/utils";

interface BrandAvatarProps {
  className?: string;
  fullCharacter?: boolean;
}

export function BrandAvatar({ className, fullCharacter = false }: BrandAvatarProps) {
  return (
    <span
      className={cn(
        "relative inline-flex shrink-0 overflow-hidden rounded-[30%] bg-transparent select-none",
        className
      )}
    >
      <img
        src="/claw-cat-transparent.png"
        alt="Claw"
        draggable={false}
        className={cn(
          "absolute inset-0 h-full w-full object-contain object-center",
          fullCharacter ? "scale-[1.1]" : "scale-[1.55] translate-y-[7%]"
        )}
      />
    </span>
  );
}
