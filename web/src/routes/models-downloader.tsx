import { useEffect, useRef, useState } from "preact/hooks";
import { Link, useSearchParams } from "wouter-preact";
import type { CatalogEntry, HfWhoami, InspectResult, SearchResult, SystemInfo, VariantOut } from "../api/models";
import { api, ApiError } from "../api/client";
import { Banner } from "../components/Banner";
import { Button, ExternalLink } from "../components/Button";
import { SearchInput, SegmentedControl } from "../components/controls";
import { Disclosure } from "../components/Disclosure";
import { Select, TextInput } from "../components/inputs";
import { Menu } from "../components/Menu";
import { PageHeader, Section } from "../components/Section";
import { Empty, ErrorState, LoadError, Loading } from "../components/States";
import { SubNav } from "../components/SubNav";
import { Tag } from "../components/Tag";
import { toast, toastError } from "../components/Toast";
import { Toggle } from "../components/Toggle";
import { DASH, formatBytes, formatCompact, formatRelativeTime } from "../lib/format";
import { isCancelled, withInstallConfirm } from "../lib/engine-install";
import { useApi } from "../lib/use-api";
import { engine } from "../store";
import { t } from "../strings/downloader";
import "../styles/pages/models.css";
import { getCatalog, getStorage, loadEngine, postDownload, searchHub, whoami, type SearchSort } from "./models/api";
import { streamInspect } from "./models/inspect";
import { CompatTag, FitTag, type CompatState } from "./models/bits";
import { DownloadsPanel } from "./models/DownloadsPanel";
import { useDownloadsPoll, useDrawerParam, useInstalled, useModelsTitle } from "./models/hooks";
import { ModelDrawer } from "./models/ModelDrawer";
import { checkModelId, diskCheck, formatLabel, hfUrl, isClefId, isLegacyId, isProjector, inspectProgress, MAX_LAZY_CHECKS, pickVariant, projectorRow, shortName } from "./models/logic";
import { VariantTable } from "./models/VariantTable";
import { MODELS_TABS } from "./tabs";

type Tab = "supported" | "id" | "search";

function readTab(v: string | null): Tab {
  return v === "id" || v === "search" ? v : "supported";
}

async function startDownload(id: string, opts: { language_only?: boolean; revision?: string; draft_model?: string } = {}) {
  try {
    await postDownload({ id, ...opts });
    toast(t("models.toast.queued", { model: shortName(id) }));
    requestAnimationFrame(() => document.getElementById("downloads")?.scrollIntoView({ block: "nearest" }));
  } catch (err) {
    if (err instanceof ApiError && err.code === "already_queued") toast(t("models.toast.already_queued", { short: shortName(id) }));
    else toastError(t("models.toast.download_failed", { short: shortName(id) }), err);
  }
}

/** Hub download sizes are decimal, as in the downloads panel and the plan. */
const HUB = { base: 1000 } as const;

function variantId(repo: string, v: string | null): string {
  return v ? `${repo}:${v}` : repo;
}

