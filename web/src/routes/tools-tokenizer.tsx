import { useState } from "preact/hooks";
import { request } from "../api/client";
import { Button, CodeBlock, LoadError, Section } from "../components";
import { engine } from "../store";
import { ToolPage } from "./tools";
import { reconstructPieces, type TokenPiece } from "./tools/tokens";
export default function Tokenizer() {
  const [text, setText] = useState("Hello, world.");
  const [special, setSpecial] = useState(false);
  const [tokens, setTokens] = useState<TokenPiece[]>([]);
  const [messages, setMessages] = useState(
    '[{"role":"user","content":"Hello"}]',
  );
  const [generation, setGeneration] = useState(true);
  const [rendered, setRendered] = useState("");
  const [count, setCount] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const tokenize = async (content: string, add_special: boolean) =>
    (
      await request<{ tokens: number[] }>("/tokenize", {
        body: { model: engine.value?.model, content, add_special },
      })
    ).tokens;
  async function run(kind: "tokens" | "template" | "count") {
    setBusy(true);
    setError(null);
    try {
      if (kind === "tokens") {
        const ids = await tokenize(text, special);
        setTokens(
          (await reconstructPieces(text, ids, special, tokenize)).tokens,
        );
      }
      if (kind === "template") {
        const response = await request<{ prompt: string }>("/apply-template", {
          body: {
            model: engine.value?.model,
            messages: JSON.parse(messages),
            add_generation_prompt: generation,
          },
        });
        setRendered(response.prompt);
      }
      if (kind === "count") {
        const response = await request<{ input_tokens: number }>(
          "/v1/messages/count_tokens",
          {
            body: {
              model: engine.value?.model,
              messages: JSON.parse(messages),
            },
          },
        );
        setCount(response.input_tokens);
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <ToolPage tool="tokenizer">
      {!!error && <LoadError thing="tokenizer request" error={error} />}
      <Section label="Tokenize">
        <label>
          Text
          <textarea
            rows={7}
            value={text}
            onInput={(e) => setText(e.currentTarget.value)}
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={special}
            onChange={(e) => setSpecial(e.currentTarget.checked)}
          />
          Add special tokens
        </label>
        <Button disabled={busy} onClick={() => void run("tokens")}>
          Tokenize
        </Button>
        <p>{tokens.length} tokens</p>
        <div class="token-list">
          {tokens.map((t, i) => (
            <span
              class="token-piece mono"
              key={i}
              data-special={t.special}
              title={`Token ${t.id}`}
            >
              {t.piece ?? "·"}
              <small>{t.id}</small>
            </span>
          ))}
        </div>
      </Section>
      <Section label="Template & Anthropic count">
        <label>
          Messages JSON
          <textarea
            rows={10}
            class="mono"
            value={messages}
            onInput={(e) => setMessages(e.currentTarget.value)}
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={generation}
            onChange={(e) => setGeneration(e.currentTarget.checked)}
          />
          Add generation prompt
        </label>
        <div class="cluster">
          <Button disabled={busy} onClick={() => void run("template")}>
            Apply template
          </Button>
          <Button disabled={busy} onClick={() => void run("count")}>
            Count Anthropic tokens
          </Button>
        </div>
        {count !== null && <p>{count} input tokens</p>}
        <CodeBlock code={rendered} />
      </Section>
    </ToolPage>
  );
}
