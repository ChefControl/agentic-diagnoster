import {
  AssistantRuntimeProvider,
  AuiConfig,
  type ChatModelAdapter,
  makeAssistantDataUI,
  Suggestions,
  type ThreadMessageLike,
  useLocalRuntime,
} from "@assistant-ui/react";
import { MessageSquarePlus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AnswerCard, Approval, Investigating, NoAnswer } from "./AnswerCard.tsx";
import { type StoredChat, newChatId, saveChat } from "./chats.ts";
import { api } from "./client.ts";

// The answer, and the fix decision when the agent waits for one. The decision
// updates the saved chat, so reopening it shows the outcome, not the buttons.
function ChatAnswer({ chat }: { chat: StoredChat & { run: NonNullable<StoredChat["run"]> } }) {
  const [run, setRun] = useState(chat.run);
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(approved: boolean) {
    setBusy(approved ? "approve" : "deny");
    setError(null);
    try {
      const next = await api.decide(run.run_id, approved);
      saveChat({ ...chat, run: next });
      setRun(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <AnswerCard run={run}>
      {run.status === "awaiting_approval" && <Approval run={run} busy={busy} error={error} onDecide={decide} />}
    </AnswerCard>
  );
}

// The chat's answer is a data part, not text: the card draws it from the
// agent's report. "investigating" shows while the agent works.
const AnswerUI = makeAssistantDataUI<StoredChat>({
  name: "answer",
  render: ({ data }) => (data.run ? <ChatAnswer chat={{ ...data, run: data.run }} /> : null),
});
const InvestigatingUI = makeAssistantDataUI({ name: "investigating", render: () => <Investigating /> });
const FailedUI = makeAssistantDataUI<{ error: string }>({ name: "failed", render: ({ data }) => <NoAnswer error={data.error} /> });

type Part = { type: "data"; name: string; data: unknown };
const answerPart = (chat: StoredChat): Part =>
  chat.run
    ? { type: "data", name: "answer", data: chat }
    : { type: "data", name: "failed", data: { error: chat.error ?? "the answer wasn't saved." } };

// One question per chat. The run isn't cancelled when you leave the page:
// the agent keeps going, and the answer still lands in the history.
function askAgent(chatId: string): ChatModelAdapter {
  return {
    async *run({ messages }) {
      const last = messages.at(-1);
      const question = (last?.content ?? [])
        .map((p) => (p.type === "text" ? p.text : ""))
        .join("\n")
        .trim();
      const askedAt = new Date().toISOString();
      yield { content: [{ type: "data", name: "investigating", data: {} }] };
      let chat: StoredChat;
      try {
        chat = { id: chatId, question, askedAt, run: await api.ask({ question, session_id: chatId }) };
      } catch (err) {
        chat = { id: chatId, question, askedAt, error: err instanceof Error ? err.message : String(err) };
      }
      saveChat(chat);
      // The address now names this chat, so reloading the page reopens it.
      window.history.replaceState(null, "", `#/chats/${chatId}`);
      yield { content: [answerPart(chat)] };
    },
  };
}

function Welcome() {
  return (
    <div className="agent-welcome">
      <h1 className="page-title">What's wrong?</h1>
      <p className="page-note">
        Describe the symptom. The agent reads the cluster, then reports the root cause, the evidence behind it and
        what to do next. It changes nothing without your approval.
      </p>
    </div>
  );
}

function NewQuestion() {
  return (
    <div className="after-question">
      <a className="btn btn-quiet" href="#/agent">
        <MessageSquarePlus size={16} strokeWidth={1.5} aria-hidden="true" />
        New question
      </a>
    </div>
  );
}

// The user-facing chat. It knows nothing about scenarios or answer keys;
// `examples` are just starter questions, and `question` prefills the box.
// With `chat`, it shows that earlier chat, read-only.
export function AgentChat({ question, examples, chat }: { question: string; examples: string[]; chat?: StoredChat }) {
  const [chatId] = useState(() => chat?.id ?? newChatId());
  const adapter = useMemo(() => askAgent(chatId), [chatId]);
  const runtime = useLocalRuntime(adapter);

  useEffect(() => {
    if (chat) {
      const messages: ThreadMessageLike[] = [
        { role: "user", content: [{ type: "text", text: chat.question }] },
        { role: "assistant", content: [answerPart(chat)] },
      ];
      runtime.thread.reset(messages);
    } else if (question) {
      runtime.thread.composer.setText(question);
    }
  }, [runtime, question, chat]);

  return (
    <TooltipProvider>
      <AssistantRuntimeProvider runtime={runtime} config={AuiConfig({ suggestions: Suggestions(examples) })}>
        <AnswerUI />
        <InvestigatingUI />
        <FailedUI />
        <div className="agent-chat">
          <Thread components={{ Welcome, AfterQuestion: NewQuestion }} />
        </div>
      </AssistantRuntimeProvider>
    </TooltipProvider>
  );
}
