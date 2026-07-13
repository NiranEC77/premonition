import { createHmac, timingSafeEqual } from 'node:crypto';

// Stateless session cookie: no server-side session store (Vercel functions
// are stateless across invocations anyway). The cookie IS the proof —
// `expiry.hmac(expiry, EDIT_PASSPHRASE)`. Nobody can forge a valid hmac
// without knowing EDIT_PASSPHRASE, which lives only in Vercel's server-side
// environment (set in the dashboard, never committed — unlike the anon key,
// this one actually is a secret). This module must never be imported from
// anything that runs in the browser.

const COOKIE_NAME = 'premonition_edit_session';
const SESSION_TTL_MS = 1000 * 60 * 60 * 4; // 4 hours

function getPassphrase(): string {
  const p = import.meta.env.EDIT_PASSPHRASE;
  if (!p) throw new Error('EDIT_PASSPHRASE is not set in the server environment');
  return p;
}

function sign(expiry: number): string {
  return createHmac('sha256', getPassphrase()).update(String(expiry)).digest('hex');
}

export function checkPassphrase(candidate: string): boolean {
  const expected = getPassphrase();
  const a = Buffer.from(candidate);
  const b = Buffer.from(expected);
  // Constant-time compare, and only when lengths already match (timingSafeEqual
  // throws on mismatched length — an early return there leaks length, not secret).
  return a.length === b.length && timingSafeEqual(a, b);
}

export function makeSessionCookieValue(): string {
  const expiry = Date.now() + SESSION_TTL_MS;
  return `${expiry}.${sign(expiry)}`;
}

export function isSessionValid(cookieValue: string | undefined): boolean {
  if (!cookieValue) return false;
  const [expiryStr, hmac] = cookieValue.split('.');
  const expiry = Number(expiryStr);
  if (!expiry || !hmac || Date.now() > expiry) return false;
  const expectedHmac = sign(expiry);
  const a = Buffer.from(hmac);
  const b = Buffer.from(expectedHmac);
  return a.length === b.length && timingSafeEqual(a, b);
}

export const SESSION_COOKIE_NAME = COOKIE_NAME;
export const SESSION_MAX_AGE_SECS = SESSION_TTL_MS / 1000;
