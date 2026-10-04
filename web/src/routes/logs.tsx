import { SubNav } from '../components/SubNav';
import { Placeholder } from './Placeholder';
import { LOGS_TABS } from './tabs';

export default function Page() {
  return (
    <>
      <SubNav items={LOGS_TABS} label="Logs" exact />
      <Placeholder title="Logs." spec="§10.8" />
    </>
  );
}
