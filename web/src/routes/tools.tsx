import type { ComponentChildren } from "preact";
import { SubNav, TOOLS_TABS } from "../components/SubNav";
import { PageHeader } from "../components";
export type ToolName = "playground" | "tokenizer" | "judgments" | "benchmark";
export function ToolPage({
  tool,
  children,
}: {
  tool: ToolName;
  children?: ComponentChildren;
}) {
  return (
    <>
      <SubNav items={TOOLS_TABS} label="Tools" />
      <PageHeader title={`${tool[0]!.toUpperCase()}${tool.slice(1)}.`} />
      {children}
    </>
  );
}
