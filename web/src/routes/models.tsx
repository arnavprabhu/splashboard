import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Link, useSearchParams } from "wouter-preact";
import type { DownloadItem, InstalledModel, JobEvent } from "../api/models";
import { Button } from "../components/Button";
import { ConfirmSheet } from "../components/ConfirmSheet";
import { PageHeader } from "../components/Section";
import { Empty, LoadError } from "../components/States";
import { SubNav } from "../components/SubNav";
import { toast, toastError } from "../components/Toast";
import { formatBytes } from "../lib/format";
import { activeDownloads, engine, settings, useEvent } from "../store";
import { t } from "../strings/models";
import "../styles/pages/models.css";
import * as flows from "./models/actions";
import { updateModel, verifyModel } from "./models/api";
import { CancelSheet } from "./models/CancelSheet";
import { DeleteSheet } from "./models/DeleteSheet";
import { DiskBand } from "./models/DiskBand";
import {
  useDownloadsPoll,
  useDrawerParam,
  useInstalled,
  useModelsTitle,
} from "./models/hooks";
import { LocalDrop } from "./models/LocalDrop";
import { deletePlan, shortName, sortInstalled } from "./models/logic";
import { ManagerRow } from "./models/ManagerRow";
import { ModelDrawer } from "./models/ModelDrawer";
import { RowList } from "./models/Row";
import { MODELS_TABS } from "./tabs";

/** The engine's model counts as active unless the engine is stopped. */
function activeModelId(): string | null {
  const e = engine.value;
  if (!e || !e.model) return null;
  return e.state === "stopped" ? null : e.model;
}

