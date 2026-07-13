import type { APIRoute } from 'astro';
import { isSessionValid, SESSION_COOKIE_NAME } from '../../lib/session';
import { getServiceClient } from '../../lib/supabaseServiceOnly';

export const prerender = false;

type Action = 'add' | 'remove' | 'fix';

interface RequestBody {
  action: Action;
  ticker: string;       // the ticker being removed / fixed away from
  newTicker?: string;   // for 'add' and 'fix': the ticker being added
  note?: string;
}

const TICKER_RE = /^[A-Z]{1,6}(\.[A-Z])?$/;

async function validateTickerLive(symbol: string): Promise<{ ok: boolean; reason?: string }> {
  const apiKey = import.meta.env.FINNHUB_API_KEY;
  if (!TICKER_RE.test(symbol)) {
    return { ok: false, reason: `"${symbol}" doesn't look like a stock ticker (letters only, 1-6 characters).` };
  }
  if (!apiKey) {
    return { ok: false, reason: 'Live symbol lookup is not configured on the server right now.' };
  }
  try {
    const resp = await fetch(
      `https://finnhub.io/api/v1/quote?symbol=${encodeURIComponent(symbol)}&token=${apiKey}`,
      { signal: AbortSignal.timeout(8000) }
    );
    if (!resp.ok) {
      return { ok: false, reason: `Couldn't check "${symbol}" right now (lookup service returned an error).` };
    }
    const data = await resp.json();
    if (!data.c) {
      return { ok: false, reason: `"${symbol}" doesn't look like a real, currently-traded ticker — no price data found for it.` };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, reason: `Couldn't check "${symbol}" right now (${e instanceof Error ? e.message : 'lookup failed'}).` };
  }
}

export const POST: APIRoute = async ({ request, cookies }) => {
  const session = cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!isSessionValid(session)) {
    return new Response(JSON.stringify({ error: 'not authenticated' }), { status: 401 });
  }

  let body: RequestBody;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: 'invalid request body' }), { status: 400 });
  }

  const today = new Date().toISOString().slice(0, 10);
  const supabase = getServiceClient();

  // Every branch below only ever INSERTs into watchlist_events — never an
  // update, never a delete. That log is the only thing standing between this
  // project and survivorship bias in the backtest (schema.sql's own comment
  // on the table says so, and this is where that promise actually gets kept
  // or broken).
  if (body.action === 'remove') {
    const ticker = (body.ticker || '').toUpperCase();
    if (!ticker) return new Response(JSON.stringify({ error: 'ticker is required' }), { status: 400 });

    const { error } = await supabase.from('watchlist_events').insert({
      ticker, action: 'remove', effective_date: today,
      note: body.note || 'removed via /health',
    });
    if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500 });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }

  if (body.action === 'add' || body.action === 'fix') {
    const newTicker = (body.newTicker || '').toUpperCase();
    if (!newTicker) return new Response(JSON.stringify({ error: 'newTicker is required' }), { status: 400 });

    const validation = await validateTickerLive(newTicker);
    if (!validation.ok) {
      return new Response(JSON.stringify({ error: validation.reason }), { status: 422 });
    }

    if (body.action === 'fix') {
      const oldTicker = (body.ticker || '').toUpperCase();
      if (!oldTicker) return new Response(JSON.stringify({ error: 'ticker is required for a fix' }), { status: 400 });

      const { error: removeError } = await supabase.from('watchlist_events').insert({
        ticker: oldTicker, action: 'remove', effective_date: today,
        note: body.note || `fixed to ${newTicker} via /health`,
      });
      if (removeError) return new Response(JSON.stringify({ error: removeError.message }), { status: 500 });
    }

    const { error: addError } = await supabase.from('watchlist_events').insert({
      ticker: newTicker, action: 'add', effective_date: today,
      note: body.note || (body.action === 'fix' ? `replacing ${body.ticker}` : 'added via /health'),
    });
    if (addError) return new Response(JSON.stringify({ error: addError.message }), { status: 500 });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }

  return new Response(JSON.stringify({ error: `unknown action "${body.action}"` }), { status: 400 });
};
