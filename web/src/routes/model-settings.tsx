import { modelIdFromParams } from '../lib/model-id';
import { Placeholder } from './Placeholder';

export default function ModelSettingsPage({ params = {} }: { params?: Record<string, string | undefined> }) {
  const id = modelIdFromParams(params);
  return <Placeholder title="Model settings." spec="§10.4, §7.5" meta={<span class="mono">{id}</span>} />;
}
