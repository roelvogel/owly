/**
 * One-shot Cursor local agent: read a prompt on stdin, print a JSON envelope on stdout.
 * tools: [] so the model can only write text (Owly already collected X posts via xAI).
 */
import { Agent } from "@cursor/sdk";
import { stdin } from "node:process";

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    stdin.setEncoding("utf8");
    stdin.on("data", (chunk) => chunks.push(chunk));
    stdin.on("end", () => resolve(chunks.join("")));
    stdin.on("error", reject);
  });
}

function usageTokens(usage) {
  if (!usage) return { input_tokens: 0, output_tokens: 0 };
  return {
    input_tokens: usage.inputTokens ?? usage.input_tokens ?? 0,
    output_tokens: usage.outputTokens ?? usage.output_tokens ?? 0,
  };
}

const prompt = (await readStdin()).trim();
if (!prompt) {
  console.error("cursor_write: empty stdin prompt");
  process.exit(1);
}

const apiKey = process.env.CURSOR_API_KEY;
if (!apiKey) {
  console.error("cursor_write: CURSOR_API_KEY is not set");
  process.exit(1);
}

try {
  const result = await Agent.prompt(prompt, {
    apiKey,
    model: { id: process.env.CURSOR_MODEL || "composer-2.5" },
    name: "owly-writer",
    local: { cwd: process.env.OWLY_ROOT || process.cwd() },
    tools: [],
  });
  const tokens = usageTokens(result.usage);
  const envelope = {
    status: result.status,
    text: result.result ?? "",
    input_tokens: tokens.input_tokens,
    output_tokens: tokens.output_tokens,
  };
  process.stdout.write(JSON.stringify(envelope));
  if (result.status !== "finished") {
    process.exit(2);
  }
} catch (err) {
  const message = err instanceof Error ? err.message : String(err);
  console.error(`cursor_write: ${message}`);
  process.exit(1);
}
