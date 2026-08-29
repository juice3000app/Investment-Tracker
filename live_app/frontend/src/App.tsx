import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  AlertTriangle, ArrowDownRight, ArrowUpRight, Bell, CalendarDays, CheckCircle2, ChevronRight,
  CircleDollarSign, Database, Gauge, LayoutDashboard, Minus, Newspaper, Plus, Settings2,
  SlidersHorizontal, Sparkles, Trash2, UploadCloud, WalletCards, XCircle,
} from 'lucide-react';
import { Area, AreaChart, CartesianGrid, Line, LineChart, ReferenceLine, XAxis, YAxis } from 'recharts';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ChartContainer, ChartTooltip, ChartTooltipContent } from '@/components/ui/chart';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Field, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { Slider } from '@/components/ui/slider';
import { Switch } from '@/components/ui/switch';
import { api, ApiError, type ApiPosition, type Candidate, type ActivityCard, type Settings, type PortfolioSnapshot, type ImportProposal, type PositionHistory } from '@/lib/api';

type ViewName = 'overview' | 'signals' | 'portfolio' | 'activity' | 'strategies';

const money = (value: number) =>
  value.toLocaleString('en-CA', { style: 'currency', currency: 'CAD' });

const positionValue = (p: ApiPosition) => p.shares * (p.status === 'closed' ? p.exit_price ?? p.last_known_price ?? p.entry_price : p.last_known_price ?? p.entry_price);
const positionChange = (p: ApiPosition) => {
  const current = p.status === 'closed' ? p.exit_price ?? p.last_known_price ?? p.entry_price : p.last_known_price ?? p.entry_price;
  return ((current / p.entry_price) - 1) * 100;
};

const STRATEGY_META: Array<{ key: keyof Settings['strategies']; name: string }> = [
  { key: 'base_enabled', name: 'Base earnings' },
  { key: 'idle_sweep_enabled', name: 'Idle cash' },
  { key: 'dip_enabled', name: 'Buy the Dip' },
  { key: 'spike_enabled', name: 'Sell the Spike' },
];

function strategyDetail(key: keyof Settings['strategies'], s: Settings): string {
  switch (key) {
    case 'base_enabled':
      return `${s.base.base_allocation_pct}% target · ${s.base.entry_lead_days} day before`;
    case 'idle_sweep_enabled':
      return `${s.idle.sweep_ticker} sweep · after ${s.idle.min_holding_days}d idle`;
    case 'dip_enabled':
      return `${s.dip.threshold_pct}% drop · ${s.dip.allocation_pct}% add-on`;
    case 'spike_enabled':
      return `${s.spike.threshold_pct}% rise · ${s.spike.allocation_pct}% add-on`;
  }
}

type ParamField = { group: 'universe' | 'base' | 'idle' | 'dip' | 'spike'; key: string; label: string; min: number; max: number; step?: number; format: (v: number) => string; notWiredKey?: string };
const pct = (v: number) => `${v}%`;

const PARAM_FIELDS: ParamField[] = [
  { group: 'universe', key: 'min_volatility_pct', label: 'Minimum volatility', min: 0, max: 150, format: pct },
  { group: 'universe', key: 'max_market_cap_b', label: 'Maximum market cap', min: 0.1, max: 10, step: 0.1, format: (v) => `$${v.toFixed(1)}B` },
  { group: 'universe', key: 'min_dollar_volume_m', label: 'Minimum dollar volume', min: 0.1, max: 20, step: 0.1, format: (v) => `$${v.toFixed(1)}M` },
  { group: 'universe', key: 'earnings_horizon_days', label: 'Earnings horizon', min: 1, max: 120, format: (v) => `${v} days` },
  { group: 'base', key: 'entry_lead_days', label: 'Entry lead time', min: 0, max: 10, format: (v) => `${v} trading day${v === 1 ? '' : 's'}`, notWiredKey: 'base.entry_lead_days' },
  { group: 'base', key: 'base_allocation_pct', label: 'Base allocation', min: 1, max: 100, format: pct },
  { group: 'base', key: 'sector_limit', label: 'Sector limit', min: 1, max: 10, format: (v) => `${v} stocks` },
  { group: 'base', key: 'trailing_stop_pct', label: 'Trailing stop', min: 1, max: 80, format: pct },
  { group: 'base', key: 'stagnation_window_days', label: 'Stagnation window', min: 1, max: 120, format: (v) => `${v} days` },
  { group: 'base', key: 'trend_threshold_pct', label: 'Trend threshold', min: 0, max: 50, format: pct },
  { group: 'base', key: 'trend_window_sessions', label: 'Trend window', min: 1, max: 30, format: (v) => `${v} sessions` },
  { group: 'dip', key: 'check_delay_days', label: 'Dip check delay', min: 1, max: 10, format: (v) => `${v} trading day${v === 1 ? '' : 's'}`, notWiredKey: 'dip.check_delay_days' },
  { group: 'dip', key: 'threshold_pct', label: 'Dip threshold', min: 0, max: 30, step: 0.5, format: pct },
  { group: 'dip', key: 'allocation_pct', label: 'Dip allocation', min: 1, max: 50, format: pct },
  { group: 'dip', key: 'holding_sessions', label: 'Dip holding period', min: 1, max: 60, format: (v) => `${v} sessions` },
  { group: 'spike', key: 'threshold_pct', label: 'Spike threshold', min: 0, max: 30, step: 0.5, format: pct },
  { group: 'spike', key: 'allocation_pct', label: 'Spike allocation', min: 1, max: 50, format: pct },
  { group: 'spike', key: 'exit_lead_hours', label: 'Exit lead time', min: 0.25, max: 4, step: 0.25, format: (v) => `${v} hr before close`, notWiredKey: 'spike.exit_lead_hours' },
];

