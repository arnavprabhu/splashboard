import { Link, useLocation } from "wouter-preact";
import { PageHeader, Section } from "../components/Section";
import { useTitle } from "../lib/title";
import { t } from "../strings/en";

/** Unknown route: title, the path, buttons Status and Back. */
export default function NotFound() {
  const [location] = useLocation();
  useTitle(t("notfound.title").replace(/\.$/, ""));
  return (
    <>
      <PageHeader title={t("notfound.title")} />
      <Section>
        <div class="stack">
          <p class="body">
            {t("notfound.body", { path: "" }).replace(/\s*\.$/, "")}{" "}
            <code class="mono">/admin{location}</code>.
          </p>
          <div class="cluster">
            <Link href="/status" class="btn" data-variant="solid">
              {t("nav.status")}
            </Link>
            <button type="button" class="btn" onClick={() => history.back()}>
              {t("common.back")}
            </button>
          </div>
        </div>
      </Section>
    </>
  );
}
