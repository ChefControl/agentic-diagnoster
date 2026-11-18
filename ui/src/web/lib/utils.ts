import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

// Merge Tailwind class lists; used by the shadcn and assistant-ui components.
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
