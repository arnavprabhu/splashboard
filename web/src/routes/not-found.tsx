import { Link } from 'wouter-preact';
import { PageHeader, Section } from '../components/Section';

export default function NotFound() {
  return (
    <>
      <PageHeader title="Not found." />
      <Section label="Where to">
        <Link href="/status" class="lead">
          Back to Status <span class="acc">→</span>
        </Link>
      </Section>
    </>
  );
}
