import { createClient, type SupabaseClient } from '@supabase/supabase-js';

// Server-side only — this module runs inside Astro's SSR request handler, never
// in a client bundle. Read with the anon key; RLS (see supabase/schema.sql and
// supabase/migrations/) is what actually keeps this safe to hold at all. The
// service_role key never appears anywhere in this directory — writes come
// exclusively from the agents laptop, via publish/supabase_rest.py.
const url = import.meta.env.SUPABASE_URL;
const anonKey = import.meta.env.SUPABASE_ANON_KEY;

export const supabaseConfigured = Boolean(
  url && anonKey && url !== 'https://supabase.com'
);

export const supabase: SupabaseClient | null = supabaseConfigured
  ? createClient(url, anonKey, { auth: { persistSession: false } })
  : null;

export const supabaseConfigError = supabaseConfigured
  ? null
  : 'Supabase is not configured for this deployment (SUPABASE_URL / SUPABASE_ANON_KEY missing or invalid).';
