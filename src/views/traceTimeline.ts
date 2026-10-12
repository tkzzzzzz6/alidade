// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * Turn a recorded run trace (see py-src/alidade/analyst/engine/trace.py) into
 * waterfall rows: model calls, tool calls and chart actions become spans with
 * a start and an end; everything else (chart checks, plan changes, questions,
 * the final answer) becomes a point mark. Sub-agent events go to their own
 * lane, keyed by the plan step they work on.
 */

export interface TraceEvent {
    type: string;
    t_ms: number;
    step_id?: string;
    [key: string]: any;
}

export interface TraceSegment {
    kind: 'start' | 'resume';
    started_at?: string;
    input?: string;
    model?: string;
    fast_model?: string;
    status?: string;
    duration_ms?: number;
    events: TraceEvent[];
}

export interface TraceSummary {
    duration_ms: number;
    status?: string | null;
    llm_calls: number;
    llm_ms?: number;
    prompt_tokens: number;
    cached_tokens: number;
    completion_tokens: number;
    by_model: Record<string, { calls: number; prompt: number; cached: number; completion: number }>;
    tiers: Record<string, number>;
    tool_calls: number;
    charts: number;
    chart_checks: number;
    subagents: number;
    asks: number;
    errors: number;
}

export interface TraceListItem {
    run_id: string;
    question: string;
    started_at?: string;
    updated_at?: string;
    model?: string;
    fast_model?: string;
    segments: number;
    summary: TraceSummary;
}

export interface RunTraceData extends Omit<TraceListItem, 'segments'> {
    segments: TraceSegment[];
}

export type SpanTone = 'strong' | 'fast' | 'main' | 'tool' | 'chart' | 'error' | 'ask' | 'info' | 'done';

export interface TraceSpan {
    lane: string;              // 'main' or 'step:<id>'
    kind: 'llm' | 'tool' | 'action' | 'mark';
    start: number;             // ms from the segment start
    end: number;               // = start for marks
    label: string;
    tone: SpanTone;
    events: TraceEvent[];      // the raw events behind the row (for the detail view)
}

export const MAIN_LANE = 'main';

export function laneOf(e: TraceEvent): string {
    return e.step_id ? `step:${e.step_id}` : MAIN_LANE;
}

function tierTone(tier: unknown): SpanTone {
    return tier === 'strong' ? 'strong' : tier === 'fast' ? 'fast' : 'main';
}

function tokensText(e: TraceEvent): string {
    const p = e.prompt_tokens, c = e.completion_tokens;
    if (p == null && c == null) return '';
    return ` · ${formatTokens(p ?? 0)} → ${formatTokens(c ?? 0)}`;
}

function toolLabel(e: TraceEvent): string {
    const what = e.purpose || e.query || e.skill || (Array.isArray(e.table_names) ? e.table_names.join(', ') : '');
    return what ? `${e.tool}: ${what}` : String(e.tool || 'tool');
}

/** Label templates for marks; `{{name}}` is filled from params. Override for translations. */
export const DEFAULT_LABELS: Record<string, string> = {
    chartCheck: 'chart check: {{status}}',
    plan: 'plan: {{steps}}',
    asked: 'asked the user: {{questions}}',
    final: 'final answer ({{status}})',
    error: 'error: {{message}}',
    subStarted: 'sub-agent started: {{title}}',
    subEnded: 'sub-agent {{status}} ({{charts}} charts)',
    dropped: 'dropped calls next to {{kept}}: {{dropped}}',
    compressed: 'condensed earlier steps',
    contextInfo: 'loaded rules / saved knowledge',
    definitions: 'used saved definitions: {{terms}}',
    insights: 'insight scan: {{count}} findings',
    answer: 'answer',
    noResult: 'no result',
    withheld: 'withheld by the chart check',
    failed: 'failed',
    reasonReadQuestion: 'read the question',
    reasonUserInput: 'new input from the user',
    reasonSystemNote: 'follow a system note',
    reasonMerge: 'merge sub-agent results',
    reasonRecover: 'recover from a failed step',
    reasonRoutine: 'routine step',
    reasonSubagent: 'sub-agent step',
    reasonCompress: 'compress earlier steps',
    reasonEmptyAnswer: 'empty final answer, redone',
    reasonUntraceable: 'numbers with no source ({{numbers}}), redone',
};