/** Models → Manager (docs/ui/03 §1, SPEC §10.4, §9.5). */
export default function ModelsManager() {
  useModelsTitle(t("models.page_title"));
  useDownloadsPoll();
  const installed = useInstalled();
  const [params, setParams] = useSearchParams();
  const [drawerId, openDrawer, closeDrawer] = useDrawerParam();
  const activeId = activeModelId();
  const list = installed.data?.models ?? [];
  const disk = installed.data?.disk ?? null;

  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState<InstalledModel[] | null>(null);
  const [deleteAll, setDeleteAll] = useState(false);
  const [cancelling, setCancelling] = useState<DownloadItem | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const [verifying, setVerifying] = useState<Map<string, number>>(new Map());
  const [verifyErrors, setVerifyErrors] = useState<Map<string, string>>(
    new Map(),
  );

  const activeModel = activeId
    ? (list.find((m) => m.id === activeId) ?? null)
    : null;
  const others = sortInstalled(list.filter((m) => m.id !== activeId));
  const installedIds = new Set(list.map((m) => m.id));
  const dlByModel = new Map<string, DownloadItem>();
  for (const d of activeDownloads.value) dlByModel.set(d.model, d);
  const downloadOnly = activeDownloads.value.filter(
    (d) => !installedIds.has(d.model) && d.model !== activeId,
  );
  const firstUpdate =
    others.find((m) => m.status === "update_available" && !m.pinned)?.id ??
    null;
  const deletable = others;

  // ?select=all — "Delete all models" from Settings → Data: every non-active row checked, sheet open.
  const selectAllHandled = useRef(false);
  useEffect(() => {
    if (
      params.get("select") !== "all" ||
      selectAllHandled.current ||
      !installed.data
    )
      return;
    selectAllHandled.current = true;
    setSelecting(true);
    setSelected(new Set(deletable.map((m) => m.id)));
    if (deletable.length) {
      setDeleteAll(true);
      setDeleting(deletable);
    }
  }, [installed.data, params]);

  // Verify progress: `job` events when the stream is up; otherwise poll the list.
  useEvent("job", (data) => {
    const job = data as Partial<JobEvent>;
    if (job.kind !== "verify" || !job.model || job.state === "running") return;
    finishVerify(
      job.model,
      job.state === "failed"
        ? (job.message ?? t("models.verify.failed_default"))
        : null,
    );
  });
  useEffect(() => {
    if (verifying.size === 0) return;
    const timer = setInterval(() => void installed.reload(), 3000);
    return () => clearInterval(timer);
  }, [verifying.size]);
  useEffect(() => {
    if (verifying.size === 0 || !installed.data) return;
    const now = Date.now();
    for (const [id, startedAt] of verifying) {
      const m = installed.data.models.find((x) => x.id === id);
      if (!m || now - startedAt < 2500 || m.status === "verifying") continue;
      finishVerify(
        id,
        m.status === "broken" ? t("models.verify.failed_default") : null,
      );
    }
  }, [installed.data]);

  function finishVerify(id: string, error: string | null) {
    setVerifying((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Map(prev);
      next.delete(id);
      return next;
    });
    setVerifyErrors((prev) => {
      const next = new Map(prev);
      if (error) next.set(id, error);
      else next.delete(id);
      return next;
    });
    if (error)
      toast(t("models.verify.failed_toast", { short: shortName(id) }), {
        tone: "error",
        detail: error,
      });
    else toast(t("models.verify.done", { short: shortName(id) }));
    void installed.reload();
  }

  const verify = async (m: InstalledModel) => {
    const full = Boolean(
      settings.value?.settings.global.downloads?.full_verify,
    );
    try {
      await verifyModel(m.id, full);
      setVerifying((prev) => new Map(prev).set(m.id, Date.now()));
      setVerifyErrors((prev) => {
        const next = new Map(prev);
        next.delete(m.id);
        return next;
      });
    } catch (err) {
      toastError(
        t("models.toast.verify_failed", { short: shortName(m.id) }),
        err,
      );
    }
  };

  const update = async (m: InstalledModel) => {
    try {
      const item = await updateModel(m.id);
      if (item && typeof item.id === "string") void flows.startDownload; // queued by the manager
      toast(t("models.toast.updating", { model: shortName(m.id) }));
    } catch (err) {
      toastError(
        t("models.toast.update_failed", { short: shortName(m.id) }),
        err,
      );
    }
  };

  const unload = () => {
    if ((engine.value?.requests_in_flight ?? 0) > 0) setConfirmStop(true);
    else void flows.unloadModel();
  };

  const toggle = (id: string, checked: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });

  const endSelect = () => {
    setSelecting(false);
    setSelected(new Set());
    if (params.get("select")) {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("select");
          return next;
        },
        { replace: true },
      );
    }
  };

  const selectedModels = useMemo(
    () => deletable.filter((m) => selected.has(m.id)),
    [deletable, selected],
  );
  const selectedPlan = deletePlan(selectedModels, activeId);

  const totalBytes = list.reduce((s, m) => s + (m.size_bytes ?? 0), 0);
  const metaParts = installed.data
    ? [
        t("models.manager.count", { n: list.length }),
        formatBytes(totalBytes),
        disk
          ? t("models.manager.free", { size: formatBytes(disk.free_bytes) })
          : null,
      ].filter(Boolean)
    : [installed.loading ? t("models.manager.count_loading") : null].filter(
        Boolean,
      );

  const headerActions = selecting ? (
    <>
      <Button
        variant="solid"
        disabled={selectedModels.length === 0}
        onClick={() => setDeleting(selectedModels)}
        data-testid="bulk-delete"
      >
        {selectedModels.length === 0
          ? t("models.select.delete_none")
          : t("models.select.delete", {
              n: selectedModels.length,
              size: formatBytes(selectedPlan.freesBytes),
            })}
      </Button>
      <Button onClick={endSelect}>{t("common.cancel")}</Button>
    </>
  ) : (
    <>
      {deletable.length > 0 && (
        <Button onClick={() => setSelecting(true)}>
          {t("models.select.start")}
        </Button>
      )}
      <Link href="/models/downloader" class="btn">
        {t("models.manager.downloader")}
      </Link>
    </>
  );

  const empty =
    installed.data &&
    list.length === 0 &&
    downloadOnly.length === 0 &&
    !activeId;
  let index = 0;

  const rowProps = (m: InstalledModel | null, id: string) => ({
    id,
    model: m,
    index: ++index,
    active: id === activeId,
    download: dlByModel.get(id),
    verifying: verifying.has(id),
    verifyError: verifyErrors.get(id),
    accentUpdate: id === firstUpdate,
    selecting,
    selected: selected.has(id),
    onToggle: (c: boolean) => toggle(id, c),
    onOpen: () => openDrawer(id),
    onUnload: unload,
    onVerify: () => m && void verify(m),
    onUpdate: () => m && void update(m),
    onDelete: () => m && setDeleting([m]),
    onCancel: (item: DownloadItem) => setCancelling(item),
  });

  return (
    <>
      <SubNav items={MODELS_TABS} label={t("models.tabs_label")} exact />
      <PageHeader
        title={t("models.manager.title")}
        meta={<span class="tnum">{metaParts.join(" · ")}</span>}
        actions={headerActions}
      />

      {empty ? (
        <section class="band" data-testid="models-empty">
          <Empty
            size="l"
            title={t("models.empty.title")}
            action={
              <Link href="/models/downloader" class="btn" data-variant="accent">
                {t("models.empty.action")}
              </Link>
            }
          >
            {t("models.empty.body")}
          </Empty>
        </section>
      ) : (
        <>
          {activeId && (
            <section
              class="band mband"
              aria-labelledby="models-active-label"
              data-testid="active-band"
            >
              <h2 class="label mband-head" id="models-active-label">
                {t("models.manager.active")}
              </h2>
              <RowList label={t("models.manager.active")}>
                <ManagerRow
                  {...rowProps(
                    activeModel ?? { ...placeholder(activeId) },
                    activeId,
                  )}
                />
              </RowList>
            </section>
          )}
          <section
            class="band mband"
            aria-labelledby="models-installed-label"
            data-loading={
              installed.loading && !installed.data ? "true" : undefined
            }
            data-testid="installed-band"
          >
            <h2 class="label mband-head" id="models-installed-label">
              {t("models.manager.installed")}
            </h2>
            {installed.error && !installed.data ? (
              <div class="mband-head">
                <LoadError
                  thing={t("models.manager.thing")}
                  error={installed.error}
                  onRetry={() => void installed.reload()}
                />
              </div>
            ) : others.length === 0 && downloadOnly.length === 0 ? (
              installed.data && (
                <p class="meta mband-head">{t("models.manager.only_active")}</p>
              )
            ) : (
              <RowList
                label={t("models.manager.installed")}
                testId="installed-list"
              >
                {others.map((m) => (
                  <ManagerRow key={m.id} {...rowProps(m, m.id)} />
                ))}
                {downloadOnly.map((d) => (
                  <ManagerRow key={d.id} {...rowProps(null, d.model)} />
                ))}
              </RowList>
            )}
          </section>
        </>
      )}

      <LocalDrop
        modelsDir={disk?.models_dir ?? null}
        onRescanned={() => void installed.reload()}
      />
      <DiskBand disk={disk} loading={installed.loading && !installed.data} />

      <ModelDrawer
        id={drawerId}
        installed={list}
        activeId={activeId}
        onClose={closeDrawer}
        onDelete={(m) => {
          closeDrawer();
          setDeleting([m]);
        }}
        onVerify={(m) => void verify(m)}
      />
      <DeleteSheet
        open={deleting !== null}
        models={deleting ?? []}
        activeId={activeId}
        freeBytes={disk?.free_bytes ?? null}
        all={deleteAll}
        onClose={() => {
          setDeleting(null);
          setDeleteAll(false);
        }}
        onDone={(ids) => {
          installed.setData((prev) =>
            prev
              ? {
                  ...prev,
                  models: prev.models.filter((m) => !ids.includes(m.id)),
                }
              : prev,
          );
          endSelect();
          void installed.reload();
        }}
      />
      <CancelSheet item={cancelling} onClose={() => setCancelling(null)} />
      <ConfirmSheet
        open={confirmStop}
        title={t("models.stop.title")}
        confirmLabel={t("models.action.unload")}
        important
        onConfirm={async () => {
          await flows.unloadModel();
          setConfirmStop(false);
        }}
        onClose={() => setConfirmStop(false)}
      >
        <p class="body">
          {t("models.stop.body", { n: engine.value?.requests_in_flight ?? 0 })}
        </p>
      </ConfirmSheet>
    </>
  );
}

/** The active row when GET /models has not answered: rendered from /engine alone (03 §1.7). */
function placeholder(id: string): InstalledModel {
  return {
    id,
    repo_id: id.split(":")[0]!,
    format: /gguf/i.test(id) ? "gguf" : "mlx",
    language_only: false,
    size_bytes: 0,
    unique_bytes: 0,
    pinned: false,
    legacy: false,
    status: "active",
  } as InstalledModel;
}
