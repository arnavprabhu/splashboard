import { useState } from "preact/hooks";
import { request } from "../api/client";
import { Button, CodeBlock, LoadError, Section } from "../components";
import { engine } from "../store";
import { ToolPage } from "./tools";
import { SEMIF_EXAMPLE, SYSTEMONE_EXAMPLE } from "./tools/endpoints";
import { typesafeSnippet } from "./tools/snippets";
export default function Judgments() {
  const [mode, setMode] = useState<"systemone" | "judgments">("systemone");
  const [body, setBody] = useState(JSON.stringify(SYSTEMONE_EXAMPLE, null, 2));
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  let parsed: Record<string, unknown> = {};
  try {
    parsed = JSON.parse(body) as Record<string, unknown>;
  } catch {
    /* handled on send */
  }
  async function send() {
    setBusy(true);
    setError(null);
    try {
      setResult(
        await request(`/v1/${mode}`, {
          body: { ...JSON.parse(body), model: engine.value?.model },
        }),
      );
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <ToolPage tool="judgments">
      <Section label="Scoring request">
        <p class="meta">
          Local model scores, not calibrated confidence. Calibrate on held-out
          data before using thresholds.
        </p>
        {engine.value?.model?.toLowerCase().includes("clef") && (
          <p role="alert">
            Splash scores with the backbone output layer, not Clef’s joint
            schema head. Results will differ from Cloudflare’s Clef.
          </p>
        )}
        <label>
          Method
          <select
            value={mode}
            onChange={(e) => {
              const next = e.currentTarget.value as typeof mode;
              setMode(next);
              setBody(
                JSON.stringify(
                  next === "systemone" ? SYSTEMONE_EXAMPLE : SEMIF_EXAMPLE,
                  null,
                  2,
                ),
              );
            }}
          >
            <option value="systemone">System One</option>
            <option value="judgments">SemIf judgment</option>
          </select>
        </label>
        <p>
          {mode === "systemone"
            ? "Use noul for yes/no, choice for a criteria map, or score for ordered levels. Up to 64 questions."
            : "Supply state, question, and 2–16 options with id and description."}
        </p>
        <label>
          Request JSON
          <textarea
            rows={20}
            class="mono"
            value={body}
            onInput={(e) => setBody(e.currentTarget.value)}
          />
        </label>
        <Button variant="accent" disabled={busy} onClick={() => void send()}>
          Score
        </Button>
      </Section>
      <Section label="Result">
        {!!error && <LoadError thing="judgment" error={error} />}
        <p class="meta" title="1 − H/log K, not calibrated accuracy">
          Confidence measures how concentrated the answer distribution is.
        </p>
        <CodeBlock code={JSON.stringify(result, null, 2)} />
      </Section>
      {mode === "systemone" && (
        <Section label="TypeSafe SDK · Python">
          <CodeBlock
            code={typesafeSnippet({
              origin: location.origin,
              auth: true,
              body: { ...parsed, model: engine.value?.model },
            })}
          />
        </Section>
      )}
    </ToolPage>
  );
}