export type Labeler = (key: string, params?: Record<string, any>) => string;

/** Why a model was picked (from py-src/alidade/analyst/engine/routing.py), as label keys. */
const REASONS: Record<string, string> = {
    'read the question': 'reasonReadQuestion',
    'new input from the user': 'reasonUserInput',
    'follow a system note': 'reasonSystemNote',
    'merge sub-agent results': 'reasonMerge',
    'recover from a failed step': 'reasonRecover',
    'routine step': 'reasonRoutine',
    'sub-agent step': 'reasonSubagent',
    'compress earlier steps': 'reasonCompress',
    'empty final answer': 'reasonEmptyAnswer',
};

export function reasonLabel(reason: string | undefined, tr: Labeler): string {
    if (!reason) return '';
    if (REASONS[reason]) return tr(REASONS[reason]);
    const m = reason.match(/^final answer has numbers with no source \((.*)\)$/);
    if (m) return tr('reasonUntraceable', { numbers: m[1] });
    return reason;
}

export const defaultLabeler: Labeler = (key, params = {}) =>
    (DEFAULT_LABELS[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_, k) => String(params[k] ?? ''));

function markOf(e: TraceEvent, tr: Labeler): { label: string; tone: SpanTone } | null {
    switch (e.type) {
        case 'chart_check':
            return { label: tr('chartCheck', { status: e.status }), tone: e.status === 'retrying' ? 'error' : 'info' };
        case 'plan':
            return { label: tr('plan', { steps: (e.steps || []).map((s: any) => `${s.title} (${s.status})`).join(' → ') }), tone: 'info' };
        case 'interact':
            return { label: tr('asked', { questions: (e.questions || []).join(' / ') }), tone: 'ask' };
        case 'completion':
            return { label: tr('final', { status: e.status }), tone: e.status === 'success' ? 'done' : 'error' };
        case 'error':
            return { label: tr('error', { message: e.message || '' }), tone: 'error' };
        case 'subagent':
            return e.status === 'started'
                ? { label: tr('subStarted', { title: e.title || e.step_id }), tone: 'info' }
                : { label: tr('subEnded', { status: e.status, charts: e.charts ?? 0 }),
                    tone: e.status === 'success' || e.status === 'budget_exhausted' ? 'info' : 'error' };
        case 'calls_dropped':
            return { label: tr('dropped', { kept: e.kept, dropped: (e.dropped || []).join(', ') }), tone: 'info' };
        case 'context_compressed':
            return { label: tr('compressed'), tone: 'info' };
        case 'context_info':
            return { label: tr('contextInfo'), tone: 'info' };
        case 'definitions_applied':
            return { label: tr('definitions', { terms: (e.terms || []).join(', ') }), tone: 'info' };
        case 'insights':
            return { label: tr('insights', { count: (e.findings || []).length }), tone: 'info' };
        default:
            return null;
    }
}

