import { fetchAuthSession } from 'aws-amplify/auth';

export interface AppConfig {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  apiBase: string;
}

let config: AppConfig;
export function setConfig(c: AppConfig) {
  config = c;
}
export function getConfig() {
  return config;
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public code = '') {
    super(message);
  }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; auth?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (opts.auth !== false) {
    const session = await fetchAuthSession();
    const token = session.tokens?.idToken?.toString();
    if (token) headers.authorization = `Bearer ${token}`;
  }
  const res = await fetch(`${config.apiBase}${path}`, {
    method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'),
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new ApiError(res.status, data.error || res.statusText, data.code);
  return data as T;
}

// ------------------------------------------------------------------ types
export interface User {
  sub: string;
  email: string;
  role: 'user' | 'superadmin';
  status: 'pending' | 'active' | 'banned';
  credits: number;
  unlimited: boolean;
  createdAt: string;
  note?: string;
}

export interface Pack { id: string; name: string; credits: number; amount: number }

export interface PublicSettings {
  siteName: string;
  accessMode: 'invite' | 'approval' | 'open';
  billingEnabled: boolean;
  enabledEngines: string[];
  defaultEngine: string;
  prices: Record<string, number>;
  packs: Pack[];
  currency: string;
  razorpayKeyId: string;
  maxFileBytes: number;
}

export interface Job {
  jobId: string;
  name: string;
  engine: string;
  outputs: string[];
  status: 'created' | 'processing' | 'finalizing' | 'done' | 'failed' | 'deleted';
  docsTotal: number;
  docsDone: number;
  docsFailed: number;
  docsSkipped: number;
  pagesTotal: number;
  pagesDone: number;
  creditsCharged: number;
  createdAt: string;
  submittedAt?: string;
  finishedAt?: string;
  hasResult: boolean;
  resultBytes?: number;
  language?: string;
  lastError?: string;
  owner: string;
}

export interface Doc {
  docId: string;
  relPath: string;
  kind: string;
  size: number;
  status: string;
  pages: number;
  unitsTotal: number;
  unitsDone: number;
  unitsFailed: number;
  error?: string;
  outputs?: Record<string, string>;
}

// ------------------------------------------------------------------ formatting
export function bytes(n = 0) {
  if (n < 1024) return `${n} B`;
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
}

export function money(paise: number, currency = 'INR') {
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency }).format(paise / 100);
}

export function when(iso?: string) {
  if (!iso) return '';
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
