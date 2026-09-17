import type { Config } from './types';

let configuration: Config | null = null;
let pendingConfiguration: Promise<Config> | null = null;

export class ApiError extends Error {
  constructor(public code: string, message: string) { super(message); }
}

async function decode<T>(response: Response): Promise<T> {
  let payload;
  try { payload = await response.json(); }
  catch { throw new ApiError('CONNECTION_ERROR', 'We could not connect to the service. Please try again.'); }
  if (!response.ok) throw new ApiError(payload.error?.code ?? 'REQUEST_FAILED', payload.error?.message ?? 'Something went wrong. Please try again.');
  return payload as T;
}

export async function getConfig(refresh = false): Promise<Config> {
  if (!refresh && configuration) return configuration;
  if (pendingConfiguration) return pendingConfiguration;
  pendingConfiguration = fetch('/api/config', { cache: 'no-store', signal: AbortSignal.timeout(15000) })
    .then(decode<Config>)
    .then(value => { configuration = value; return value; })
    .finally(() => { pendingConfiguration = null; });
  return pendingConfiguration;
}

export async function post<T>(path: 'geocode' | 'grade' | 'assess' | 'contact', body: unknown, retry = true): Promise<T> {
  const config = await getConfig();
  try {
    return await decode<T>(await fetch(`/api/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Rolloff-Token': config.formToken },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(path === 'contact' ? 45000 : 20000),
    }));
  } catch (error) {
    if (error instanceof ApiError && error.code === 'TOKEN_EXPIRED' && retry) {
      await getConfig(true);
      return post<T>(path, body, false);
    }
    throw error;
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')) return 'This is taking longer than expected. Your details are still here; please try again.';
  return 'We could not connect. Check your connection and try again. Your details are still here.';
}
