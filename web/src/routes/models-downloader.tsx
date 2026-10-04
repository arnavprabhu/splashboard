import { useState } from "preact/hooks";
import type { InspectResult } from "../api/models";
import {
  Button,
  LoadError,
  PageHeader,
  Section,
  SubNav,
  toastError,
} from "../components";
import { useApi } from "../lib/use-api";
import {
  getCatalog,
  inspectModel,
  searchHub,
  postDownload,
} from "./models/api";
import { DownloadsPanel } from "./models/DownloadsPanel";
import { useDownloadsPoll, useInstalled } from "./models/hooks";
import { MODELS_TABS } from "./tabs";
export default function Downloader() {
  useDownloadsPoll();
  const installed = useInstalled();
  const catalog = useApi(getCatalog);
  const [id, setId] = useState("");
  const [inspection, setInspection] = useState<InspectResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [checking, setChecking] = useState(false);
  const [variant, setVariant] = useState("");
  const [languageOnly, setLanguageOnly] = useState(false);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<"downloads" | "likes" | "recent">(
    "downloads",
  );
  const results = useApi(
    (s) => searchHub(search, sort, s),
    [search, sort],
    !!search,
  );
  async function inspect(value: string) {
    setId(value);
    setChecking(true);
    setError(null);
    setInspection(null);
    try {
      const result = await inspectModel(value);
      setInspection(result);
      setVariant(result.recommended_variant ?? "");
      setLanguageOnly(!result.vision.available);
    } catch (e) {
      setError(e);
    } finally {
      setChecking(false);
    }
  }
  async function download() {
    try {
      await postDownload({
        id: inspection?.repo_id + (variant ? ":" + variant : ""),
        language_only: languageOnly,
      });
    } catch (e) {
      toastError("Could not queue the download.", e);
    }
  }
  return (
    <>
      <SubNav items={MODELS_TABS} label="Models" exact />
      <PageHeader title="Downloader." />
      <Section label="Download by ID">
        <label>
          Hugging Face model ID
          <input
            class="mono"
            value={id}
            placeholder="owner/repo[:variant]"
            onInput={(e) => setId(e.currentTarget.value)}
            onBlur={() => id && void inspect(id)}
          />
        </label>
        <Button onClick={() => void inspect(id)} disabled={!id || checking}>
          {checking ? "Checking…" : "Check compatibility"}
        </Button>
        {!!error && (
          <LoadError
            error={error}
            thing="models"
            onRetry={() => void inspect(id)}
          />
        )}
        {inspection && (
          <div class="stack">
            <p>
              {inspection.compatible ? "Compatible" : "Incompatible"} ·{" "}
              {inspection.reason ?? inspection.family}
            </p>
            {(inspection.variants ?? []).length > 0 && (
              <label>
                GGUF variant
                <select
                  value={variant}
                  onChange={(e) => setVariant(e.currentTarget.value)}
                >
                  {(inspection.variants ?? []).map((v) => (
                    <option
                      key={v.name}
                      value={v.name}
                      disabled={v.loadable === false}
                    >
                      {v.name} · {v.fit?.replace(/_/g, " ")}{" "}
                      {v.recommended ? "· recommended" : ""}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label>
              <input
                type="checkbox"
                checked={languageOnly}
                onChange={(e) => setLanguageOnly(e.currentTarget.checked)}
              />{" "}
              Language only
            </label>
            <Button
              variant="accent"
              disabled={!inspection.compatible}
              onClick={() => void download()}
            >
              Download
            </Button>
          </div>
        )}
      </Section>
      <Section label="Supported models">
        {!!catalog.error && (
          <LoadError
            error={catalog.error}
            thing="models"
            onRetry={catalog.reload}
          />
        )}{" "}
        {catalog.data?.families.map((f) => (
          <div key={f.family}>
            <h2 class="heading">{f.label}</h2>
            {f.groups
              .flatMap((g) => g.entries)
              .map((m) => (
                <div class="listrow" key={m.id}>
                  <button
                    class="text-button mono"
                    onClick={() => void inspect(m.id)}
                  >
                    {m.id}
                  </button>
                  <span class="meta">
                    {m.format} {m.installed ? "· installed" : ""}
                  </span>
                </div>
              ))}
          </div>
        ))}
      </Section>
      <Section label="Search Hugging Face">
        <form
          class="cluster"
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(query);
          }}
        >
          <label>
            Search
            <input
              value={query}
              onInput={(e) => setQuery(e.currentTarget.value)}
            />
          </label>
          <label>
            Sort
            <select
              value={sort}
              onChange={(e) => setSort(e.currentTarget.value as typeof sort)}
            >
              <option value="downloads">Downloads</option>
              <option value="likes">Likes</option>
              <option value="recent">Recent</option>
            </select>
          </label>
          <Button type="submit">Search</Button>
        </form>
        {!!results.error && (
          <LoadError
            error={results.error}
            thing="models"
            onRetry={results.reload}
          />
        )}
        <ul class="list">
          {results.data?.results.map((m) => (
            <li key={m.id}>
              <button
                class="text-button mono"
                onClick={() => void inspect(m.id)}
              >
                {m.id}
              </button>
              <span class="meta"> · {m.format_guess}</span>
            </li>
          ))}
        </ul>
      </Section>
      <DownloadsPanel
        installedIds={new Set(installed.data?.models.map((m) => m.id) ?? [])}
      />
    </>
  );
}
