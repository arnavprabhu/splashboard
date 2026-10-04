import { SubNav, TOOLS_TABS } from '../components/SubNav';
import { Placeholder } from './Placeholder';

const TOOLS = {
  playground: { title: 'Playground.', spec: '§10.6 Playground' },
  tokenizer: { title: 'Tokenizer.', spec: '§10.6 Tokenizer & template' },
  judgments: { title: 'Judgments.', spec: '§10.6 Judgments' },
  benchmark: { title: 'Benchmark.', spec: '§10.6 Benchmark' },
} as const;

export type ToolName = keyof typeof TOOLS;

export function ToolPage({ tool }: { tool: ToolName }) {
  const t = TOOLS[tool];
  return (
    <>
      <SubNav items={TOOLS_TABS} label="Tools" />
      <Placeholder title={t.title} spec={t.spec} />
    </>
  );
}
