import { SubNav } from '../components/SubNav';
import { Placeholder } from './Placeholder';
import { STATUS_TABS } from './tabs';

export default function Page() {
  return (
    <>
      <SubNav items={STATUS_TABS} label="Status" exact />
      <Placeholder title="Usage." spec="§10.3, §16.2" />
    </>
  );
}
