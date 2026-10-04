import { useEffect, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import {
  Button,
  LoadError,
  PageHeader,
  Section,
  toast,
  toastError,
} from "../components";
import { modelIdFromParams } from "../lib/model-id";
import { useApi } from "../lib/use-api";
import type { SamplingOverlay } from "../api/models";
import { modelForm, schema } from "./settings/state";
import { SettingField } from "./settings/SettingField";
import { settingsApi } from "./settings/api";
import { SettingsSave } from "./settings";

export default function ModelSettingsPage({
  params = {},
}: {
  params?: Record<string, string | undefined>;
}) {
  const id = modelIdFromParams(params);
  const form = modelForm(id);
  const profiles = useApi((s) => settingsApi.profiles(id, s), [id]);
  const [text, setText] = useState("");
  useEffect(() => {
    void form.load();
  }, [id]);
  useEffect(() => {
    if (profiles.data)
      setText(
        JSON.stringify(
          {
            sampling_defaults: profiles.data.sampling_defaults,
            profiles: Object.fromEntries(
              profiles.data.profiles
                .filter((p) => p.name !== "default")
                .map((p) => [p.name, p.overlay]),
            ),
          },
          null,
          2,
        ),
      );
  }, [profiles.data]);
  async function saveProfiles() {
    try {
      await settingsApi.saveProfiles(
        id,
        JSON.parse(text) as {
          profiles: Record<string, SamplingOverlay | null>;
          sampling_defaults: SamplingOverlay;
        },
      );
      await form.load(true);
      await profiles.reload();
      toast("Profiles saved.");
    } catch (e) {
      toastError("Could not save profiles.", e);
    }
  }
  return (
    <>
      <PageHeader
        title="Model settings."
        meta={<span class="mono">{id}</span>}
      />
      <Section label="Overrides">
        <p>
          Unset values inherit global settings. Model overrides apply on the
          next load.
        </p>
        <Link href="/models">Back to models</Link>
        {!!form.loadError.value && (
          <LoadError thing="model settings" error={form.loadError.value} />
        )}
        <div class="stack">
          {schema.value?.fields
            .filter((f) => f.scope !== "G")
            .map((field) => (
              <SettingField
                key={field.key}
                field={field}
                form={form}
                model={id}
              />
            ))}
        </div>
      </Section>
      <Section label="Sampling & profiles">
        <p>
          Profiles change request defaults immediately. Caller-supplied values
          take precedence. Set a built-in profile to null to hide it.
        </p>
        {!!profiles.error && (
          <LoadError
            thing="profiles"
            error={profiles.error}
            onRetry={profiles.reload}
          />
        )}
        <label class="stack">
          Profiles JSON
          <textarea
            class="mono"
            rows={18}
            value={text}
            onInput={(e) => setText(e.currentTarget.value)}
          />
        </label>
        <Button disabled={!profiles.data} onClick={() => void saveProfiles()}>
          Save profiles
        </Button>
      </Section>
      <SettingsSave form={form} />
    </>
  );
}
