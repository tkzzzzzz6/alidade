import { describe, expect, it } from 'vitest';

import {
    buildSpans, cacheShare, defaultLabeler, formatMs, formatTokens, lanesOf, reasonLabel, TraceEvent,
} from '../../../../src/views/traceTimeline';

const ev = (t_ms: number, type: string, extra: Record<string, any> = {}): TraceEvent => ({ type, t_ms, ...extra });

describe('traceTimeline', () => {
    it('turns model calls into spans that end when the call returned', () => {
        const [span] = buildSpans([ev(1500, 'llm_call', {
            model: 'small', tier: 'fast', reason: 'routine step', duration_ms: 1200,
            prompt_tokens: 12_000, completion_tokens: 80, tool_calls: ['visualize'],
        })]);
        expect(span).toMatchObject({ kind: 'llm', start: 300, end: 1500, tone: 'fast' });
        expect(span.label).toContain('small (routine step) → visualize');
        expect(span.label).toContain('12k → 80');
    });

    it('pairs tool starts and results per lane, even when sub-agents interleave', () => {
        const spans = buildSpans([
            ev(100, 'tool_start', { tool: 'execute_python_script', purpose: 'A', step_id: 's1' }),
            ev(120, 'tool_start', { tool: 'execute_python_script', purpose: 'B', step_id: 's2' }),
            ev(400, 'tool_result', { tool: 'execute_python_script', status: 'ok', step_id: 's2' }),
            ev(900, 'tool_result', { tool: 'execute_python_script', status: 'error', error: 'boom', step_id: 's1' }),
        ]);
        expect(spans.map(s => [s.lane, s.start, s.end, s.tone, s.label])).toEqual([
            ['step:s1', 100, 900, 'error', 'execute_python_script: A'],
            ['step:s2', 120, 400, 'tool', 'execute_python_script: B'],
        ]);
        expect(lanesOf(spans)).toEqual(['main', 'step:s1', 'step:s2']);
    });

    it('a chart action runs from the decision to the chart, or to the check that withheld it', () => {
        const spans = buildSpans([
            ev(0, 'action', { action: 'visualize', display_instruction: 'Sales by region' }),
            ev(800, 'chart_check', { status: 'retrying' }),
            ev(900, 'action', { action: 'visualize', display_instruction: 'Sales by region' }),
            ev(1700, 'result', { status: 'success', title: 'Sales by region' }),
            ev(1800, 'completion', { status: 'success' }),
        ]);
        const actions = spans.filter(s => s.kind === 'action');
        expect(actions.map(s => [s.start, s.end, s.tone])).toEqual([[0, 800, 'error'], [900, 1700, 'chart']]);
        expect(spans.filter(s => s.kind === 'mark').map(s => s.tone)).toEqual(['error', 'done']);
    });

    it('closes what never finished at the end of the segment', () => {
        const spans = buildSpans([ev(50, 'tool_start', { tool: 'inspect_source_data', table_names: ['sales'] })], 3000);
        expect(spans[0]).toMatchObject({ start: 50, end: 3000 });
        expect(spans[0].label).toContain('(no result)');
    });

    it('formats durations, tokens and cache share', () => {
        expect([formatMs(420), formatMs(4200), formatMs(125_000), formatMs(undefined)])
            .toEqual(['420 ms', '4.2 s', '2 min 5 s', '—']);
        expect([formatTokens(950), formatTokens(4321), formatTokens(54_321), formatTokens(2_500_000)])
            .toEqual(['950', '4.3k', '54k', '2.50M']);
        expect(cacheShare({ prompt_tokens: 1000, cached_tokens: 900 })).toBeCloseTo(0.9);
        expect(cacheShare({ prompt_tokens: 0, cached_tokens: 0 })).toBeNull();
    });

    it('names why a model was picked', () => {
        expect(reasonLabel('routine step', defaultLabeler)).toBe('routine step');
        expect(reasonLabel('final answer has numbers with no source (4,213.5, 9)', defaultLabeler))
            .toBe('numbers with no source (4,213.5, 9), redone');
        expect(reasonLabel('something new', defaultLabeler)).toBe('something new');
        expect(reasonLabel(undefined, defaultLabeler)).toBe('');
    });
});