export default function Downloader() {
  useDownloadsPoll();
  useModelsTitle(t("downloader.page_title"));
  const installed = useInstalled();
  const installedIds = new Set(installed.data?.models.map((m) => m.id) ?? []);
  const [params, setParams] = useSearchParams();
  const tab = readTab(params.get("tab"));
  const setTab = (next: Tab) =>
    setParams(
      (prev) => {
        const p = new URLSearchParams(prev);
        if (next === "supported") p.delete("tab");
        else p.set("tab", next);
        return p;
      },
      { replace: true },
    );
  const system = useApi((s) => api.read<SystemInfo>("/system", undefined, s));
  const storage = useApi((s) => getStorage(s));
  const token = useApi<HfWhoami>((sig) => whoami(sig));
  const who = token.data;
  const tokenState = who
    ? who.status === "ok"
      ? t(`downloader.token.${who.source === "override" ? "override" : who.source === "env" ? "env" : "hf_login"}`, { user: who.user ?? "—" })
      : t(`downloader.token.${who.status}`)
    : null;
  const free = storage.data?.free_bytes ?? null;
  const s = system.data;
  const [drawerId, openDrawer, closeDrawer] = useDrawerParam();

  return (
    <>
      <SubNav items={MODELS_TABS} label={t("models.tabs_label")} exact />
      <PageHeader
        title={t("downloader.title")}
        meta={
          <span class="cluster dlr-meta">
            {s && <span>{t("downloader.mac", { chip: s.chip ?? DASH, ram: formatBytes(s.memory_bytes), free: free === null ? DASH : formatBytes(free) })}</span>}
            {tokenState && (
              <span>
                {t("downloader.token", { state: tokenState })}
                {who && (who.status === "no_token" || who.status === "rejected") && (
                  <>
                    {" · "}
                    <Link href="/settings/hf">{t("downloader.token_settings")} ↗</Link>
                  </>
                )}
              </span>
            )}
          </span>
        }
        actions={
          <SegmentedControl
            label={t("downloader.tabs")}
            value={tab}
            options={[
              { value: "supported", label: t("downloader.tab.supported") },
              { value: "id", label: t("downloader.tab.id") },
              { value: "search", label: t("downloader.tab.search") },
            ]}
            onChange={setTab}
          />
        }
      />
      {tab === "supported" && <Supported installedIds={installedIds} memory={s?.memory_bytes ?? null} onDetails={openDrawer} />}
      {tab === "id" && <ById initial={params.get("id") ?? ""} free={free} memory={s?.memory_bytes ?? null} installedIds={installedIds} />}
      {tab === "search" && <Search onOpen={openDrawer} />}
      <DownloadsPanel installedIds={installed.data ? installedIds : null} />
      <ModelDrawer id={drawerId} installed={installed.data?.models ?? []} activeId={engine.value?.model ?? null} onClose={closeDrawer} />
    </>
  );
}

// ---------- Supported ----------

