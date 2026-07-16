import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return time;

  const calendarDay = (value: Date) => Date.UTC(
    value.getFullYear(),
    value.getMonth(),
    value.getDate()
  );
  const daysAgo = Math.round(
    (calendarDay(now) - calendarDay(d)) / 86_400_000
  );
  if (daysAgo === 1) return "昨天";
  if (daysAgo > 1 && daysAgo < 7) return `${daysAgo}天前`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export function deriveTitle(text: string, fallback: string = "新对话"): string {
  if (!text) return fallback;
  const cleaned = text.replace(/^[\s\n\r]+/, "").replace(/[\s\n\r]+$/, "");
  const firstLine = cleaned.split("\n")[0] || cleaned;
  return firstLine.slice(0, 50) || fallback;
}

export function projectNameFromPath(path: string): string {
  if (!path) return "";
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}
