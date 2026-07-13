import { defineConfig } from 'astro/config';
import vercel from '@astrojs/vercel';

// Server-rendered: every page reads Supabase at request time with the anon
// key, server-side only (never bundled into client JS). See src/lib/supabase.ts.
export default defineConfig({
  output: 'server',
  adapter: vercel(),
});
