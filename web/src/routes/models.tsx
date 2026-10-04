import { SubNav } from '../components/SubNav';
import { Placeholder } from './Placeholder';
import { MODELS_TABS } from './tabs';

export default function Page() {
  return (
    <>
      <SubNav items={MODELS_TABS} label="Models" exact />
      <Placeholder title="Models." spec="§10.4, §9.5" />
    </>
  );
}