function EntryRow({ e, index, installedIds, memory, onDetails }: { e: CatalogEntry; index: number; installedIds: ReadonlySet<string>; memory: number | null; onDetails: (id: string) => void }) {
  const variants = (e.variants ?? []).filter((v) => !isProjector(v.name));
  const rec = e.recommended_variant ?? null;
  const loadable = variants.filter((v) => v.loadable !== false);
  // A GGUF repo is installed per variant (`repo:VARIANT`): say which one, and load that one.
  const installedVariants = variants.filter((v) => installedIds.has(variantId(e.repo_id, v.name))).map((v) => v.name);
  const loadId = variants.length ? (installedVariants[0] ? variantId(e.repo_id, installedVariants[0]) : null) : e.installed || installedIds.has(e.id) ? e.id : null;
  // Choosing in the Variant menu selects; only the button downloads (Enter on the menu never starts a download).
  const [picked, setPicked] = useState<string | null>(null);
  const choice = variants.find((v) => v.name === (picked ?? rec)) ?? loadable[0] ?? null;
  const meta = [
    e.repo_id,
    e.memory_need_bytes ? t("downloader.needs", { size: formatBytes(e.memory_need_bytes) }) : null,
    variants.length ? t("downloader.variants", { n: variants.length }) : null,
    rec ? t("downloader.recommended_variant", { name: rec }) : null,
    e.vision === true ? t("downloader.vision") : e.vision === false ? t("downloader.text_only") : null,
    e.license,
    e.perf_note,
  ].filter(Boolean);
  const [loading, setLoading] = useState(false);
  // `download_bytes` is what the pick fetches (target + draft + vision, as /inspect's plan);
  // `size_bytes` is only the weights, so it is labelled as such (bug: "Download · 15 GB" fetched 19.93 GB).
  const sized = choice ?? e;
  const dlBytes = sized.download_bytes ?? (choice ? null : e.download_bytes);
  const dlSize = dlBytes ? formatBytes(dlBytes, HUB) : null;
  const weights = sized.size_bytes ?? e.size_bytes;
  const canDownload = variants.length > 0 ? !!choice && choice.loadable !== false && !installedVariants.includes(choice.name) : !loadId;
  return (
    <li class="dlr-entry" data-installed={loadId ? "true" : undefined}>
      <div class="dlr-entry-head">
        <h3 class="heading dlr-entry-name">{shortName(e.id)}</h3>
        <span class="cluster dlr-entry-tags">
          {e.recommended && (
            <Tag tone="acc" data-testid="recommended">
              {t("downloader.recommended")}
            </Tag>
          )}
          {variants.length > 0
            ? installedVariants.map((name) => <Tag key={name}>{t("downloader.variant_installed", { name })}</Tag>)
            : loadId && <Tag>{t("downloader.installed")}</Tag>}
          <span class="label">
            {String(index).padStart(2, "0")} — {formatLabel(e.format, e.id)}
            {dlSize ? ` · ${dlSize}` : weights ? ` · ${t("downloader.weights", { size: formatBytes(weights, HUB) })}` : ""}
          </span>
          {/* The entry's memory need is for its default variant; another pick has only its own `fit` (no need figure). */}
          <FitTag fit={(choice?.fit ?? e.fit) ?? null} needBytes={!choice || choice.name === rec ? e.memory_need_bytes : null} memoryBytes={memory} />
        </span>
      </div>
      <p class="meta dlr-entry-meta">
        <span class="mono">{meta[0]}</span>
        {meta.slice(1).map((m, i) => (
          <span key={i}> · {m}</span>
        ))}
      </p>
      {e.notes && <p class="meta">{e.notes}</p>}
      <div class="cluster">
        {loadId && (
          <Button
            size="s"
            variant={engine.value?.model || canDownload ? "outline" : "accent"}
            loading={loading}
            onClick={() => {
              setLoading(true);
              void withInstallConfirm((force) => loadEngine(loadId, force), loadId)
                .then(() => toast(t("models.toast.loading", { model: shortName(loadId) })))
                .catch((err) => isCancelled(err) || toastError(t("models.toast.load_failed", { short: shortName(loadId) }), err))
                .finally(() => setLoading(false));
            }}
          >
            {installedVariants.length > 0 ? t("downloader.load_variant", { name: installedVariants[0]! }) : t("downloader.load")}
          </Button>
        )}
        {variants.length > 0 ? (
          <span class="cluster">
            <Button size="s" variant="solid" disabled={!canDownload} onClick={() => choice && void startDownload(variantId(e.repo_id, choice.name))}>
              {choice && dlSize ? t("downloader.download_variant_size", { name: choice.name, size: dlSize }) : t("downloader.download_variant", { name: choice?.name ?? "" })}
            </Button>
            <Menu
              label={t("downloader.variant")}
              size="s"
              variant="text"
              radio
              items={variants.map((v) => ({
                key: v.name,
                label: <span class="mono">{v.name}</span>,
                text: v.name,
                checked: v.name === choice?.name,
                disabled: v.loadable === false,
                detail:
                  [
                    v.download_bytes ? formatBytes(v.download_bytes, HUB) : v.size_bytes ? t("downloader.weights", { size: formatBytes(v.size_bytes, HUB) }) : null,
                    v.loadable === false ? (v.reason ?? t("models.variant.unsupported_default")) : installedVariants.includes(v.name) ? t("downloader.installed") : null,
                  ]
                    .filter(Boolean)
                    .join(" · ") || undefined,
                onSelect: () => setPicked(v.name),
              }))}
            />
          </span>
        ) : (
          !loadId && (
            <Button size="s" variant="solid" disabled={e.fit === "wont_fit"} onClick={() => void startDownload(e.id)}>
              {dlSize ? t("downloader.download_size", { size: dlSize }) : t("downloader.download")}
            </Button>
          )
        )}
        <Button size="s" variant="text" onClick={() => onDetails(e.id)}>
          {t("downloader.details")}
        </Button>
      </div>
    </li>
  );
}

