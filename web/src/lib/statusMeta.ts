// Status vocabularies for the two badge families on this dashboard. Colors are
// assigned by ROLE (good/warning/serious/critical from the status palette, or a
// neutral "tone" for the freshness page's live/frozen distinction, which is not
// a good/bad judgment — see the comment on FIELD_STATUS_META). Every entry pairs
// an icon glyph with a label; color is never the only signal, per the dataviz
// skill's "status colors ship with an icon + label, never color alone" rule.

export type StatusRole = 'good' | 'warning' | 'serious' | 'critical';

export interface HealthStatusInfo {
  label: string;
  role: StatusRole;
  icon: string;
}

// Matches ticker_health's check constraint in supabase/schema.sql.
export const HEALTH_STATUS_META: Record<string, HealthStatusInfo> = {
  ok: { label: 'OK', role: 'good', icon: '●' }, // ●
  degraded: { label: 'Degraded', role: 'warning', icon: '▲' }, // ▲
  insufficient_history: { label: 'Insufficient history', role: 'warning', icon: '▲' },
  unresolved: { label: 'Unresolved', role: 'serious', icon: '?' },
  excluded_tradability: { label: 'Excluded — tradability gate', role: 'serious', icon: '⛔' }, // ⛔
  no_data: { label: 'No data', role: 'critical', icon: '✕' }, // ✕
  restricted: { label: 'Restricted', role: 'critical', icon: '✕' },
};

export function healthStatusInfo(status: string): HealthStatusInfo {
  return HEALTH_STATUS_META[status] ?? { label: status, role: 'warning', icon: '?' };
}

export type FieldTone = 'live' | 'frozen' | 'gap';

export interface FieldStatusInfo {
  label: string;
  tone: FieldTone;
  icon: string;
}

// Matches probe_field_behavior's check constraint. NOTE: unlike health status,
// "updates_after_close" is not universally good or bad — it's exactly what you
// want from postMarketPrice and exactly what you don't want from
// regularMarketPrice. So these use neutral categorical tones (live/frozen), not
// the good/warning/serious/critical status palette — the page copy next to each
// field supplies the judgment, the badge just states the observed fact.
export const FIELD_STATUS_META: Record<string, FieldStatusInfo> = {
  updates_after_close: { label: 'Live — still updating', tone: 'live', icon: '↻' }, // ↻
  freezes_after_close: { label: 'Frozen after close', tone: 'frozen', icon: '❄' }, // ❄
  insufficient_data: { label: 'Not enough data yet', tone: 'gap', icon: '…' }, // …
};

export function fieldStatusInfo(status: string): FieldStatusInfo {
  return FIELD_STATUS_META[status] ?? { label: status, tone: 'gap', icon: '?' };
}
