// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * Per-run agent settings chosen in the UI, and the sub-agent controls.
 *
 * Every field is optional: a missing one follows the server default
 * (env vars, see py-src/alidade/analyst/engine/run_settings.py). The chosen
 * values are sent with each analyst request as `agent_settings`; the backend
 * clamps them to the same ranges as below and saves them with the run, so a
 * run that pauses for a question resumes with the settings it started with.
 */

import { apiRequest } from './apiClient';
import { getUrls } from './utils';

export interface AgentSettings {
    /** hand independent plan steps to parallel sub-agents */
    subagents?: boolean;
    /** how many sub-agents run at the same time */
    maxParallel?: number;
    /** charts each sub-agent may show */
    subagentMaxCharts?: number;
    /** seconds before a sub-agent is stopped */
    subagentTimeoutS?: number;
    /** plan steps advance by themselves as charts are shown */
    planAutoAdvance?: boolean;
}

/** The backend's wire names. */
export interface RunSettingsWire {
    subagents: boolean;
    max_parallel: number;
    subagent_max_charts: number;
    subagent_timeout_s: number;
    plan_auto_advance: boolean;
}

export interface RunSettingsInfo {
    defaults: RunSettingsWire;
    limits: Record<'max_parallel' | 'subagent_max_charts' | 'subagent_timeout_s', { min: number; max: number }>;
}

/** Ranges (the backend enforces the same). */
export const AGENT_SETTING_LIMITS = {
    maxParallel: { min: 1, max: 4 },
    subagentMaxCharts: { min: 1, max: 3 },
    subagentTimeoutS: { min: 30, max: 600 },
} as const;

const clamp = (value: number, { min, max }: { min: number; max: number }) =>
    Math.min(max, Math.max(min, Math.round(value)));

/** The request field for these settings; undefined when everything follows the server. */
export function toRequestSettings(s: AgentSettings | undefined): Partial<RunSettingsWire> | undefined {
    if (!s) return undefined;
    const out: Partial<RunSettingsWire> = {};
    if (typeof s.subagents === 'boolean') out.subagents = s.subagents;
    if (typeof s.planAutoAdvance === 'boolean') out.plan_auto_advance = s.planAutoAdvance;
    if (Number.isFinite(s.maxParallel)) out.max_parallel = clamp(s.maxParallel!, AGENT_SETTING_LIMITS.maxParallel);
    if (Number.isFinite(s.subagentMaxCharts)) {
        out.subagent_max_charts = clamp(s.subagentMaxCharts!, AGENT_SETTING_LIMITS.subagentMaxCharts);
    }
    if (Number.isFinite(s.subagentTimeoutS)) {
        out.subagent_timeout_s = clamp(s.subagentTimeoutS!, AGENT_SETTING_LIMITS.subagentTimeoutS);
    }
    return Object.keys(out).length > 0 ? out : undefined;
}

/** Drop fields that are unset, so "server default" is stored as absence. */
export function compactSettings(s: AgentSettings): AgentSettings {
    return Object.fromEntries(
        Object.entries(s).filter(([, v]) => v !== undefined && v !== null && !(typeof v === 'number' && !Number.isFinite(v))),
    ) as AgentSettings;
}

export function sameSettings(a: AgentSettings | undefined, b: AgentSettings | undefined): boolean {
    const x = compactSettings(a || {}), y = compactSettings(b || {});
    const keys = new Set([...Object.keys(x), ...Object.keys(y)]);
    return Array.from(keys).every(k => (x as any)[k] === (y as any)[k]);
}

export async function fetchRunSettingsInfo(): Promise<RunSettingsInfo> {
    const { data } = await apiRequest<RunSettingsInfo>(getUrls().RUN_SETTINGS);
    return data;
}

/** Ask the backend to stop one sub-agent. Resolves to false when it was no longer running. */
export async function stopSubagent(runId: string, stepId: string): Promise<boolean> {
    const { data } = await apiRequest<{ stopped: boolean }>(getUrls().SUBAGENT_STOP, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: runId, step_id: stepId }),
    });
    return !!data?.stopped;
}

/** Finished states of a sub-agent (anything else is still going). */
export const SUBAGENT_FINISHED = new Set(['success', 'budget_exhausted', 'timeout', 'cancelled', 'stopped', 'failed']);

export function subagentIsLive(status: string | undefined): boolean {
    return !!status && !SUBAGENT_FINISHED.has(status);
}
