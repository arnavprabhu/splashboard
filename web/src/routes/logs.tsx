import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "preact/hooks";
import { Link, useSearchParams } from "wouter-preact";
import { ADMIN, api } from "../api/client";
import type { DeletedBytes } from "../api/models";
import { Button } from "../components/Button";
import { ConfirmSheet } from "../components/ConfirmSheet";
import { SearchInput, SegmentedControl } from "../components/controls";
import { Menu } from "../components/Menu";
import { PageHeader } from "../components/Section";
import { Empty, LoadError } from "../components/States";
import { SubNav } from "../components/SubNav";
import { toast, toastError } from "../components/Toast";
import { Toggle } from "../components/Toggle";
import { formatBytes } from "../lib/format";
import { useTitle } from "../lib/title";
import { engine, loadSettings, settings } from "../store";
import { t } from "../strings/logs";
import { LiveLog } from "./logs/LiveLog";
import {
  levelCounts,
  matchIndices,
  parseLevel,
  visibleRows,
  type LevelFilter,
  type LogSource,
} from "./logs/model";
import { useLogStream } from "./logs/useLogStream";
import { LOGS_TABS } from "./tabs";
import "../styles/pages/logs.css";

const SEARCH_DEBOUNCE_MS = 200;

function stored(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v === "1";
  } catch {
    return fallback;
  }
}

function store(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* private mode */
  }
}

function useStoredToggle(
  key: string,
  fallback: boolean,
): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState(() => stored(key, fallback));
  return [
    value,
    (v: boolean) => {
      setValue(v);
      store(key, v);
    },
  ];
}

