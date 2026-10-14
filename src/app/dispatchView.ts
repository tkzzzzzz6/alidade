// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * What the dispatch panel shows, as a pure function of the plan's sub-agent
 * records and the current time.
 *
 * Everything on the panel — step states, the timeline, the totals, the
 * "slowing the round" flag and the event log — is derived from the records
 * the backend keeps on each plan step (`queued_at`, `started_at`,
 * `duration_s`, status, usage). Nothing is simulated.
 *
 * Every round of the run is shown, one column per round: steps a later round
 * will take (they wait for steps of an earlier one) are drawn "locked" in
 * their column until they are handed out. With `replay` the records are read
 * as of `now` (a step that had not started by then is queued, one that had
 * not ended is running), which is how a finished run is played back.
 */

import { AgentPlanStep, SubagentRecord } from '../components/ComponentType';

/** `locked`: not handed out yet — waiting for steps of an earlier round. */
export type StepPhase = 'locked' | 'queued' | 'running' | 'done' | 'failed' | 'stopped';

export interface StepUsage {
    calls: number;
    promptTokens: number;
    cachedTokens: number;
    completionTokens: number;
    toolCalls: number;
}

export interface DispatchStepView {
    id: string;
    /** 1-based position in the plan (the "Step n" the rest of the UI uses) */
    number: number;
    title: string;
    /** the fan-out round (= the diagram column, 1-based) */
    round: number;
    /** plan numbers of the steps this one waits for */
    waitsFor: number[];
    phase: StepPhase;
    /** the backend status (success, budget_exhausted, timeout, …) */
    status: string;
    queuedAt: number;
    startedAt: number | null;
    endedAt: number | null;
    /** seconds running so far (live) or in total (finished) */
    runSeconds: number;
    /** seconds spent waiting for a slot */
    waitSeconds: number;
    timeoutSeconds: number | null;
    charts: number;
    chartTitles: string[];
    findings: string;
    error: string;
    straggler: boolean;
    usage: StepUsage | null;
}

export type SegmentKind = 'queued' | 'running' | 'done' | 'failed';

export interface TimelineRow {
    id: string;
    number: number;
    round: number;
    segments: { kind: SegmentKind; from: number; to: number }[];
}

export type LogKind = 'dispatch' | 'queued' | 'started' | 'done' | 'failed' | 'timeout' | 'cancelled' | 'straggler';

export interface LogEntry {
    at: number;
    /** null = the main agent */
    stepNumber: number | null;
    kind: LogKind;
    params: Record<string, string | number>;
}

export type MainPhase = 'waiting' | 'merging' | 'done';

export interface RoundView {
    round: number;
    /** the steps of this column (locked ones included) */
    steps: DispatchStepView[];
    /** handed out (not locked) */
    total: number;
    finished: number;
    /** false while the round is still to be handed out */
    started: boolean;
    live: boolean;
    start: number;
    end: number;
    elapsedSeconds: number;
    serialSeconds: number;
    savedSeconds: number;
}

export interface DispatchView {
    /** the latest round handed out */
    round: number;
    /** one per diagram column, in order */
    rounds: RoundView[];
    /** the run's limit on sub-agents at once (null when the records predate it) */
    maxParallel: number | null;
    /** handed-out steps of every round, in round then plan order (no locked ones) */
    steps: DispatchStepView[];
    /** plan steps the main agent does itself (no sub-agent) */
    ownSteps: { number: number; title: string }[];
    total: number;
    finished: number;
    running: number;
    queued: number;
    /** steps waiting for a later round */
    locked: number;
    charts: number;
    live: boolean;
    mainPhase: MainPhase;
    /** first step queued */
    roundStart: number;
    /** end so far: now while live, else the last step's end */
    roundEnd: number;
    elapsedSeconds: number;
    /** sum of every step's running time: what it would take one after another */
    serialSeconds: number;
    /** summed per round (the time between rounds is the main agent's, not saved by running in parallel) */
    savedSeconds: number;
    /** times worth jumping to in a replay: every start and end */
    keyTimes: number[];
    timeline: TimelineRow[];
    log: LogEntry[];
}

/** A step is "slowing the round" once it has run this many times the typical finished sibling. */
export const STRAGGLER_FACTOR = 2;
/** … and at least this much longer (a 3 s step against a 1 s one is not worth flagging). */
export const STRAGGLER_MIN_EXTRA_S = 5;

