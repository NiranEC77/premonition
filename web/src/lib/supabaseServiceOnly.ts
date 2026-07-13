import { createClient, type SupabaseClient } from '@supabase/supabase-js';

// ⚠️ SERVICE_ROLE KEY — bypasses RLS entirely. This module may ONLY be
// imported from files under src/pages/api/ (server-only Astro API routes).
// Never import this from a .astro page's frontmatter that renders in a
// request that could be prerendered/cached, and never from any client
// script. src/lib/supabase.ts (anon key, read-only) is what every page uses.
//
// SUPABASE_SERVICE_ROLE_KEY is set in the Vercel dashboard's environment
// variables, never committed — unlike web/.env's anon key, this one is a
// real secret and must never appear in the repo.
const url = import.meta.env.SUPABASE_URL;
const serviceRoleKey = import.meta.env.SUPABASE_SERVICE_ROLE_KEY;

export function getServiceClient(): SupabaseClient {
  if (!url || !serviceRoleKey) {
    throw new Error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set in the server environment');
  }
  return createClient(url, serviceRoleKey, { auth: { persistSession: false } });
}
