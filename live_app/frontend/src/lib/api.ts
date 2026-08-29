/**
 * Thin client for the Flask JSON API (live_app/server.py). Every call
 * goes through the browser's own HTTP Basic Auth prompt (handled by the
 * browser once, not by this code) -- no ChatGPT auth, no client-side
 * token handling needed here at all.
 */

export type LotType = 'base' | 'o1' | 'o2';

export interface ApiPosition {
  id: number;
  ticker: string;
  lot_type: LotType;
  lot_label: string;
  parent_id: number | null;
  entry_date: string;
  entry_price: number;
  dollar_amount: number;
  shares: number;
  catalyst_date: string;
  sector: string | null;
  status: 'open' | 'closed';
  exit_date: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  merged_into_base: boolean;
  last_known_price: number | null;
  last_checked_at: string | null;
}

export interface AvailableBasePosition {
  id: number;
  ticker: string;
  entry_date: string;
  catalyst_date: string;
}

export interface PositionsResponse {
  open: ApiPosition[];
  closed: ApiPosition[];
  available_base_positions: AvailableBasePosition[];
}

export interface HistoryPoint { date: string; price: number }
export interface HistoryEvent { date: string; label: string; kind: string }
export interface PositionHistory { points: HistoryPoint[]; events: HistoryEvent[] }

export interface Candidate { ticker: string; catalyst_date: string; days_until: number }

export interface ActivityCard {
  ticker: string | null;
  action: string;
  label: string;
  category: 'info' | 'warning' | 'opportunity' | 'error';
  detail: Record<string, unknown>;
  time: string;
}

export interface Settings {
  strategies: { base_enabled: boolean; idle_sweep_enabled: boolean; dip_enabled: boolean; spike_enabled: boolean };
  universe: { min_volatility_pct: number; max_market_cap_b: number; min_dollar_volume_m: number; earnings_horizon_days: number };
  base: {
    entry_lead_days: number; base_allocation_pct: number; sector_limit: number; trailing_stop_pct: number;
    stagnation_window_days: number; trend_threshold_pct: number; trend_window_sessions: number;
  };
  idle: { sweep_ticker: string; min_holding_days: number };
  dip: { check_delay_days: number; threshold_pct: number; allocation_pct: number; holding_sessions: number };
  spike: { threshold_pct: number; allocation_pct: number; exit_lead_hours: number };
  not_wired: Record<string, string>;
}

export interface CashAdjustment { id: number; direction: 'deposit' | 'withdrawal'; amount: number; note: string | null; effective_at: string }
export interface CashResponse { balance: number; adjustments: CashAdjustment[] }

export interface IdleSweepState { ticker: string; shares: number; idle_days_counter: number; last_checked_date: string | null }
export interface IdleSweepEvent { run_date: string; action: 'buy' | 'sell'; ticker: string; shares: number; price: number; amount: number; fees: number }

export interface PortfolioSnapshot { snapshot_date: string; cash_balance: number; positions_value: number; sweep_value: number; total_value: number }

export interface ImportProposal {
  kind: 'new_position' | 'skip';
  ticker: string;
  default_include: boolean;
  reason: string;
  entry_date?: string;
  entry_price?: number;
  dollar_amount?: number;
  shares?: number;
  row_index?: number;
}

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'content-type': 'application/json', ...(init?.headers || {}) },
  });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError((body && (body as { error?: string }).error) || response.statusText, response.status);
  }
  return body as T;
}

export const api = {
  positions: () => call<PositionsResponse>('/api/positions'),
  positionHistory: (id: number) => call<PositionHistory>(`/api/positions/${id}/history`),
  addPosition: (body: { ticker: string; entry_date: string; entry_price: number; dollar_amount: number; catalyst_date: string; sector?: string }) =>
    call<{ position: ApiPosition }>('/api/positions', { method: 'POST', body: JSON.stringify(body) }),
  addAddonPosition: (body: { parent_id: number; lot_type: 'o1' | 'o2'; entry_date: string; entry_price: number; dollar_amount: number }) =>
    call<{ position: ApiPosition }>('/api/positions/addon', { method: 'POST', body: JSON.stringify(body) }),
  updatePosition: (id: number, body: { shares?: number; entry_price?: number; reopen?: boolean }) =>
    call<{ position: ApiPosition }>(`/api/positions/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  closePosition: (id: number, body: { exit_price?: string; exit_date?: string; reason?: string }) =>
    call<{ ok: true }>(`/api/positions/${id}/close`, { method: 'POST', body: JSON.stringify(body) }),
  removePosition: (id: number) => call<{ ok: true }>(`/api/positions/${id}/remove`, { method: 'POST' }),

  candidates: () => call<{ candidates: Candidate[] }>('/api/candidates'),
  activity: (limit = 50) => call<{ activity: ActivityCard[] }>(`/api/activity?limit=${limit}`),

  settings: () => call<Settings>('/api/settings'),
  saveSettings: (patch: Record<string, unknown>) => call<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) }),

  cash: () => call<CashResponse>('/api/cash'),
  adjustCash: (direction: 'deposit' | 'withdrawal', amount: number, note?: string) =>
    call<{ balance: number }>('/api/cash', { method: 'POST', body: JSON.stringify({ direction, amount, note }) }),

  idleSweep: () => call<{ state: IdleSweepState; events: IdleSweepEvent[] }>('/api/idle-sweep'),
  sellIdleSweep: () => call<{ ticker: string; shares: number; price: number; amount: number }>('/api/idle-sweep/sell', { method: 'POST' }),

  snapshots: () => call<{ snapshots: PortfolioSnapshot[] }>('/api/snapshots'),

  importPreview: (source: 'wealthsimple' | 'yahoo', fileName: string, csvText: string) =>
    call<{ import_id: number; row_count: number; proposals: ImportProposal[] }>('/api/import/preview', {
      method: 'POST',
      body: JSON.stringify({ source, file_name: fileName, csv_text: csvText }),
    }),
  importApply: (importId: number, decisions: ImportProposal[]) =>
    call<{ applied: { ticker: string; position_id: number }[] }>('/api/import/apply', {
      method: 'POST',
      body: JSON.stringify({ import_id: importId, decisions }),
    }),

  backupExport: () => call<Record<string, unknown>>('/api/backup/export'),
  backupImport: (bundle: unknown) => call<{ ok: true; restored_from: string | null }>('/api/backup/import', { method: 'POST', body: JSON.stringify(bundle) }),

  refresh: () => call<Record<string, unknown>>('/api/refresh', { method: 'POST' }),
};

export { ApiError };
