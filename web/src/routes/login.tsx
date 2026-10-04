import { useState } from 'preact/hooks';
import { Redirect, useLocation, useSearch } from 'wouter-preact';
import { api, ApiError } from '../api/client';
import { Button } from '../components/Button';
import { Field } from '../components/Field';
import { TextInput } from '../components/inputs';
import { PageHeader, Section } from '../components/Section';
import { auth, authKnown, refreshAll, refreshAuth, safeNext, setAuth } from '../store';

export function loginErrorMessage(err: unknown): string {
  if (!(err instanceof ApiError)) return 'Could not sign in.';
  if (err.status === 401) return "That key didn't match.";
  if (err.status === 429) return 'Too many attempts. Try again in a moment.';
  if (err.status === 0) return 'Splash GUI is not answering. Is it running?';
  return err.message;
}

export default function LoginPage() {
  const [key, setKey] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [, navigate] = useLocation();
  const next = safeNext(new URLSearchParams(useSearch()).get('next'));

  if (authKnown.value && (!auth.value.admin_requires_key || auth.value.authenticated)) {
    return <Redirect to={next} replace />;
  }

  const submit = async (e: Event) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const state = await api.post<unknown>('/auth/login', { key });
      if (state) setAuth(state);
      else await refreshAuth();
      refreshAll();
      navigate(next, { replace: true });
    } catch (err) {
      setError(loginErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageHeader title="Sign in." />
      <Section label="Admin key" meta="SPEC §17.1, D5">
        <form onSubmit={submit} class="stack">
          <Field label="API key" help="The key set in Settings → Security on the Mac that runs Splash GUI." error={error}>
            {({ id, describedBy }) => (
              <TextInput
                id={id}
                aria-describedby={describedBy}
                type="password"
                autocomplete="current-password"
                value={key}
                onChange={setKey}
                invalid={Boolean(error)}
              />
            )}
          </Field>
          <div>
            <Button type="submit" variant="solid" disabled={busy || !key}>
              {busy ? 'Checking…' : 'Sign in'}
            </Button>
          </div>
        </form>
      </Section>
    </>
  );
}
