// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * Pure helpers for showing sub-agent work in the running-step list.
 *
 * With ALIDADE_SUBAGENTS on, several plan steps run at once and their events
 * (tool_start / tool_result / action / result) arrive interleaved, each
 * carrying a `step_id`. The step list is a flat string array, so lines from a
 * sub-agent get a "[Step 2] " tag and every per-step operation (mark a tool
 * done, collect a chart's steps) only touches lines with that tag. Events
 * without a `step_id` come from the main agent and only touch untagged lines.
 */

import { AgentPlanStep } from '../components/ComponentType';

/** Status markers a step line can start with. */
export const LINE_MARKERS = /^(✓|✗|⚠|📋)\s*/u;

/** 1-based position of a step in the plan, or null when it is not there. */
export function stepNumber(plan: AgentPlanStep[], stepId: string): number | null {
    const i = plan.findIndex(s => s.id === stepId);
    return i >= 0 ? i + 1 : null;
}

/** The tag put in front of a sub-agent's lines, e.g. "[Step 2] ". Empty for the main agent. */
export function lineTag(stepLabel: string | null | undefined): string {
    return stepLabel ? `[${stepLabel}] ` : '';
}

/** Tag a line from `stepLabel` (no-op for the main agent). */
export function tagLine(text: string, stepLabel: string | null | undefined): string {
    return lineTag(stepLabel) + text;
}

function bare(line: string): string {
    return line.replace(LINE_MARKERS, '');
}

/** Does `line` belong to the agent with this tag? (main agent: tag === '') */
export function lineBelongsTo(line: string, tag: string): boolean {
    const body = bare(line);
    if (tag) return body.startsWith(tag);
    return !/^\[[^\]]+\] /u.test(body);
}

/**
 * Mark the newest unfinished tool line of one agent as done (✓) or failed (✗).
 * Returns a new array; lines of other agents and note lines are left alone.
 */
export function markToolDone(lines: string[], tag: string, isError: boolean): string[] {
    const out = [...lines];
    for (let i = out.length - 1; i >= 0; i--) {
        const line = out[i];
        if (line.startsWith('⚠') || line.startsWith('📋')) continue;
        if (!lineBelongsTo(line, tag)) continue;
        if (line.startsWith('✓') || line.startsWith('✗')) continue;
        out[i] = (isError ? '✗ ' : '✓ ') + line;
        break;
    }
    return out;
}

/**
 * Split the lines for one chart: `own` are the lines of the agent that made it
 * (they go into that chart's step history), `rest` stay in the running list
 * because another step is still working.
 */
export function takeLinesFor(lines: string[], tag: string): { own: string[]; rest: string[] } {
    const own: string[] = [];
    const rest: string[] = [];
    for (const line of lines) (lineBelongsTo(line, tag) ? own : rest).push(line);
    return { own, rest };
}

/** Steps currently being worked on, in plan order (only meaningful when > 1). */
export function parallelSteps(plan: AgentPlanStep[] | undefined): number[] {
    if (!plan) return [];
    const nums = plan
        .map((s, i) => (s.status === 'in_progress' ? i + 1 : 0))
        .filter(n => n > 0);
    return nums.length > 1 ? nums : [];
}

/** Text for a chart made by a sub-agent: "Step 2 · <title>". */
export function chartLabel(instruction: string, stepLabel: string | null | undefined): string {
    if (!stepLabel) return instruction;
    return instruction ? `${stepLabel} · ${instruction}` : stepLabel;
}

/** Per-agent value keyed by step id ('' = main agent). */
export class PerStep<T> {
    private values = new Map<string, T>();
    constructor(private readonly empty: () => T) {}
    get(stepId: string | undefined | null): T {
        const key = stepId || '';
        return this.values.has(key) ? (this.values.get(key) as T) : this.empty();
    }
    set(stepId: string | undefined | null, value: T): void {
        this.values.set(stepId || '', value);
    }
    clear(stepId: string | undefined | null): void {
        this.values.delete(stepId || '');
    }
}

/**
 * What one agent is doing right now: its newest line in the running list,
 * without the status marker and the "[Step 2] " tag ('' when it has none).
 */
export function latestActivity(lines: string[], tag: string): string {
    for (let i = lines.length - 1; i >= 0; i--) {
        const line = lines[i];
        if (!tag || !lineBelongsTo(line, tag)) continue;
        return bare(line).slice(tag.length).split('\n')[0].trim();
    }
    return '';
}