function Supported({ installedIds, memory, onDetails }: { installedIds: ReadonlySet<string>; memory: number | null; onDetails: (id: string) => void }) {
  const catalog = useApi(getCatalog);
  if (catalog.error) return <Section><LoadError thing={t("downloader.thing_catalog")} error={catalog.error} onRetry={catalog.reload} /></Section>;
  if (!catalog.data) return <Section><Loading /></Section>;
  let index = 0;
  return (
    <>
      {catalog.data.offline && (
        <section class="band tight">
          <Banner tone="info">{t("downloader.offline")}</Banner>
        </section>
      )}
      {catalog.data.families.map((f) => (
        <Section key={f.family} label={f.label}>
          <ul class="dlr-entries">
            {f.groups.flatMap((g) => g.entries).map((e) => (
              <EntryRow key={e.id} e={e} index={++index} installedIds={installedIds} memory={memory ?? catalog.data!.memory_bytes} onDetails={onDetails} />
            ))}
          </ul>
        </Section>
      ))}
      <section class="band tight">
        <p class="meta">{t("downloader.estimate")}</p>
      </section>
    </>
  );
}

// ---------- Download by ID ----------

function ById({ initial, free, memory, installedIds }: { initial: string; free: number | null; memory: number | null; installedIds: ReadonlySet<string> }) {
  const [id, setId] = useState(initial);
  const [result, setResult] = useState<InspectResult | null>(null);
  const [checked, setChecked] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [variant, setVariant] = useState<string | null>(null);
  const [revision, setRevision] = useState("");
  const [draft, setDraft] = useState("");
  const [languageOnly, setLanguageOnly] = useState(false);
  const ctrl = useRef<AbortController | null>(null);
  const v = checkModelId(id);

  async function check(value = id) {
    const trimmed = value.trim();
    if (!trimmed || checkModelId(trimmed).error) return;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setBusy(true);
    setError(null);
    setResult(null);
    setChecked(trimmed);
    setVariant(null);
    const requested = trimmed.includes(":") ? trimmed.split(":")[1]! : null;
    // The likely pick's verdict arrives first; keep a variant the user picked meanwhile.
    const keep = (r: InspectResult) => (current: string | null) =>
      current && r.variants?.some((x) => x.name === current && x.loadable !== false) ? current : pickVariant(r, requested);
    try {
      const r = await streamInspect(trimmed, {
        signal: c.signal,
        onProgress: (partial) => {
          if (c.signal.aborted) return;
          setResult(partial);
          setVariant(keep(partial));
        },
      });
      if (c.signal.aborted) return;
      setResult(r);
      setVariant(keep(r));
      setLanguageOnly(r.badge === "text_only");
    } catch (err) {
      if (!c.signal.aborted) setError(err);
    } finally {
      if (!c.signal.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    if (initial) void check(initial);
  }, [initial]);

  const repo = result?.repo_id ?? id.split(":")[0] ?? "";
  const variants = (result?.variants ?? []).filter((x) => !isProjector(x.name));
  const chosen: VariantOut | undefined = variants.find((x) => x.name === variant);
  const legacy = isLegacyId(repo) || result?.format === "legacy";
  // The manager's plan is for the requested/recommended variant; another pick falls back to its size.
  const lang = languageOnly || result?.badge === "text_only";
  const rawPlan = lang ? result?.language_only_plan : result?.download_plan;
  const plan = rawPlan && (!variants.length || rawPlan.variant === variant) ? rawPlan : null;
  const size = plan ? plan.remaining_bytes : (chosen?.size_bytes ?? null);
  const disk = plan
    ? { ok: plan.fits_on_disk, neededBytes: plan.remaining_bytes + plan.margin_bytes, freeBytes: plan.free_bytes ?? free }
    : diskCheck(size, free);
  const target = variants.length ? variantId(repo, variant) : (result?.id ?? id.trim());
  const progress = inspectProgress(result);
  const isInstalled = installedIds.has(target);

  return (
    <Section label={t("downloader.id_label")}>
      <div class="stack">
        <div class="cluster">
          <TextInput
            class="mono dlr-id"
            value={id}
            aria-label={t("downloader.id_label")}
            placeholder="owner/repo[:variant]"
            invalid={!!v.error}
            onChange={setId}
            onBlur={() => id.trim() && id.trim() !== checked && void check()}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void check();
              }
            }}
          />
          <Button disabled={!id.trim() || !!v.error || busy} onClick={() => void check()}>
            {t("downloader.check")}
          </Button>
        </div>
        <p class="field-help">{t("downloader.id_help")}</p>
        {v.error && (
          <p class="field-error" role="alert">
            {v.error} {v.hint}
          </p>
        )}
        {busy && !result && <Loading label={t("downloader.checking", { id: id.trim() })} />}
        {!!error && <LoadError thing={t("downloader.thing_inspect")} error={error} onRetry={() => void check()} />}
        {result && (
          <div class="stack dlr-result">
            <div class="cluster">
              <CompatTag state={result.badge as CompatState} />
              <span class="meta">
                {[result.family, result.format && formatLabel(result.format, result.id), result.vision.available ? t("downloader.vision_line", { v: result.vision.projector ?? "on" }) : null, result.draft ? t("downloader.draft_line", { v: result.draft }) : null]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
              <FitTag fit={result.fit ?? null} needBytes={result.memory_need_bytes} memoryBytes={memory} />
            </div>
            {result.badge === "incompatible" && result.reason && <p class="body" data-testid="compat-reason">{result.reason}</p>}
            {result.badge === "incompatible" && result.reason_detail && <p class="meta mono" data-testid="compat-detail">{result.reason_detail}</p>}
            {result.badge === "text_only" && <p class="meta">{t("downloader.no_vision", { reason: result.vision.reason ?? DASH })}</p>}
            {(result.badge === "not_clef_accurate" || isClefId(result.id)) && (
              <Banner tone="info" title={t("models.clef.title")}>
                {t("models.clef.body")}
              </Banner>
            )}
            {progress && (
              <p class="meta tnum" data-testid="inspect-progress">
                {t("models.byid.progress", { checked: progress.checked, total: progress.total })}
              </p>
            )}
            {result.badge === "checking" && variants.length > 0 && (
              <VariantTable variants={variants} projector={projectorRow(result.vision)} pending={result.pending} caption={t("downloader.variant")} installed={installedIds} />
            )}
            {result.compatible && (
              <>
                {variants.length > 0 && (
                  <VariantTable variants={variants} projector={projectorRow(result.vision)} selected={variant} onSelect={setVariant} pending={result.pending} caption={t("downloader.variant")} installed={installedIds} />
                )}
                <Disclosure summary={t("downloader.options")}>
                  {legacy ? (
                    <p class="meta">{t("downloader.legacy_disabled")}</p>
                  ) : (
                    <div class="stack">
                      <label class="cluster">
                        <span class="label">{t("downloader.revision")}</span>
                        <TextInput class="mono" value={revision} placeholder="main" onChange={setRevision} />
                      </label>
                      <label class="cluster">
                        <span class="label">{t("downloader.draft_override")}</span>
                        <TextInput class="mono" value={draft} placeholder="auto" onChange={setDraft} />
                      </label>
                      <div class="cluster">
                        <Toggle checked={languageOnly} disabled={result.badge === "text_only"} onChange={setLanguageOnly} label={t("downloader.language_only")} />
                        <span class="label">{t("downloader.language_only")}</span>
                        <span class="meta flag mono">--language-only</span>
                      </div>
                    </div>
                  )}
                </Disclosure>
                {result.badge === "text_only" && <p class="meta">{t("downloader.text_forced")}</p>}
                {plan ? (
                  <div class="stack dlr-plan" data-testid="download-plan">
                    <ul class="dlr-plan-files">
                      {(plan.files ?? []).map((f) => (
                        <li key={`${f.repo_id}/${f.name}`} class="cluster">
                          <span class="mono">{f.name}</span>
                          <span class="meta tnum">{f.bytes != null ? formatBytes(f.bytes, HUB) : DASH}</span>
                          {f.present && <span class="meta">{t("downloader.plan_present")}</span>}
                          {f.repo_id !== result.repo_id && <span class="meta mono">{f.repo_id}</span>}
                        </li>
                      ))}
                    </ul>
                    <p class="meta tnum">
                      {t("downloader.plan_line", {
                        remaining: formatBytes(plan.remaining_bytes, HUB),
                        total: formatBytes(plan.total_bytes, HUB),
                        free: plan.free_bytes != null ? formatBytes(plan.free_bytes, HUB) : DASH,
                      })}
                    </p>
                  </div>
                ) : (
                  <p class="meta tnum">
                    {size !== null ? t("downloader.size_free", { size: t("downloader.weights", { size: formatBytes(size, HUB) }), free: free === null ? DASH : formatBytes(free, HUB) }) : null}
                  </p>
                )}
                {!disk.ok && (
                  <Banner tone="warn" actions={<Link href="/settings/storage">{t("models.disk.storage_settings")} ↗</Link>}>
                    {t("downloader.no_space", { need: formatBytes(disk.neededBytes, HUB), free: formatBytes(disk.freeBytes ?? 0, HUB) })}
                  </Banner>
                )}
                <div class="cluster">
                  <Button
                    variant="accent"
                    disabled={!disk.ok || isInstalled || (variants.length > 0 && (!variant || chosen?.loadable !== true))}
                    onClick={() =>
                      void startDownload(target, {
                        language_only: languageOnly || result.badge === "text_only",
                        ...(revision.trim() ? { revision: revision.trim() } : {}),
                        ...(draft.trim() ? { draft_model: draft.trim() } : {}),
                      })
                    }
                  >
                    {plan ? t("downloader.download_size", { size: formatBytes(plan.remaining_bytes, HUB) }) : t("downloader.download")}
                  </Button>
                  {isInstalled && <Tag>{t("downloader.installed")}</Tag>}
                  <ExternalLink href={hfUrl(repo)}>{t("downloader.hf")}</ExternalLink>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </Section>
  );
}

// ---------- Search ----------

/**
 * A search hit's format is a guess from its name and tags (`format_guess`): "MLX 4-bit" only once the
 * check says Splash can load it (Splash takes MLX at 4-bit, group size 64 only); before, just "MLX".
 */
export function searchFormat(guess: SearchResult["format_guess"], id: string, state: CompatState): string {
  if (guess === "unknown") return DASH;
  if (guess === "mlx" && state !== "compatible" && state !== "text_only") return t("downloader.format.mlx");
  return formatLabel(guess, id);
}

function SearchRow({ r, index, state, onVisible, onOpen }: { r: SearchResult; index: number; state: CompatState; onVisible: () => void; onOpen: () => void }) {
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined" || state !== "unchecked") return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((x) => x.isIntersecting)) {
          onVisible();
          io.disconnect();
        }
      },
      { rootMargin: "100% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [state]);
  return (
    <li ref={ref} class="dlr-entry">
      <div class="dlr-entry-head">
        <h3 class="heading dlr-entry-name">{shortName(r.id)}</h3>
        <span class="cluster dlr-entry-tags">
          <CompatTag state={state} />
          <span class="label">
            {String(index).padStart(2, "0")} — {searchFormat(r.format_guess, r.id, state)}
            {r.downloads != null ? ` · ${t("downloader.downloads_n", { n: formatCompact(r.downloads) })}` : ""}
          </span>
        </span>
      </div>
      <p class="meta">
        <span class="mono">{r.id}</span>
        {r.last_modified ? ` · ${t("downloader.updated", { when: formatRelativeTime(r.last_modified) })}` : ""}
      </p>
      <div class="cluster">
        <Button size="s" variant="text" onClick={onOpen}>
          {t("downloader.details")}
        </Button>
        <ExternalLink href={hfUrl(r.id)}>{t("downloader.hf")}</ExternalLink>
      </div>
    </li>
  );
}

function Search({ onOpen }: { onOpen: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SearchSort>("downloads");
  const [format, setFormat] = useState<"all" | "mlx" | "gguf">("all");
  const [states, setStates] = useState<Record<string, CompatState>>({});
  const queue = useRef<string[]>([]);
  const inFlight = useRef(0);
  useEffect(() => {
    const id = setTimeout(() => setQ(query.trim()), 400);
    return () => clearTimeout(id);
  }, [query]);
  const results = useApi((s) => searchHub(q, sort, s), [q, sort], !!q);
  const pump = () => {
    while (inFlight.current < MAX_LAZY_CHECKS && queue.current.length) {
      const id = queue.current.shift()!;
      inFlight.current += 1;
      setStates((st) => ({ ...st, [id]: "checking" }));
      // The badge settles with the first verdict that decides it (the likely pick's),
      // then this row stops listening; the manager finishes the check and keeps it.
      const ctrl = new AbortController();
      const settle = (r: InspectResult) => {
        if (r.badge === "checking") return;
        setStates((st) => ({ ...st, [id]: r.badge as CompatState }));
        ctrl.abort();
      };
      void streamInspect(id, { signal: ctrl.signal, onProgress: settle })
        .then(settle)
        .catch(() => {
          if (!ctrl.signal.aborted) setStates((st) => ({ ...st, [id]: "error" }));
        })
        .finally(() => {
          inFlight.current -= 1;
          pump();
        });
    }
  };
  const list = (results.data?.results ?? []).filter((r) => format === "all" || r.format_guess === format);
  return (
    <Section label={t("downloader.search")}>
      <div class="stack">
        <div class="cluster">
          <SearchInput value={query} onChange={setQuery} label={t("downloader.search")} placeholder={t("downloader.search_placeholder")} primary />
          <label class="cluster">
            <span class="label">{t("downloader.sort")}</span>
            <Select value={sort} options={(["downloads", "likes", "recent"] as const).map((x) => ({ value: x, label: t(`downloader.sort.${x}`) }))} onChange={(v) => setSort(v as SearchSort)} />
          </label>
          <label class="cluster">
            <span class="label">{t("downloader.format")}</span>
            <Select value={format} options={(["all", "mlx", "gguf"] as const).map((x) => ({ value: x, label: t(`downloader.format.${x}`) }))} onChange={(v) => setFormat(v as typeof format)} />
          </label>
        </div>
        {!q ? null : results.error ? (
          <ErrorState error={new Error(t("downloader.hub_down"))} onRetry={results.reload} />
        ) : !results.data ? (
          <Loading />
        ) : list.length === 0 ? (
          <Empty title={t("downloader.no_results", { q })}>{t("downloader.no_results_body")}</Empty>
        ) : (
          <>
            <p class="meta">
              {t("downloader.results", { n: list.length })} · {t("downloader.lazy_note")}
            </p>
            <ul class="dlr-entries">
              {list.map((r, i) => (
                <SearchRow
                  key={r.id}
                  r={r}
                  index={i + 1}
                  state={states[r.id] ?? "unchecked"}
                  onVisible={() => {
                    if (states[r.id] || queue.current.includes(r.id)) return;
                    queue.current.push(r.id);
                    pump();
                  }}
                  onOpen={() => onOpen(r.id)}
                />
              ))}
            </ul>
          </>
        )}
        {results.error ? (
          <Link href="/models" class="meta">
            {t("downloader.offline_link")} ↗
          </Link>
        ) : null}
      </div>
    </Section>
  );
}
