import {
  AssistantRuntimeProvider,
  AuiConfig,
  type ChatModelAdapter,
  Suggestions,
  useLocalRuntime,
} from "@assistant-ui/react";
import { useEffect } from "react";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { TooltipProvider } from "@/components/ui/tooltip";

// Where the chat gets its answers. For now a stub: the agent doesn't exist
// yet. When it does, only this adapter changes: it will call our API, stream
// the agent's steps back as tool-call parts, and pause on the remediation
// until the user approves it.
const notConnected: ChatModelAdapter = {
  async run() {
    return {
      content: [
        {
          type: "text",
          text: "The agent isn't connected yet. Once it is, it will investigate this, then report the root cause, the evidence behind it and the next action.",
        },
      ],
    };
  },
};

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

// The user-facing chat. It knows nothing about scenarios or answer keys;
// `examples` are just starter questions, and `question` prefills the box.
export function AgentChat({ question, examples }: { question: string; examples: string[] }) {
  const runtime = useLocalRuntime(notConnected);

  useEffect(() => {
    if (question) runtime.thread.composer.setText(question);
  }, [runtime, question]);

  return (
    <TooltipProvider>
      <AssistantRuntimeProvider runtime={runtime} config={AuiConfig({ suggestions: Suggestions(examples) })}>
        <div className="agent-chat">
          <Thread components={{ Welcome }} />
        </div>
      </AssistantRuntimeProvider>
    </TooltipProvider>
  );
}