/** Waterfall rows for one segment, ordered by start time. */
export function buildSpans(events: TraceEvent[], segmentEnd?: number, tr: Labeler = defaultLabeler): TraceSpan[] {
    const spans: TraceSpan[] = [];
    const openTools = new Map<string, TraceEvent[]>();
    const openActions = new Map<string, TraceEvent>();
    const end = segmentEnd ?? Math.max(0, ...events.map(e => e.t_ms || 0));

    const closeAction = (lane: string, e: TraceEvent, tone: SpanTone, suffix: string) => {
        const a = openActions.get(lane);
        if (!a) return false;
        openActions.delete(lane);
        spans.push({
            lane, kind: 'action', start: a.t_ms, end: e.t_ms, tone, events: [a, e],
            label: `${a.action}${a.display_instruction ? `: ${a.display_instruction}` : ''}${suffix}`,
        });
        return true;
    };

    for (const e of events) {
        const lane = laneOf(e);
        if (e.type === 'llm_call') {
            const start = Math.max(0, e.t_ms - (e.duration_ms || 0));
            const calls = ` → ${(e.tool_calls || []).length ? (e.tool_calls || []).join(', ') : tr('answer')}`;
            spans.push({
                lane, kind: 'llm', start, end: e.t_ms, tone: tierTone(e.tier), events: [e],
                label: `${e.model || 'model'}${e.effort ? ` · ${e.effort}` : ''}${e.reason ? ` (${reasonLabel(e.reason, tr)})` : ''}${calls}${tokensText(e)}`,
            });
        } else if (e.type === 'tool_start') {
            const key = `${lane}|${e.tool}`;
            openTools.set(key, [...(openTools.get(key) || []), e]);
        } else if (e.type === 'tool_result') {
            const key = `${lane}|${e.tool}`;
            const queue = openTools.get(key) || [];
            const start = queue.shift();
            openTools.set(key, queue);
            const failed = e.status === 'error' || !!e.error;
            spans.push({
                lane, kind: 'tool', start: start ? start.t_ms : e.t_ms, end: e.t_ms,
                tone: failed ? 'error' : 'tool', events: start ? [start, e] : [e],
                label: start ? toolLabel(start) : String(e.tool),
            });
        } else if (e.type === 'action') {
            if (e.action === 'write_report' || e.action === 'visualize') openActions.set(lane, e);
        } else if (e.type === 'result') {
            if (!closeAction(lane, e, e.status === 'success' ? 'chart' : 'error', e.title ? ` → ${e.title}` : '')) {
                spans.push({ lane, kind: 'mark', start: e.t_ms, end: e.t_ms, tone: 'chart', events: [e],
                             label: `chart: ${e.title || ''}` });
            }
        } else {
            if (e.type === 'chart_check' && e.status === 'retrying') closeAction(lane, e, 'error', ` (${tr('withheld')})`);
            else if (e.type === 'error') closeAction(lane, e, 'error', ` (${tr('failed')})`);
            const mark = markOf(e, tr);
            if (mark) spans.push({ lane, kind: 'mark', start: e.t_ms, end: e.t_ms, events: [e], ...mark });
        }
    }
    // anything still open ran until the segment ended (stopped, or a bug)
    openTools.forEach((queue) => queue.forEach(s => spans.push({
        lane: laneOf(s), kind: 'tool', start: s.t_ms, end, tone: 'tool', events: [s], label: `${toolLabel(s)} (${tr('noResult')})`,
    })));
    openActions.forEach((a, lane) => spans.push({
        lane, kind: 'action', start: a.t_ms, end, tone: 'chart', events: [a], label: `${a.action} (${tr('noResult')})`,
    }));
    return spans.sort((x, y) => x.start - y.start || x.end - y.end);
}

/** Lanes in display order: the main agent, then sub-agent steps. */
export function lanesOf(spans: TraceSpan[]): string[] {
    const steps = Array.from(new Set(spans.map(s => s.lane).filter(l => l !== MAIN_LANE))).sort();
    return [MAIN_LANE, ...steps];
}

export function formatMs(ms: number | null | undefined): string {
    if (ms == null) return '—';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
    const m = Math.floor(ms / 60_000);
    return `${m} min ${Math.round((ms % 60_000) / 1000)} s`;
}

export function formatTokens(n: number | null | undefined): string {
    if (n == null) return '—';
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
    if (n >= 10_000) return `${Math.round(n / 1000)}k`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return String(n);
}

/** Share of prompt tokens served from the provider's prefix cache. */
export function cacheShare(summary: Pick<TraceSummary, 'prompt_tokens' | 'cached_tokens'>): number | null {
    return summary.prompt_tokens > 0 ? summary.cached_tokens / summary.prompt_tokens : null;
}
