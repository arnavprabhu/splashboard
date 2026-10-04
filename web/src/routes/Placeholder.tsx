import type { ComponentChildren } from "preact";
import { PageHeader, Section } from "../components/Section";
import { Empty } from "../components/States";

export interface PlaceholderProps {
  title: string;
  spec: string;
  meta?: ComponentChildren;
  children?: ComponentChildren;
}

/** Stand-in for a page that later tracks will build. */
export function Placeholder({ title, spec, meta, children }: PlaceholderProps) {
  return (
    <>
      <PageHeader title={title} meta={meta} />
      {children}
      <Section label="Not built yet" meta={spec}>
        <Empty title="Coming in a later build step">
          This page is specified in SPEC {spec}.
        </Empty>
      </Section>
    </>
  );
}