const FINISHED_OK = new Set(['success', 'budget_exhausted']);

export function phaseOf(status: string | undefined): StepPhase {
    if (!status || status === 'queued') return 'queued';
    if (status === 'running') return 'running';
    if (FINISHED_OK.has(status)) return 'done';
    if (status === 'cancelled') return 'stopped';
    return 'failed';
}

function median(values: number[]): number {
    const v = [...values].sort((a, b) => a - b);
    const mid = Math.floor(v.length / 2);
    return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

function usageOf(r: SubagentRecord): StepUsage | null {
    if (r.calls == null) return null;
    return {
        calls: r.calls ?? 0, promptTokens: r.prompt_tokens ?? 0, cachedTokens: r.cached_tokens ?? 0,
        completionTokens: r.completion_tokens ?? 0, toolCalls: r.tool_calls ?? 0,
    };
}

const round1 = (x: number) => Math.round(x * 10) / 10;

interface StepSource { step: AgentPlanStep; number: number }

/**
 * Pending steps a later round will probably take: grouped by how deep they sit
 * behind the delegated steps, a group of two or more is what the dispatcher
 * hands out together (one step alone the main agent does itself). Only the
 * groups right after the last real round, in order, are kept.
 */
function lockedColumns(all: StepSource[], lastRound: number): Map<string, number> {
    const byId = new Map(all.map(x => [x.step.id, x]));
    const depth = new Map<string, number>();
    const depthOf = (id: string, seen: Set<string>): number => {
        if (depth.has(id)) return depth.get(id)!;
        const x = byId.get(id);
        if (!x || seen.has(id)) return 0;
        seen.add(id);
        let d: number;
        if (x.step.subagent) d = x.step.subagent.round ?? 1;
        else if (x.step.status === 'pending') {
            const deps = (x.step.depends_on || []).map(dep => depthOf(dep, seen));
            const below = deps.length ? Math.max(...deps) : 0;
            d = below > 0 ? below + 1 : 0;
        } else d = 0;
        depth.set(id, d);
        return d;
    };
    const out = new Map<string, number>();
    for (let col = lastRound + 1; ; col++) {
        const group = all.filter(({ step }) => !step.subagent && step.status === 'pending' && depthOf(step.id, new Set()) === col);
        if (group.length < 2) break;
        group.forEach(({ step }) => out.set(step.id, col));
    }
    return out;
}

function stepView(src: StepSource, round: number, numberOf: Map<string, number>, now: number, replay: boolean): DispatchStepView {
    const { step, number } = src;
    const waitsFor = (step.depends_on || []).map(d => numberOf.get(d)).filter((n): n is number => n != null);
    const base = {
        id: step.id, number, title: step.title, round, waitsFor,
        timeoutSeconds: null as number | null, charts: 0, chartTitles: [] as string[], findings: '', error: '',
        straggler: false, usage: null as StepUsage | null,
    };
    const r = step.subagent;
    if (!r || (replay && r.queued_at != null && r.queued_at > now)) {
        return { ...base, phase: 'locked', status: 'locked', queuedAt: now, startedAt: null, endedAt: null, runSeconds: 0, waitSeconds: 0,
                 timeoutSeconds: r?.timeout_s ?? null };
    }
    let phase = phaseOf(r.status);
    let startedAt = r.started_at ?? null;
    const queuedAt = r.queued_at ?? startedAt ?? now;
    let finished = phase !== 'queued' && phase !== 'running';
    let endedAt = finished && startedAt != null && r.duration_s != null ? startedAt + r.duration_s * 1000 : null;
    let status = r.status;
    let ended = true;
    if (replay) {
        // the records as they stood at `now`
        if (startedAt != null && startedAt > now) {
            startedAt = null; endedAt = null; phase = 'queued'; status = 'queued'; finished = false;
        } else if (endedAt != null && endedAt > now) {
            endedAt = null; phase = 'running'; status = 'running'; finished = false;
        }
        ended = finished;
    }
    const runSeconds = startedAt == null ? 0
        : finished ? (r.duration_s ?? 0) : Math.max(0, (now - startedAt) / 1000);
    const waitSeconds = Math.max(0, ((startedAt ?? now) - queuedAt) / 1000);
    return {
        ...base, phase, status, queuedAt, startedAt, endedAt,
        runSeconds: round1(runSeconds), waitSeconds: round1(waitSeconds),
        timeoutSeconds: r.timeout_s ?? null,
        charts: ended ? (r.charts ?? (r.chart_titles?.length || 0)) : 0,
        chartTitles: ended ? (r.chart_titles || []) : [],
        findings: ended ? (r.findings || '') : '', error: ended ? (r.error || '') : '',
        usage: ended ? usageOf(r) : null,
    };
}

/**
 * The panel for every fan-out round of `plan`, or null when no step was
 * handed to a sub-agent. `runActive`: the run is still going (after the last
 * step returns the main agent merges and writes the answer). `replay`: read
 * the records as of `now` (playing back a finished run).
 */
export function buildDispatchView(
    plan: AgentPlanStep[] | undefined, now: number, runActive: boolean, opts: { replay?: boolean } = {},
): DispatchView | null {
    const replay = !!opts.replay;
    const all: StepSource[] = (plan || []).map((step, i) => ({ step, number: i + 1 }));
    const delegated = all.filter(({ step }) => step.subagent);
    if (delegated.length === 0) return null;
    const numberOf = new Map(all.map(x => [x.step.id, x.number]));
    const lastRound = Math.max(...delegated.map(({ step }) => step.subagent!.round ?? 1));
    const locked = runActive || replay ? lockedColumns(all, lastRound) : new Map<string, number>();
    const columnOf = (x: StepSource) => x.step.subagent ? (x.step.subagent.round ?? 1) : locked.get(x.step.id);
    const shown = all.filter(x => columnOf(x) != null);
    const columns = Array.from(new Set(shown.map(x => columnOf(x)!))).sort((a, b) => a - b);

    const views = new Map(shown.map(x => [x.step.id, stepView(x, columnOf(x)!, numberOf, now, replay)]));
    const handed = (list: DispatchStepView[]) => list.filter(s => s.phase !== 'locked');

    // a step running (or having run) far longer than its finished siblings in the round
    for (const col of columns) {
        const steps = handed(shown.filter(x => columnOf(x) === col).map(x => views.get(x.step.id)!));
        for (const s of steps) {
            if (s.startedAt == null) continue;
            const others = steps.filter(o => o !== s && o.endedAt != null).map(o => o.runSeconds);
            if (others.length === 0) continue;
            const typical = median(others);
            s.straggler = s.runSeconds > STRAGGLER_FACTOR * typical && s.runSeconds - typical >= STRAGGLER_MIN_EXTRA_S;
        }
    }

    const steps = columns.flatMap(col => handed(shown.filter(x => columnOf(x) === col).map(x => views.get(x.step.id)!)));
    const live = steps.some(s => s.phase === 'queued' || s.phase === 'running');
    const roundStart = steps.length ? Math.min(...steps.map(s => s.queuedAt)) : now;
    const ends = steps.map(s => s.endedAt).filter((x): x is number => x != null);
    const roundEnd = live || ends.length === 0 ? Math.max(now, roundStart) : Math.max(...ends);

    const rounds: RoundView[] = columns.map(col => {
        const colSteps = shown.filter(x => columnOf(x) === col).map(x => views.get(x.step.id)!);
        const out = handed(colSteps);
        const isLive = out.some(s => s.phase === 'queued' || s.phase === 'running');
        const start = out.length ? Math.min(...out.map(s => s.queuedAt)) : now;
        const colEnds = out.map(s => s.endedAt).filter((x): x is number => x != null);
        const end = isLive || colEnds.length === 0 ? Math.max(now, start) : Math.max(...colEnds);
        const elapsed = out.length ? (end - start) / 1000 : 0;
        const serial = out.reduce((sum, s) => sum + s.runSeconds, 0);
        return {
            round: col, steps: colSteps, total: out.length,
            finished: out.filter(s => s.phase !== 'queued' && s.phase !== 'running').length,
            started: out.length > 0, live: isLive, start, end,
            elapsedSeconds: round1(elapsed), serialSeconds: round1(serial),
            savedSeconds: round1(out.length > 1 ? Math.max(0, serial - elapsed) : 0),
        };
    });
    const elapsedSeconds = round1((roundEnd - roundStart) / 1000);
    const serialSeconds = round1(steps.reduce((sum, s) => sum + s.runSeconds, 0));
    const round = Math.max(1, ...rounds.filter(r => r.started).map(r => r.round));

    const timeline: TimelineRow[] = steps.map(s => {
        const segments: TimelineRow['segments'] = [];
        const startOrNow = s.startedAt ?? roundEnd;
        if (startOrNow > s.queuedAt + 250) segments.push({ kind: 'queued', from: s.queuedAt, to: startOrNow });
        if (s.startedAt != null) {
            const kind: SegmentKind = s.phase === 'running' ? 'running' : s.phase === 'done' ? 'done' : 'failed';
            segments.push({ kind, from: s.startedAt, to: s.endedAt ?? roundEnd });
        }
        return { id: s.id, number: s.number, round: s.round, segments };
    });

    const log: LogEntry[] = [];
    for (const r of rounds) {
        if (r.started) log.push({ at: r.start, stepNumber: null, kind: 'dispatch', params: { n: r.total, round: r.round } });
    }
    for (const s of steps) {
        if (s.waitSeconds >= 1 || s.phase === 'queued') log.push({ at: s.queuedAt, stepNumber: s.number, kind: 'queued', params: {} });
        // a step that started right away is covered by the dispatch row
        if (s.startedAt != null && s.waitSeconds >= 1) log.push({ at: s.startedAt, stepNumber: s.number, kind: 'started', params: {} });
        if (s.endedAt != null) {
            const kind: LogKind = s.phase === 'done' ? 'done' : s.status === 'timeout' ? 'timeout'
                : s.phase === 'stopped' ? 'cancelled' : 'failed';
            log.push({ at: s.endedAt, stepNumber: s.number, kind, params: { s: s.runSeconds.toFixed(1), charts: s.charts } });
        }
        if (s.straggler && s.startedAt != null) {
            const others = steps.filter(o => o !== s && o.round === s.round && o.endedAt != null).map(o => o.runSeconds);
            const at = s.startedAt + STRAGGLER_FACTOR * median(others) * 1000;
            if (at <= roundEnd) log.push({ at, stepNumber: s.number, kind: 'straggler', params: {} });
        }
    }
    log.sort((a, b) => a.at - b.at);

    // every start and end in the records (not as of now), for the replay control
    const keyTimes = Array.from(new Set(delegated.flatMap(({ step }) => {
        const r = step.subagent!;
        const out: number[] = [];
        if (r.queued_at != null) out.push(r.queued_at);
        if (r.started_at != null) {
            out.push(r.started_at);
            if (r.duration_s != null) out.push(r.started_at + r.duration_s * 1000);
        }
        return out;
    }))).sort((a, b) => a - b);

    const count = (p: StepPhase) => steps.filter(s => s.phase === p).length;
    const limits = delegated.map(({ step }) => step.subagent!.max_parallel).filter((x): x is number => !!x);
    const moreToCome = rounds.some(r => !r.started);
    return {
        round, rounds, maxParallel: limits.length ? limits[0] : null, steps,
        ownSteps: all.filter(x => columnOf(x) == null).map(({ step, number }) => ({ number, title: step.title })),
        total: steps.length, finished: steps.length - count('queued') - count('running'),
        running: count('running'), queued: count('queued'),
        locked: rounds.reduce((sum, r) => sum + r.steps.filter(s => s.phase === 'locked').length, 0),
        charts: steps.reduce((sum, s) => sum + s.charts, 0),
        live, mainPhase: live ? 'waiting' : runActive || (replay && moreToCome) ? 'merging' : 'done',
        roundStart, roundEnd, elapsedSeconds, serialSeconds,
        savedSeconds: round1(rounds.reduce((sum, r) => sum + r.savedSeconds, 0)),
        keyTimes, timeline, log,
    };
}

/** The span a replay plays over: first step handed out → last step back. */
export function replaySpan(plan: AgentPlanStep[] | undefined): { start: number; end: number } | null {
    const v = buildDispatchView(plan, Number.MAX_SAFE_INTEGER / 2, false);
    if (!v || v.keyTimes.length === 0) return null;
    const start = v.keyTimes[0];
    const end = Math.max(...v.keyTimes);
    return end > start ? { start, end } : null;
}

/** Does this plan have a fan-out round worth replaying (every step handed out has finished)? */
export function canReplay(plan: AgentPlanStep[] | undefined): boolean {
    return !hasLiveRound(plan) && replaySpan(plan) != null;
}

/** Is a sub-agent round going on in this plan right now (the panel should take over the canvas)? */
export function hasLiveRound(plan: AgentPlanStep[] | undefined): boolean {
    return (plan || []).some(s => s.subagent && (s.subagent.status === 'queued' || s.subagent.status === 'running'));
}