const GROUP_META: Record<ParamField['group'], { label: string; tone: string; badge: string }> = {
  universe: { label: 'Universe filters', tone: 'border-sky-200 bg-sky-50/70', badge: 'bg-sky-100 text-sky-800' },
  base: { label: 'Base earnings', tone: 'border-teal-200 bg-teal-50/70', badge: 'bg-teal-100 text-teal-800' },
  idle: { label: 'Idle cash', tone: 'border-violet-200 bg-violet-50/70', badge: 'bg-violet-100 text-violet-800' },
  dip: { label: 'Buy the Dip', tone: 'border-amber-200 bg-amber-50/70', badge: 'bg-amber-100 text-amber-800' },
  spike: { label: 'Sell the Spike', tone: 'border-rose-200 bg-rose-50/70', badge: 'bg-rose-100 text-rose-800' },
};

function PerformanceChart({ data }: { data: PortfolioSnapshot[] }) {
  const points = data.map((s) => ({ date: s.snapshot_date, value: s.total_value }));
  if (points.length === 0) {
    return <p className="py-8 text-center text-sm text-muted-foreground">No performance history yet -- it fills in as the daily job (or a manual refresh) runs.</p>;
  }
  return (
    <ChartContainer config={{ value: { label: 'Portfolio value', color: 'oklch(0.55 0.13 190)' } }} className="h-[240px] w-full aspect-auto">
      <AreaChart data={points} margin={{ left: 2, right: 12, top: 12, bottom: 0 }}>
        <defs><linearGradient id="portfolioFill" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="var(--color-value)" stopOpacity={0.3} /><stop offset="95%" stopColor="var(--color-value)" stopOpacity={0.02} /></linearGradient></defs>
        <CartesianGrid vertical={false} /><XAxis dataKey="date" tickLine={false} axisLine={false} /><YAxis hide domain={['dataMin - 1000', 'dataMax + 1000']} />
        <ChartTooltip content={<ChartTooltipContent formatter={(value) => <span className="font-mono font-semibold">{money(Number(value))}</span>} />} />
        <Area type="monotone" dataKey="value" stroke="var(--color-value)" strokeWidth={2.5} fill="url(#portfolioFill)" />
      </AreaChart>
    </ChartContainer>
  );
}

const EVENT_COLORS: Record<string, string> = {
  entry: '#0f8f83', catalyst: '#0284c7', stop_loss: '#dc2626', stagnation: '#d97706',
  merged_into_base: '#7c3aed', manual: '#64748b', exit: '#64748b',
};

function PositionChart({ history }: { history: PositionHistory }) {
  const data = history.points;
  return (
    <ChartContainer config={{ price: { label: 'Close', color: 'oklch(0.49 0.13 202)' } }} className="h-[270px] w-full aspect-auto">
      <LineChart data={data} margin={{ left: 0, right: 18, top: 24, bottom: 4 }}>
        <CartesianGrid vertical={false} /><XAxis dataKey="date" tickLine={false} axisLine={false} />
        <YAxis domain={['dataMin - 2', 'dataMax + 2']} tickFormatter={(v) => `$${v}`} width={42} />
        <ChartTooltip content={<ChartTooltipContent formatter={(value) => <span className="font-mono font-semibold">{money(Number(value))}</span>} />} />
        {history.events.map((event) => (
          <ReferenceLine key={event.date + event.label} x={event.date} stroke={EVENT_COLORS[event.kind] || '#64748b'} strokeDasharray="4 4"
            label={{ value: event.label, position: 'insideTopRight', fill: EVENT_COLORS[event.kind] || '#64748b', fontSize: 10 }} />
        ))}
        <Line type="monotone" dataKey="price" stroke="var(--color-price)" strokeWidth={2.5} dot={false} activeDot={{ r: 5 }} />
      </LineChart>
    </ChartContainer>
  );
}

function SimpleView({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <div className="mx-auto max-w-[1180px] space-y-5 px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
      <div>
        <p className="mb-1 text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">Signal Ledger</p>
        <h1 className="text-3xl font-bold">{title}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
      </div>
      {children}
    </div>
  );
}

