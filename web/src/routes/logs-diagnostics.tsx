import { useState } from "preact/hooks";
import { api, request } from "../api/client";
import type { DiagnosticsBundle, DoctorReport, TraceList } from "../api/models";
import {
  Button,
  ConfirmSheet,
  CopyButton,
  LoadError,
  PageHeader,
  Section,
  SubNav,
  toastError,
} from "../components";
import { useApi } from "../lib/use-api";
import { formatBytes } from "../lib/format";
import { LOGS_TABS } from "./tabs";
import { bundleText, doctorText } from "./logs/diagnostics";

export default function Diagnostics() {
  const bundle = useApi((s) =>
    api.get<DiagnosticsBundle>("/diagnostics", undefined, s),
  );
  const doctor = useApi((s) => api.get<DoctorReport>("/doctor", undefined, s));
  const traces = useApi((s) => api.get<TraceList>("/traces", undefined, s));
  const [deleting, setDeleting] = useState<string | null>(null);
  const [output, setOutput] = useState("");
  const [replaying, setReplaying] = useState(false);
  async function replay(name: string) {
    setReplaying(true);
    setOutput("");
    try {
      const response = await request<Response>(
        `/api/admin/traces/${encodeURIComponent(name)}/replay`,
        { method: "POST", raw: true },
      );
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        setOutput((prev) => prev + decoder.decode(value, { stream: true }));
      }
    } catch (e) {
      toastError("Replay failed.", e);
    } finally {
      setReplaying(false);
    }
  }
  return (
    <>
      <SubNav items={LOGS_TABS} label="Logs" exact />
      <PageHeader title="Diagnostics." />
      <Section label="Diagnostic bundle">
        {!!bundle.error && (
          <LoadError
            thing="diagnostics"
            error={bundle.error}
            onRetry={bundle.reload}
          />
        )}
        <p>
          Versions, hardware, redacted settings, the last 500 engine log lines,
          and engine status.
        </p>
        {bundle.data && <CopyButton text={bundleText(bundle.data)} />}
        <Button onClick={bundle.reload}>Refresh</Button>
      </Section>
      <Section label="Doctor">
        {!!doctor.error && (
          <LoadError
            thing="doctor report"
            error={doctor.error}
            onRetry={doctor.reload}
          />
        )}
        <pre>{doctor.data && doctorText(doctor.data)}</pre>
        <Button onClick={doctor.reload}>Run again</Button>
      </Section>
      <Section label="Raw engine status">
        <details open>
          <summary>Schema 6 · /status</summary>
          <pre>{JSON.stringify(bundle.data?.status ?? {}, null, 2)}</pre>
          <CopyButton
            text={JSON.stringify(bundle.data?.status ?? {}, null, 2)}
          />
        </details>
      </Section>
      <Section label="Crash traces">
        <p>
          {traces.data?.enabled ? "Enabled" : "Disabled"} · Traces can contain
          private conversation data.
        </p>
        {!!traces.error && (
          <LoadError
            thing="crash traces"
            error={traces.error}
            onRetry={traces.reload}
          />
        )}
        <Button
          onClick={() =>
            void api
              .post("/system/reveal", { target: "traces" })
              .catch((e) => toastError("Could not reveal traces.", e))
          }
        >
          Reveal traces
        </Button>
        {traces.data?.traces.map((t) => (
          <div class="listrow" key={t.name}>
            <span>
              {t.name} · {formatBytes(t.size_bytes)}
            </span>
            <Button disabled={replaying} onClick={() => void replay(t.name)}>
              Replay
            </Button>
            <Button onClick={() => setDeleting(t.name)}>Delete</Button>
          </div>
        ))}
        {output && <pre role="log">{output}</pre>}
      </Section>
      <ConfirmSheet
        open={!!deleting}
        title="Delete crash trace."
        confirmLabel="Delete"
        onClose={() => setDeleting(null)}
        onConfirm={async () => {
          try {
            await api.del(`/traces/${encodeURIComponent(deleting!)}`);
            setDeleting(null);
            await traces.reload();
          } catch (e) {
            toastError("Could not delete trace.", e);
          }
        }}
      >
        <p>{deleting} will be permanently deleted.</p>
      </ConfirmSheet>
    </>
  );
}