function download(href: string): void {
  const a = document.createElement("a");
  a.href = href;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Logs → Live tail (docs/ui/06 §1–§4, SPEC §10.8). */
export default function LogsPage() {
  useTitle(t("logs.page_title"));
  const [params, setParams] = useSearchParams();
  const source: LogSource =
    params.get("source") === "manager" ? "manager" : "engine";
  const level = parseLevel(params.get("level"));
  const requests = params.get("requests") !== "0";
  const urlQuery = params.get("q") ?? "";

  const setParam = useCallback(
    (key: string, value: string | null) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value === null || value === "") next.delete(key);
          else next.set(key, value);
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  // Search: typed value updates at once, the URL (and the filter) after 200 ms.
  const [typed, setTyped] = useState(urlQuery);
  const [query, setQuery] = useState(urlQuery);
  useEffect(() => {
    const id = setTimeout(() => {
      setQuery(typed);
      if (typed !== urlQuery) setParam("q", typed || null);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typed]);

  const [filterMode, setFilterMode] = useState(false);
  const [timestamps, setTimestamps] = useStoredToggle(
    "splash-gui-logs-timestamps",
    true,
  );
  const [wrap, setWrap] = useStoredToggle(
    "splash-gui-logs-wrap",
    typeof window !== "undefined" && window.innerWidth < 600,
  );
  const [more, setMore] = useState(false);
  const [follow, setFollowState] = useState(true);
  const [mark, setMark] = useState(0);
  const [current, setCurrent] = useState(-1);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);

  useEffect(() => {
    void loadSettings();
  }, []);

  const command =
    (engine.value?.view.command as string | null | undefined) ?? null;
  const stream = useLogStream(source, command);
  const { buffer } = stream;

  const appendedRef = useRef(buffer.appended);
  appendedRef.current = buffer.appended;
  const setFollow = useCallback((v: boolean) => {
    setFollowState(v);
    if (!v) setMark(appendedRef.current);
  }, []);
  // Switching source re-follows.
  useEffect(() => {
    setFollowState(true);
    setCurrent(-1);
  }, [source]);

  const rows = useMemo(
    () => visibleRows(buffer.rows, { level, requests, query, filterMode }),
    [buffer.rows, level, requests, query, filterMode],
  );
  const matches = useMemo(() => matchIndices(rows, query), [rows, query]);
  const matchSet = useMemo(() => new Set(matches), [matches]);
  const counts = useMemo(() => levelCounts(buffer.rows), [buffer.rows]);
  useEffect(() => setCurrent(-1), [query, filterMode, level, requests, source]);

  const jump = (dir: 1 | -1) => {
    if (matches.length === 0) return;
    const pos = matches.indexOf(current);
    const next =
      pos < 0
        ? dir === 1
          ? 0
          : matches.length - 1
        : (pos + dir + matches.length) % matches.length;
    setFollowState(false);
    setMark(buffer.appended);
    setCurrent(matches[next]!);
  };

  const levelLabel = (value: LevelFilter) => {
    const base = t(`logs.level_filter.${value}`);
    const n =
      value === "warn" ? counts.warn : value === "error" ? counts.error : 0;
    return n > 0 ? `${base} ${n.toLocaleString("en-US")}` : base;
  };

  const resolved = settings.value?.resolved as { base?: string } | undefined;
  const logsDir = resolved?.base ? `${resolved.base}/logs` : null;

  const onClear = async () => {
    setClearing(true);
    try {
      const res = await api.del<DeletedBytes>("/logs");
      stream.clear();
      setConfirmClear(false);
      toast(
        t("logs.clear.done", { size: formatBytes(res?.deleted_bytes ?? 0) }),
      );
    } catch (err) {
      toastError(t("logs.clear.failed"), err);
    } finally {
      setClearing(false);
    }
  };

  const searchKeys = (e: KeyboardEvent) => {
    if (
      e.key === "Enter" &&
      (e.target as HTMLElement).getAttribute("type") === "search"
    ) {
      e.preventDefault();
      jump(e.shiftKey ? -1 : 1);
    }
  };

  const currentPos = matches.indexOf(current);
  const label =
    source === "engine" ? t("logs.pane.engine") : t("logs.pane.manager");

  return (
    <>
      <SubNav items={LOGS_TABS} label={t("logs.subnav")} exact />
      <PageHeader
        title={t("logs.title")}
        meta={logsDir ? <span class="mono">{logsDir}</span> : undefined}
      />
      <section class="band tight logs-band" aria-label={t("logs.toolbar")}>
        <div class="logs-toolbar">
          <div class="logs-tools-primary">
            <SegmentedControl
              label={t("logs.source")}
              value={source}
              options={[
                { value: "engine", label: t("logs.source.engine") },
                { value: "manager", label: t("logs.source.manager") },
              ]}
              onChange={(v) => setParam("source", v === "engine" ? null : v)}
            />
            <div class="logs-field">
              <span class="label" aria-hidden="true">
                {t("logs.level")}
              </span>
              <SegmentedControl
                label={t("logs.level")}
                value={level}
                options={(["all", "info", "warn", "error"] as const).map(
                  (v) => ({ value: v, label: levelLabel(v) }),
                )}
                onChange={(v) => setParam("level", v === "all" ? null : v)}
              />
            </div>
            <div class="logs-search" onKeyDown={searchKeys}>
              <SearchInput
                value={typed}
                onChange={setTyped}
                label={t("logs.search")}
                primary
              />
              <span class="meta tnum logs-match-count" aria-live="polite">
                {query
                  ? currentPos >= 0
                    ? t("logs.match_pos", {
                        i: currentPos + 1,
                        n: matches.length,
                      })
                    : t("logs.matches", { n: matches.length })
                  : ""}
              </span>
              <Button
                size="s"
                variant="text"
                onClick={() => jump(-1)}
                disabled={matches.length === 0}
                aria-label={t("logs.match_prev")}
              >
                ◀
              </Button>
              <Button
                size="s"
                variant="text"
                onClick={() => jump(1)}
                disabled={matches.length === 0}
                aria-label={t("logs.match_next")}
              >
                ▶
              </Button>
              <span class="logs-toggle">
                <span class="label" aria-hidden="true">
                  {t("logs.filter")}
                </span>
                <Toggle
                  checked={filterMode}
                  onChange={setFilterMode}
                  label={t("logs.filter_label")}
                />
              </span>
            </div>
          </div>
          <div class="logs-tools-secondary-wrap">
            <Button
              size="s"
              variant="text"
              class="logs-more-btn"
              aria-expanded={more}
              aria-controls="logs-secondary"
              onClick={() => setMore(!more)}
            >
              {t("logs.more")} {more ? "▾" : "▸"}
            </Button>
            <div
              id="logs-secondary"
              class="logs-tools-secondary"
              data-open={String(more)}
            >
              <span class="logs-toggle">
                <span class="label" aria-hidden="true">
                  {t("logs.requests")}
                </span>
                <Toggle
                  checked={requests}
                  onChange={(v) => setParam("requests", v ? null : "0")}
                  label={t("logs.requests_label")}
                />
              </span>
              <span class="logs-toggle">
                <span class="label" aria-hidden="true">
                  {t("logs.timestamps")}
                </span>
                <Toggle
                  checked={timestamps}
                  onChange={setTimestamps}
                  label={t("logs.timestamps")}
                />
              </span>
              <span class="logs-toggle">
                <span class="label" aria-hidden="true">
                  {t("logs.wrap")}
                </span>
                <Toggle
                  checked={wrap}
                  onChange={setWrap}
                  label={t("logs.wrap")}
                />
              </span>
              <Menu
                label={t("logs.download")}
                size="s"
                testId="logs-download"
                items={[
                  {
                    key: "engine",
                    label: t("logs.download.engine"),
                    onSelect: () => download(`${ADMIN}/logs/engine/download`),
                  },
                  {
                    key: "manager",
                    label: t("logs.download.manager"),
                    onSelect: () => download(`${ADMIN}/logs/manager/download`),
                  },
                ]}
              />
              <Button size="s" onClick={() => setConfirmClear(true)}>
                {t("logs.clear")}
              </Button>
            </div>
          </div>
          <Button
            size="s"
            variant={follow ? "solid" : "outline"}
            aria-pressed={follow}
            onClick={() => setFollow(!follow)}
            data-testid="logs-follow"
          >
            {follow ? `● ${t("logs.following")}` : t("logs.follow")}
          </Button>
        </div>
        {Boolean(stream.error) && (
          <LoadError
            thing={t("logs.thing")}
            error={stream.error}
            onRetry={stream.retry}
          />
        )}
        <LiveLog
          rows={rows}
          label={label}
          timestamps={timestamps}
          wrap={wrap}
          query={query}
          current={current}
          matches={matchSet}
          follow={follow}
          onFollowChange={setFollow}
          unseen={Math.max(0, buffer.appended - mark)}
          loading={stream.loading}
          empty={
            stream.loading ? (
              <p class="meta loading-dots">{t("logs.loading")}</p>
            ) : filterMode && query && buffer.rows.length > 0 ? (
              <p class="body">{t("logs.no_match", { q: query })}</p>
            ) : buffer.rows.length > 0 ? (
              <p class="body">{t("logs.none_at_level")}</p>
            ) : (
              <Empty
                title={t("logs.empty.title")}
                action={
                  source === "engine" ? (
                    <Link href="/status" class="btn" data-variant="outline">
                      {t("logs.empty.load")}
                    </Link>
                  ) : undefined
                }
              >
                {source === "engine"
                  ? t("logs.empty.engine")
                  : t("logs.empty.manager")}
              </Empty>
            )
          }
        />
      </section>
      <ConfirmSheet
        open={confirmClear}
        title={t("logs.clear.title")}
        confirmLabel={t("logs.clear.confirm")}
        busyLabel={t("logs.clear.busy")}
        busy={clearing}
        onConfirm={onClear}
        onClose={() => setConfirmClear(false)}
      >
        <ul class="logs-consequences">
          <li>{t("logs.clear.what")}</li>
          <li>{t("logs.clear.session")}</li>
        </ul>
      </ConfirmSheet>
    </>
  );
}
