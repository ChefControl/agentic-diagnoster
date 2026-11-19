// The last chats, kept in this browser only (localStorage). Each chat is one
// question and the agent's answer, or why there is none.
import { useEffect, useState } from "react";
import type { AgentRun } from "../shared/types.ts";

export interface StoredChat {
  id: string; // also the Phoenix session id of the chat's runs
  question: string;
  askedAt: string; // ISO time
  run?: AgentRun;
  error?: string;
}

export const MAX_CHATS = 20;
const KEY = "diagnoster.chats";
const CHANGED = "diagnoster-chats-changed";

// Newest first; a chat saved again replaces its older copy.
export function remember(chats: StoredChat[], chat: StoredChat): StoredChat[] {
  return [chat, ...chats.filter((c) => c.id !== chat.id)].slice(0, MAX_CHATS);
}

export function loadChats(): StoredChat[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(parsed) ? (parsed as StoredChat[]) : [];
  } catch {
    return [];
  }
}

export function saveChat(chat: StoredChat): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(remember(loadChats(), chat)));
  } catch {
    // Storage full or blocked: the chat still shows, it just isn't kept.
  }
  window.dispatchEvent(new Event(CHANGED));
}

export function useChats(): StoredChat[] {
  const [chats, setChats] = useState(loadChats);
  useEffect(() => {
    const reload = () => setChats(loadChats());
    window.addEventListener(CHANGED, reload);
    window.addEventListener("storage", reload); // another tab
    return () => {
      window.removeEventListener(CHANGED, reload);
      window.removeEventListener("storage", reload);
    };
  }, []);
  return chats;
}

// A Phoenix session id: letters, digits and dashes (the agent checks the shape).
export const newChatId = () => crypto.randomUUID();