function PositionList({ title, description, positions, onSelect, empty }: { title: string; description: string; positions: ApiPosition[]; onSelect: (p: ApiPosition) => void; empty?: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
        <CardAction><Badge variant="secondary">{positions.length}</Badge></CardAction>
      </CardHeader>
      <CardContent className="divide-y divide-border px-0">
        {positions.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">{empty ?? 'No positions.'}</p>
        ) : (
          positions.map((p) => {
            const change = positionChange(p);
            return (
              <button type="button" key={p.id} onClick={() => onSelect(p)}
                className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition-colors hover:bg-secondary/60 focus-visible:bg-secondary focus-visible:outline-none">
                <div className="grid size-10 place-items-center rounded-xl bg-secondary text-[10px] font-bold">{p.ticker.replace('.TO', '')}</div>
                <div className="min-w-0 flex-1">
                  <p className="font-semibold">{p.ticker} {p.lot_type !== 'base' && <Badge variant="outline" className="ml-1 align-middle text-[10px]">{p.lot_label}</Badge>}</p>
                  <p className="truncate text-xs text-muted-foreground">{p.sector || '—'} · {p.status === 'open' ? `since ${p.entry_date}` : `closed ${p.exit_date}`}</p>
                </div>
                <div className="text-right">
                  <p className="font-semibold tabular-nums">{money(positionValue(p))}</p>
                  <p className={`inline-flex items-center text-xs font-semibold ${change >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                    {change >= 0 ? <ArrowUpRight className="size-3" /> : <ArrowDownRight className="size-3" />}{change >= 0 ? '+' : ''}{change.toFixed(1)}%
                  </p>
                </div>
                <ChevronRight className="size-4 text-muted-foreground" />
              </button>
            );
          })
        )}
      </CardContent>
    </Card>
  );
}

export default function App() {
  const [activeView, setActiveView] = useState<ViewName>('overview');
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState('');

  const [positions, setPositions] = useState<ApiPosition[]>([]);
  const [availableBase, setAvailableBase] = useState<{ id: number; ticker: string; entry_date: string; catalyst_date: string }[]>([]);
  const [cashBalance, setCashBalance] = useState(0);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [activity, setActivity] = useState<ActivityCard[]>([]);
  const [snapshots, setSnapshots] = useState<PortfolioSnapshot[]>([]);
  const [idleSweep, setIdleSweep] = useState<{ ticker: string; shares: number } | null>(null);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [paramDraft, setParamDraft] = useState<Settings | null>(null);
  const [cashOpen, setCashOpen] = useState(false);
  const [cashDirection, setCashDirection] = useState<'deposit' | 'withdrawal'>('deposit');
  const [cashAmount, setCashAmount] = useState('1000');
  const [addOpen, setAddOpen] = useState(false);
  const [addPrefill, setAddPrefill] = useState<{ ticker: string; catalyst_date: string } | null>(null);
  const [addonOpen, setAddonOpen] = useState(false);
  const [positionOpen, setPositionOpen] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [selected, setSelected] = useState<ApiPosition | null>(null);
  const [selectedHistory, setSelectedHistory] = useState<PositionHistory | null>(null);
  const [editShares, setEditShares] = useState('');
  const [editEntryPrice, setEditEntryPrice] = useState('');
  const [editExitPrice, setEditExitPrice] = useState('');
  const [editExitDate, setEditExitDate] = useState('');
  const [importOpen, setImportOpen] = useState(false);
  const [importSource, setImportSource] = useState<'wealthsimple' | 'yahoo'>('wealthsimple');
  const [importState, setImportState] = useState<'idle' | 'uploading' | 'preview' | 'error'>('idle');
  const [importMessage, setImportMessage] = useState('');
  const [importId, setImportId] = useState<number | null>(null);
  const [proposals, setProposals] = useState<ImportProposal[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const loadAll = async () => {
    const [pos, s, cand, act, snap, sweep] = await Promise.all([
      api.positions(), api.settings(), api.candidates(), api.activity(30), api.snapshots(), api.idleSweep(),
    ]);
    setPositions([...pos.open, ...pos.closed]);
    setAvailableBase(pos.available_base_positions);
    setSettings(s);
    setCandidates(cand.candidates);
    setActivity(act.activity);
    setSnapshots(snap.snapshots);
    setIdleSweep(sweep.state.shares > 0 ? sweep.state : null);
    const cash = await api.cash();
    setCashBalance(cash.balance);
  };

  useEffect(() => {
    loadAll().catch((e) => setNotice(e instanceof ApiError ? e.message : 'Could not load the dashboard.')).finally(() => setLoading(false));
  }, []);

  const openPositions = positions.filter((p) => p.status === 'open');
  const closedPositions = positions.filter((p) => p.status === 'closed');
  const totalPortfolio = cashBalance + openPositions.reduce((sum, p) => sum + positionValue(p), 0) +
    (idleSweep ? (snapshots.at(-1)?.sweep_value ?? 0) : 0);
  const strategyCapital = openPositions.reduce((sum, p) => sum + positionValue(p), 0);
  const activePct = totalPortfolio ? Math.round((strategyCapital / totalPortfolio) * 100) : 0;

  const withNotice = async (fn: () => Promise<void>, successMessage: string) => {
    try {
      await fn();
      setNotice(successMessage);
      await loadAll();
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : 'Something went wrong.');
    }
  };

  const changeView = (view: ViewName) => { setActiveView(view); window.scrollTo({ top: 0, behavior: 'smooth' }); };

  const showPosition = async (p: ApiPosition) => {
    setSelected(p);
    setEditShares(String(Math.round(p.shares * 10000) / 10000));
    setEditEntryPrice(String(p.entry_price));
    setEditExitPrice(String(p.exit_price ?? p.last_known_price ?? p.entry_price));
    setEditExitDate(p.exit_date ?? new Date().toISOString().slice(0, 10));
    setSelectedHistory(null);
    setPositionOpen(true);
    try {
      setSelectedHistory(await api.positionHistory(p.id));
    } catch {
      setSelectedHistory({ points: [], events: [] });
    }
  };

  const toggleStrategy = (key: keyof Settings['strategies'], checked: boolean) =>
    withNotice(async () => { await api.saveSettings({ strategies: { [key]: checked } }); }, `${STRATEGY_META.find((m) => m.key === key)?.name} turned ${checked ? 'on' : 'off'}.`);

  const openSettingsDialog = () => { setParamDraft(settings); setSettingsOpen(true); };
  const saveParameters = () => withNotice(async () => {
    if (!paramDraft) return;
    const { not_wired, ...patch } = paramDraft;
    void not_wired;
    await api.saveSettings(patch);
  }, 'Parameters saved.').then(() => setSettingsOpen(false));

  const recordCash = () => {
    const amount = Number(cashAmount);
    if (!Number.isFinite(amount) || amount <= 0) { setNotice('Enter a cash amount greater than zero.'); return; }
    withNotice(async () => { await api.adjustCash(cashDirection, amount); }, `${cashDirection === 'deposit' ? 'Deposit' : 'Withdrawal'} of ${money(amount)} recorded.`)
      .then(() => setCashOpen(false));
  };

  const importCsv = async (file: File) => {
    setImportState('uploading');
    try {
      const text = await file.text();
      const result = await api.importPreview(importSource, file.name, text);
      setImportId(result.import_id);
      setProposals(result.proposals.map((p) => ({ ...p })));
      setImportState('preview');
    } catch (e) {
      setImportMessage(e instanceof ApiError ? e.message : 'Import failed.');
      setImportState('error');
    }
  };

  const applyImport = () => withNotice(async () => {
    if (importId == null) return;
    const chosen = proposals.filter((p) => p.kind === 'new_position' && p.default_include);
    const result = await api.importApply(importId, chosen);
    setImportMessage(`${result.applied.length} position${result.applied.length === 1 ? '' : 's'} recorded.`);
  }, 'Import applied.').then(() => { setImportOpen(false); setImportState('idle'); setProposals([]); });

  const runRefresh = async () => {
    setRefreshing(true);
    try {
      await api.refresh();
      await loadAll();
      setNotice('Refreshed with the latest data.');
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : 'Refresh failed.');
    } finally {
      setRefreshing(false);
    }
  };

  const downloadBackup = async () => {
    const bundle = await api.backupExport();
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `signal-ledger-backup-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const restoreBackup = async (file: File) => {
    try {
      const bundle = JSON.parse(await file.text());
      await api.backupImport(bundle);
      await loadAll();
      setNotice('Backup restored.');
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : 'Could not restore that backup file.');
    }
  };

  const navItems: [typeof LayoutDashboard, string, ViewName][] = [
    [LayoutDashboard, 'Overview', 'overview'], [Sparkles, 'Signals', 'signals'],
    [WalletCards, 'Portfolio', 'portfolio'], [SlidersHorizontal, 'Strategies', 'strategies'],
  ];

  if (loading || !settings) {
    return <main className="grid min-h-screen place-items-center text-sm text-muted-foreground">Loading Signal Ledger…</main>;
  }

  return (
    <main className="min-h-screen bg-background pb-24 text-foreground lg:pb-8">
      <header className="sticky top-0 z-30 border-b border-border/70 bg-background/90 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-[1440px] items-center gap-4 px-4 sm:px-6 lg:px-8">
          <div className="flex items-center gap-2.5">
            <span className="grid size-9 place-items-center rounded-xl bg-primary text-primary-foreground"><Gauge className="size-5" /></span>
            <div><p className="font-heading text-sm font-bold">Signal Ledger</p><p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">TSX strategy desk</p></div>
          </div>
          <nav className="ml-8 hidden items-center gap-1 lg:flex" aria-label="Primary navigation">
            {navItems.map(([Icon, label, view]) => (
              <Button key={view} variant={activeView === view ? 'secondary' : 'ghost'} onClick={() => changeView(view)}><Icon />{label}</Button>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={runRefresh} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Refresh now'}</Button>
            <Button aria-label="Activity" variant="ghost" size="icon" className="relative" onClick={() => changeView('activity')}>
              <Bell />{activity.length > 0 && <span className="absolute right-1.5 top-1.5 size-2 rounded-full bg-amber-500 ring-2 ring-background" />}
            </Button>
            <div className="grid size-8 place-items-center rounded-full bg-foreground text-xs font-bold text-background">MM</div>
          </div>
        </div>
      </header>

      {notice && (
        <div className="mx-auto max-w-[1440px] px-4 pt-4 sm:px-6 lg:px-8">
          <Alert className="border-emerald-200 bg-emerald-50"><CheckCircle2 className="text-emerald-700" /><AlertTitle>Signal Ledger</AlertTitle><AlertDescription>{notice}</AlertDescription></Alert>
        </div>
      )}

      {activeView === 'overview' && (
        <div className="mx-auto grid max-w-[1440px] gap-5 px-4 py-5 sm:px-6 lg:grid-cols-[minmax(0,1fr)_340px] lg:px-8 lg:py-7">
          <section className="min-w-0 space-y-5">
            <div className="flex items-end justify-between gap-4">
              <div><p className="mb-1 text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">{new Date().toLocaleDateString('en-CA', { weekday: 'long', month: 'long', day: 'numeric' })}</p>
                <h1 className="font-heading text-2xl font-bold tracking-[-0.035em] sm:text-3xl">Your strategy at a glance</h1></div>
            </div>
            <div className="grid gap-3 sm:grid-cols-3">
              <Card className="bg-foreground text-background ring-0">
                <CardHeader><CardDescription className="text-background/60">Portfolio value</CardDescription><CardTitle className="text-2xl">{money(totalPortfolio)}</CardTitle>
                  <CardAction><CircleDollarSign className="size-5 text-background/50" /></CardAction></CardHeader>
              </Card>
              <Card><CardHeader><CardDescription>Strategy capital</CardDescription><CardTitle className="text-2xl">{money(strategyCapital)}</CardTitle></CardHeader>
                <CardContent><Progress value={activePct} className="[&_[data-slot=progress-indicator]]:bg-teal-500" /><p className="mt-2 text-xs text-muted-foreground">{activePct}% active · {100 - activePct}% cash / idle sweep</p></CardContent></Card>
              <Card><CardHeader><CardDescription>Cash position</CardDescription><CardTitle className="text-2xl">{money(cashBalance)}</CardTitle></CardHeader>
                <CardContent><Button size="sm" variant="outline" onClick={() => setCashOpen(true)}><CircleDollarSign />Adjust cash</Button></CardContent></Card>
            </div>
            <Card><CardHeader><CardTitle>Overall performance</CardTitle><CardDescription>Real portfolio value, recorded once per daily job run (or manual refresh).</CardDescription></CardHeader>
              <CardContent><PerformanceChart data={snapshots} /></CardContent></Card>
            <Card>
              <CardHeader><CardTitle>Add a position</CardTitle><CardDescription>Record a trade you actually made — the system never places orders itself.</CardDescription></CardHeader>
              <CardContent><Button onClick={() => { setAddPrefill(null); setAddOpen(true); }}><Plus />Record a purchase</Button></CardContent>
            </Card>
            <PositionList title="Open positions" description="Tap a position for its chart, strategy events, and controls." positions={openPositions} onSelect={showPosition} />
          </section>
          <aside className="space-y-5">
            <Card><CardHeader><CardTitle>Strategy switches</CardTitle><CardDescription>Disabling stops new entries; open positions keep exit monitoring.</CardDescription></CardHeader>
              <CardContent className="space-y-1">
                {STRATEGY_META.map(({ key, name }) => (
                  <label key={key} className="flex cursor-pointer items-center gap-3 rounded-xl px-2 py-3 hover:bg-secondary/70">
                    <span className="min-w-0 flex-1"><span className="block font-semibold">{name}</span><span className="block text-xs text-muted-foreground">{strategyDetail(key, settings)}</span></span>
                    <Switch checked={settings.strategies[key]} onCheckedChange={(checked) => toggleStrategy(key, checked)} aria-label={`Toggle ${name}`} />
                  </label>
                ))}
                <Button variant="outline" className="mt-3 w-full" onClick={openSettingsDialog}><SlidersHorizontal />Adjust parameters</Button>
              </CardContent>
            </Card>
            <Card><CardHeader><CardTitle>Candidates passing the screen</CardTitle><CardDescription>From the latest scan (a daily job run, or Refresh now).</CardDescription>
              <CardAction><Badge variant="secondary">{candidates.length}</Badge></CardAction></CardHeader>
              <CardContent className="space-y-3 text-sm">
                <Button className="w-full" variant="secondary" onClick={() => changeView('signals')}><Sparkles />Explore candidates</Button>
              </CardContent>
            </Card>
            {idleSweep && (
              <Card><CardHeader><CardTitle>Idle cash sweep</CardTitle><CardDescription>Currently holding {idleSweep.shares.toFixed(2)} shares of {idleSweep.ticker}.</CardDescription></CardHeader>
                <CardContent><Button variant="outline" className="w-full" onClick={() => withNotice(async () => { await api.sellIdleSweep(); }, 'Idle sweep sold.')}>Sell idle sweep now</Button></CardContent>
              </Card>
            )}
            <Card><CardHeader><CardTitle className="flex items-center gap-2"><Database className="size-4" />Account imports</CardTitle><CardDescription>Reconcile with official CSV exports.</CardDescription></CardHeader>
              <CardContent className="space-y-2">
                <Button variant="outline" className="w-full" onClick={() => { setImportSource('wealthsimple'); setImportOpen(true); }}><UploadCloud />Import Wealthsimple CSV</Button>
                <Button variant="outline" className="w-full" onClick={() => { setImportSource('yahoo'); setImportOpen(true); }}><UploadCloud />Import Yahoo CSV</Button>
              </CardContent>
            </Card>
          </aside>
        </div>
      )}

      {activeView === 'signals' && (
        <SimpleView title="Signals" subtitle="Candidates passing the screen right now">
          {candidates.length === 0 ? (
            <Card><CardContent className="py-8 text-center text-sm text-muted-foreground">No candidates yet — click Refresh now, or wait for the next daily job run.</CardContent></Card>
          ) : candidates.map((c) => (
            <Card key={c.ticker}>
              <CardContent className="flex items-center justify-between gap-3 py-4">
                <div><p className="font-semibold">{c.ticker}</p><p className="text-xs text-muted-foreground">Catalyst {c.catalyst_date} · {c.days_until} day{c.days_until === 1 ? '' : 's'} away</p></div>
                <Button size="sm" onClick={() => { setAddPrefill({ ticker: c.ticker, catalyst_date: c.catalyst_date }); setAddOpen(true); }}>Record purchase<ChevronRight /></Button>
              </CardContent>
            </Card>
          ))}
        </SimpleView>
      )}

      {activeView === 'portfolio' && (
        <SimpleView title="Portfolio" subtitle="Open and closed positions, cash, and performance">
          <div className="grid gap-4 sm:grid-cols-3">
            <Card><CardHeader><CardDescription>Total value</CardDescription><CardTitle>{money(totalPortfolio)}</CardTitle></CardHeader></Card>
            <Card><CardHeader><CardDescription>Cash</CardDescription><CardTitle>{money(cashBalance)}</CardTitle></CardHeader><CardContent><Button variant="outline" onClick={() => setCashOpen(true)}>Adjust cash</Button></CardContent></Card>
            <Card><CardHeader><CardDescription>Closed positions</CardDescription><CardTitle>{closedPositions.length}</CardTitle></CardHeader></Card>
          </div>
          <PositionList title="Open positions" description="Click any row to inspect or adjust it." positions={openPositions} onSelect={showPosition} />
          <PositionList title="Closed / exited positions" description="Completed trades remain available for performance history." positions={closedPositions} onSelect={showPosition} empty="No closed positions yet." />
        </SimpleView>
      )}

      {activeView === 'activity' && (
        <SimpleView title="Activity" subtitle="What the engine has actually found or recommended, most recent first">
          {activity.length === 0 ? (
            <Card><CardContent className="py-8 text-center text-sm text-muted-foreground">Nothing yet — click Refresh now to run a check.</CardContent></Card>
          ) : activity.map((a, i) => (
            <Card key={i}>
              <CardHeader>
                <div className="mb-2 flex items-center justify-between">
                  {a.ticker && <Badge variant="secondary">{a.ticker}</Badge>}
                  <span className="text-xs text-muted-foreground">{new Date(a.time).toLocaleString()}</span>
                </div>
                <CardTitle className="leading-snug">{a.label}</CardTitle>
                <CardDescription>{Object.entries(a.detail).filter(([k]) => k !== 'position_id').map(([k, v]) => `${k}: ${v}`).join(' · ')}</CardDescription>
              </CardHeader>
            </Card>
          ))}
        </SimpleView>
      )}

      {activeView === 'strategies' && (
        <SimpleView title="Strategies" subtitle="Enable strategies and tune their rules">
          <Card><CardHeader><CardTitle>Strategy switches</CardTitle><CardDescription>Existing positions remain monitored when a strategy is disabled.</CardDescription></CardHeader>
            <CardContent className="space-y-2">
              {STRATEGY_META.map(({ key, name }) => (
                <label key={key} className="flex items-center gap-3 rounded-xl border p-4">
                  <span className="flex-1"><span className="block font-semibold">{name}</span><span className="text-xs text-muted-foreground">{strategyDetail(key, settings)}</span></span>
                  <Switch checked={settings.strategies[key]} onCheckedChange={(checked) => toggleStrategy(key, checked)} />
                </label>
              ))}
              <Button className="mt-3 w-full" onClick={openSettingsDialog}><SlidersHorizontal />Adjust parameters</Button>
            </CardContent>
          </Card>
          <Card><CardHeader><CardTitle className="flex items-center gap-2"><Database className="size-4" />Backup</CardTitle>
            <CardDescription>Free hosting wipes stored data on a code update — export before, restore after.</CardDescription></CardHeader>
            <CardContent className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={downloadBackup}>Export backup</Button>
              <Button variant="outline" onClick={() => document.getElementById('restore-input')?.click()}>Restore backup</Button>
              <input id="restore-input" type="file" accept=".json" className="sr-only" onChange={(e) => e.target.files?.[0] && restoreBackup(e.target.files[0])} />
            </CardContent>
          </Card>
        </SimpleView>
      )}

      <nav className="fixed inset-x-3 bottom-3 z-40 grid grid-cols-5 rounded-2xl border bg-background/95 p-1.5 shadow-xl backdrop-blur-xl lg:hidden" aria-label="Mobile navigation">
        {([[LayoutDashboard, 'Home', 'overview'], [Sparkles, 'Signals', 'signals'], [WalletCards, 'Portfolio', 'portfolio'], [Newspaper, 'Activity', 'activity'], [Settings2, 'Settings', 'strategies']] as const).map(([Icon, label, view]) => (
          <button type="button" key={view} onClick={() => changeView(view)} aria-current={activeView === view ? 'page' : undefined}
            className={`flex min-h-12 flex-col items-center justify-center gap-0.5 rounded-xl text-[10px] font-semibold ${activeView === view ? 'bg-secondary text-primary' : 'text-muted-foreground'}`}>
            <Icon className="size-4" />{label}
          </button>
        ))}
      </nav>

      {/* Position detail / edit dialog */}
      <Dialog open={positionOpen} onOpenChange={setPositionOpen}>
        <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-3xl">
          {selected && (
            <>
              <DialogHeader>
                <div className="flex flex-wrap items-center gap-2"><DialogTitle>{selected.ticker}</DialogTitle>
                  <Badge className={selected.status === 'open' ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-200 text-slate-700'}>{selected.status}</Badge></div>
                <DialogDescription>{selected.sector || 'Sector unknown'} · {selected.lot_label}</DialogDescription>
              </DialogHeader>
              <div className="rounded-xl border p-2">
                {selectedHistory ? <PositionChart history={selectedHistory} /> : <p className="py-16 text-center text-sm text-muted-foreground">Loading chart…</p>}
              </div>
              <p className="text-xs text-muted-foreground">Chart uses the closes fetched live for this ticker.</p>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field><FieldLabel htmlFor="edit-shares">Quantity</FieldLabel><Input id="edit-shares" inputMode="numeric" value={editShares} onChange={(e) => setEditShares(e.target.value)} /></Field>
                <Field><FieldLabel htmlFor="edit-entry">Average entry price (CAD)</FieldLabel><Input id="edit-entry" inputMode="decimal" value={editEntryPrice} onChange={(e) => setEditEntryPrice(e.target.value)} /></Field>
              </div>
              {selected.status === 'open' && (
                <div className="grid gap-4 rounded-xl bg-secondary/50 p-3 sm:grid-cols-2">
                  <Field><FieldLabel htmlFor="exit-price">Exit price (CAD)</FieldLabel><Input id="exit-price" inputMode="decimal" value={editExitPrice} onChange={(e) => setEditExitPrice(e.target.value)} /></Field>
                  <Field><FieldLabel htmlFor="exit-date">Exit date</FieldLabel><Input id="exit-date" type="date" value={editExitDate} onChange={(e) => setEditExitDate(e.target.value)} /></Field>
                </div>
              )}
              <DialogFooter className="flex-wrap sm:justify-between">
                <Button variant="ghost" className="text-destructive" onClick={() => setRemoveOpen(true)}><Trash2 />Remove</Button>
                <div className="flex flex-wrap gap-2">
                  <Button variant="outline" onClick={() => withNotice(async () => {
                    await api.updatePosition(selected.id, { shares: Math.max(0, Number(editShares)), entry_price: Number(editEntryPrice) });
                  }, `${selected.ticker} details updated.`).then(() => setPositionOpen(false))}>Save changes</Button>
                  {selected.status === 'open' ? (
                    <Button onClick={() => withNotice(async () => {
                      await api.closePosition(selected.id, { exit_price: editExitPrice, exit_date: editExitDate });
                    }, `${selected.ticker} marked closed.`).then(() => setPositionOpen(false))}><XCircle />Mark closed / exited</Button>
                  ) : (
                    <Button onClick={() => withNotice(async () => { await api.updatePosition(selected.id, { reopen: true }); }, `${selected.ticker} reopened.`).then(() => setPositionOpen(false))}>Reopen position</Button>
                  )}
                </div>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={removeOpen} onOpenChange={setRemoveOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader><DialogTitle>Remove this position?</DialogTitle>
            <DialogDescription>This removes {selected?.ticker} from the portfolio and its history. Closing it instead preserves performance records.</DialogDescription></DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRemoveOpen(false)}>Keep position</Button>
            <Button variant="destructive" onClick={() => {
              if (!selected) return;
              withNotice(async () => { await api.removePosition(selected.id); }, `${selected.ticker} removed.`);
              setRemoveOpen(false); setPositionOpen(false);
            }}>Remove permanently</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Add position dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader><DialogTitle>Record a purchase</DialogTitle><DialogDescription>Enter what you actually bought — this never places an order for you.</DialogDescription></DialogHeader>
          <AddPositionForm prefillTicker={addPrefill?.ticker} prefillCatalyst={addPrefill?.catalyst_date}
            onSubmit={(body) => withNotice(async () => { await api.addPosition(body); }, `${body.ticker} recorded.`).then(() => setAddOpen(false))} />
        </DialogContent>
      </Dialog>

      {/* Add-on lot dialog */}
      <Dialog open={addonOpen} onOpenChange={setAddonOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader><DialogTitle>Record a Mechanism O add-on lot</DialogTitle>
            <DialogDescription>Link it to its base position — the base's ticker/catalyst carry over automatically.</DialogDescription></DialogHeader>
          <AddonPositionForm basePositions={availableBase} onSubmit={(body) => withNotice(async () => { await api.addAddonPosition(body); }, 'Add-on lot recorded.').then(() => setAddonOpen(false))} />
        </DialogContent>
      </Dialog>

      {/* Settings dialog */}
      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
          <DialogHeader><DialogTitle>Strategy parameters</DialogTitle><DialogDescription>Each strategy has its own colour. Values update as you move a slider.</DialogDescription></DialogHeader>
          {paramDraft && (
            <div className="space-y-4">
              {(Object.keys(GROUP_META) as ParamField['group'][]).map((group) => {
                const meta = GROUP_META[group];
                const fields = PARAM_FIELDS.filter((f) => f.group === group);
                return (
                  <section key={group} className={`rounded-2xl border p-4 ${meta.tone}`}>
                    <div className="mb-4 flex items-center justify-between">
                      <h3 className="font-bold">{meta.label}</h3>
                      <span className={`rounded-full px-2.5 py-1 text-[10px] font-bold uppercase ${meta.badge}`}>{meta.label}</span>
                    </div>
                    {group === 'idle' && (
                      <Field><FieldLabel htmlFor="idle-ticker">Index ticker</FieldLabel>
                        <Input id="idle-ticker" value={paramDraft.idle.sweep_ticker}
                          onChange={(e) => setParamDraft({ ...paramDraft, idle: { ...paramDraft.idle, sweep_ticker: e.target.value.toUpperCase() } })} /></Field>
                    )}
                    {group === 'idle' && (
                      <div className="mt-4"><div className="mb-2 flex items-center justify-between gap-3"><span className="text-sm font-medium">Days idle before sweeping</span>
                        <Badge variant="outline" className="bg-white/70">{paramDraft.idle.min_holding_days} days</Badge></div>
                        <Slider value={[paramDraft.idle.min_holding_days]} min={1} max={90} step={1}
                          onValueChange={(v) => setParamDraft({ ...paramDraft, idle: { ...paramDraft.idle, min_holding_days: Array.isArray(v) ? v[0] : v } })} /></div>
                    )}
                    <div className="space-y-5">
                      {fields.map((f) => {
                        const groupValues = paramDraft[f.group] as unknown as Record<string, number>;
                        const value = groupValues[f.key];
                        const disabled = Boolean(f.notWiredKey);
                        return (
                          <div key={f.key} className={disabled ? 'opacity-60' : ''}>
                            <div className="mb-2 flex items-center justify-between gap-3"><span className="text-sm font-medium">{f.label}</span>
                              <Badge variant="outline" className="bg-white/70">{f.format(value)}</Badge></div>
                            <Slider disabled={disabled} value={[value]} min={f.min} max={f.max} step={f.step ?? 1}
                              onValueChange={(next) => {
                                const v = Array.isArray(next) ? next[0] : next;
                                if (typeof v !== 'number' || !Number.isFinite(v)) return;
                                setParamDraft({ ...paramDraft, [f.group]: { ...groupValues, [f.key]: v } } as Settings);
                              }} aria-label={f.label} />
                            {f.notWiredKey && <p className="mt-1 text-[11px] text-muted-foreground">{paramDraft.not_wired[f.notWiredKey]}</p>}
                          </div>
                        );
                      })}
                    </div>
                  </section>
                );
              })}
            </div>
          )}
          <DialogFooter><Button variant="outline" onClick={() => setSettingsOpen(false)}>Cancel</Button><Button onClick={saveParameters}>Save parameters</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Cash dialog */}
      <Dialog open={cashOpen} onOpenChange={setCashOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader><DialogTitle>Adjust cash position</DialogTitle><DialogDescription>Current recorded cash: {money(cashBalance)}. Deposits increase it; withdrawals reduce it.</DialogDescription></DialogHeader>
          <div className="grid grid-cols-2 gap-2">
            <Button variant={cashDirection === 'deposit' ? 'default' : 'outline'} onClick={() => setCashDirection('deposit')}><Plus />Add cash</Button>
            <Button variant={cashDirection === 'withdrawal' ? 'default' : 'outline'} onClick={() => setCashDirection('withdrawal')}><Minus />Remove cash</Button>
          </div>
          <Field><FieldLabel htmlFor="cash-amount">Amount (CAD)</FieldLabel><Input id="cash-amount" inputMode="decimal" value={cashAmount} onChange={(e) => setCashAmount(e.target.value)} /></Field>
          <Alert><CircleDollarSign /><AlertTitle>Projected cash: {money(cashBalance + (cashDirection === 'deposit' ? Number(cashAmount || 0) : -Number(cashAmount || 0)))}</AlertTitle>
            <AlertDescription>The updated amount will immediately appear on the overview and portfolio screens.</AlertDescription></Alert>
          <DialogFooter><Button variant="outline" onClick={() => setCashOpen(false)}>Cancel</Button><Button onClick={recordCash}>Record {cashDirection === 'deposit' ? 'deposit' : 'withdrawal'}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Import dialog */}
      <Dialog open={importOpen} onOpenChange={(open) => { setImportOpen(open); if (!open) { setImportState('idle'); setProposals([]); } }}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
          <DialogHeader><DialogTitle>Import portfolio records</DialogTitle><DialogDescription>Upload a CSV exported from {importSource === 'wealthsimple' ? 'Wealthsimple' : 'Yahoo Finance'}.</DialogDescription></DialogHeader>
          <div className="grid grid-cols-2 gap-2">
            <Button variant={importSource === 'wealthsimple' ? 'default' : 'outline'} onClick={() => setImportSource('wealthsimple')}>Wealthsimple</Button>
            <Button variant={importSource === 'yahoo' ? 'default' : 'outline'} onClick={() => setImportSource('yahoo')}>Yahoo Finance</Button>
          </div>
          {importState !== 'preview' && (
            <div className="rounded-xl border border-dashed bg-secondary/40 p-5 text-center">
              <UploadCloud className="mx-auto mb-2 size-8 text-primary" /><p className="font-semibold">Choose a CSV export</p>
              <input ref={fileInput} type="file" accept=".csv,text/csv" className="sr-only" onChange={(e) => e.target.files?.[0] && importCsv(e.target.files[0])} />
              <Button className="mt-4" variant="outline" disabled={importState === 'uploading'} onClick={() => fileInput.current?.click()}>Select CSV file</Button>
            </div>
          )}
          {importState === 'error' && <Alert variant="destructive"><AlertTriangle /><AlertTitle>Import failed</AlertTitle><AlertDescription>{importMessage}</AlertDescription></Alert>}
          {importState === 'preview' && (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">Review what this file found — nothing is applied until you confirm below.</p>
              {proposals.map((p, i) => (
                <label key={i} className={`flex items-start gap-3 rounded-xl border p-3 text-sm ${p.kind === 'skip' ? 'opacity-70' : ''}`}>
                  <input type="checkbox" className="mt-1 size-4 accent-primary" disabled={p.kind === 'skip'} checked={p.default_include}
                    onChange={(e) => setProposals((prev) => prev.map((x, idx) => idx === i ? { ...x, default_include: e.target.checked } : x))} />
                  <span className="min-w-0 flex-1">
                    <span className="block font-semibold">{p.ticker} {p.kind === 'new_position' && `— ${p.shares?.toFixed(2)} sh @ ${money(p.entry_price ?? 0)}`}</span>
                    <span className="block text-xs text-muted-foreground">{p.reason}</span>
                  </span>
                </label>
              ))}
            </div>
          )}
          <DialogFooter>
            {importState === 'preview' && <Button onClick={applyImport}>Apply selected</Button>}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}

function AddPositionForm({ prefillTicker, prefillCatalyst, onSubmit }: {
  prefillTicker?: string; prefillCatalyst?: string;
  onSubmit: (body: { ticker: string; entry_date: string; entry_price: number; dollar_amount: number; catalyst_date: string; sector?: string }) => void;
}) {
  const today = new Date().toISOString().slice(0, 10);
  const [ticker, setTicker] = useState(prefillTicker ?? '');
  const [entryDate, setEntryDate] = useState(today);
  const [entryPrice, setEntryPrice] = useState('');
  const [dollarAmount, setDollarAmount] = useState('');
  const [catalystDate, setCatalystDate] = useState(prefillCatalyst ?? today);
  const [sector, setSector] = useState('');

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field><FieldLabel>Ticker</FieldLabel><Input value={ticker} onChange={(e) => setTicker(e.target.value)} placeholder="e.g. ABC.TO" /></Field>
        <Field><FieldLabel>Sector (optional)</FieldLabel><Input value={sector} onChange={(e) => setSector(e.target.value)} /></Field>
        <Field><FieldLabel>Entry date</FieldLabel><Input type="date" value={entryDate} onChange={(e) => setEntryDate(e.target.value)} /></Field>
        <Field><FieldLabel>Catalyst date</FieldLabel><Input type="date" value={catalystDate} onChange={(e) => setCatalystDate(e.target.value)} /></Field>
        <Field><FieldLabel>Entry price</FieldLabel><Input inputMode="decimal" value={entryPrice} onChange={(e) => setEntryPrice(e.target.value)} /></Field>
        <Field><FieldLabel>Dollar amount</FieldLabel><Input inputMode="decimal" value={dollarAmount} onChange={(e) => setDollarAmount(e.target.value)} /></Field>
      </div>
      <p className="text-xs text-muted-foreground">Entry date can be in the past — the trailing-stop peak is backfilled from real price history since then.</p>
      <DialogFooter>
        <Button onClick={() => onSubmit({ ticker, entry_date: entryDate, entry_price: Number(entryPrice), dollar_amount: Number(dollarAmount), catalyst_date: catalystDate, sector: sector || undefined })}
          disabled={!ticker || !entryPrice || !dollarAmount}>Add</Button>
      </DialogFooter>
    </div>
  );
}

function AddonPositionForm({ basePositions, onSubmit }: {
  basePositions: { id: number; ticker: string; entry_date: string; catalyst_date: string }[];
  onSubmit: (body: { parent_id: number; lot_type: 'o1' | 'o2'; entry_date: string; entry_price: number; dollar_amount: number }) => void;
}) {
  const today = new Date().toISOString().slice(0, 10);
  const [parentId, setParentId] = useState<number | ''>('');
  const [lotType, setLotType] = useState<'o1' | 'o2'>('o1');
  const [entryDate, setEntryDate] = useState(today);
  const [entryPrice, setEntryPrice] = useState('');
  const [dollarAmount, setDollarAmount] = useState('');

  return (
    <div className="space-y-4">
      <Field><FieldLabel>Base position</FieldLabel>
        <select className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" value={parentId} onChange={(e) => setParentId(Number(e.target.value))}>
          <option value="" disabled>Choose…</option>
          {basePositions.map((b) => <option key={b.id} value={b.id}>{b.ticker} (entered {b.entry_date})</option>)}
        </select>
      </Field>
      <Field><FieldLabel>Lot type</FieldLabel>
        <select className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" value={lotType} onChange={(e) => setLotType(e.target.value as 'o1' | 'o2')}>
          <option value="o1">O1 (dip-buy)</option><option value="o2">O2 (momentum-buy)</option>
        </select>
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field><FieldLabel>Entry date</FieldLabel><Input type="date" value={entryDate} onChange={(e) => setEntryDate(e.target.value)} /></Field>
        <Field><FieldLabel>Entry price</FieldLabel><Input inputMode="decimal" value={entryPrice} onChange={(e) => setEntryPrice(e.target.value)} /></Field>
        <Field><FieldLabel>Dollar amount</FieldLabel><Input inputMode="decimal" value={dollarAmount} onChange={(e) => setDollarAmount(e.target.value)} /></Field>
      </div>
      <DialogFooter>
        <Button disabled={!parentId || !entryPrice || !dollarAmount}
          onClick={() => parentId && onSubmit({ parent_id: parentId, lot_type: lotType, entry_date: entryDate, entry_price: Number(entryPrice), dollar_amount: Number(dollarAmount) })}>
          Add add-on lot
        </Button>
      </DialogFooter>
    </div>
  );
}
