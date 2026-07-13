import type { APIRoute } from 'astro';
import { checkPassphrase, makeSessionCookieValue, SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECS } from '../../lib/session';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies }) => {
  let body: { passphrase?: string };
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: 'invalid request body' }), { status: 400 });
  }

  if (!body.passphrase || !checkPassphrase(body.passphrase)) {
    // Same error for "wrong passphrase" and "missing passphrase" — no hint
    // about which one, and no distinction in timing (checkPassphrase is
    // constant-time on the compare itself).
    return new Response(JSON.stringify({ error: 'incorrect passphrase' }), { status: 401 });
  }

  cookies.set(SESSION_COOKIE_NAME, makeSessionCookieValue(), {
    httpOnly: true,
    secure: true,
    sameSite: 'strict',
    path: '/',
    maxAge: SESSION_MAX_AGE_SECS,
  });

  return new Response(JSON.stringify({ ok: true }), { status: 200 });
};

export const DELETE: APIRoute = async ({ cookies }) => {
  cookies.delete(SESSION_COOKIE_NAME, { path: '/' });
  return new Response(JSON.stringify({ ok: true }), { status: 200 });
};
