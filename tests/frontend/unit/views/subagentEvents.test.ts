import { describe, expect, it } from 'vitest';

import {
    PerStep, chartLabel, lineBelongsTo, lineTag, markToolDone, parallelSteps, stepNumber, tagLine, takeLinesFor,
} from '../../../../src/views/subagentEvents';
import { AgentPlanStep } from '../../../../src/components/ComponentType';

const plan: AgentPlanStep[] = [
    { id: 's1', title: 'Margins', status: 'done' },
    { id: 's2', title: 'Churn', status: 'in_progress' },
    { id: 's3', title: 'Seasonality', status: 'in_progress' },
];

describe('subagentEvents', () => {
    it('numbers steps by plan position', () => {
        expect(stepNumber(plan, 's3')).toBe(3);
        expect(stepNumber(plan, 'nope')).toBeNull();
    });

    it('tags only sub-agent lines', () => {
        expect(tagLine('Running code', 'Step 2')).toBe('[Step 2] Running code');
        expect(tagLine('Running code', null)).toBe('Running code');
        expect(lineTag(null)).toBe('');
    });

    it('tells main-agent and sub-agent lines apart, with or without a marker', () => {
        expect(lineBelongsTo('✓ [Step 2] Running code', '[Step 2] ')).toBe(true);
        expect(lineBelongsTo('[Step 3] Running code', '[Step 2] ')).toBe(false);
        expect(lineBelongsTo('Inspecting data', '')).toBe(true);
        expect(lineBelongsTo('[Step 2] Running code', '')).toBe(false);
    });

    it('marks the open tool line of the right step when events interleave', () => {
        const lines = ['[Step 2] Running code A', '[Step 3] Running code B', '⚠ [Step 3] note'];
        const out = markToolDone(lines, '[Step 2] ', false);
        expect(out).toEqual(['✓ [Step 2] Running code A', '[Step 3] Running code B', '⚠ [Step 3] note']);
        expect(markToolDone(out, '[Step 3] ', true)[1]).toBe('✗ [Step 3] Running code B');
        expect(lines[0]).toBe('[Step 2] Running code A');            // input untouched
    });

    it('main-agent results never close a sub-agent line', () => {
        const lines = ['✓ Inspecting data', '[Step 2] Running code'];
        expect(markToolDone(lines, '', false)).toEqual(lines);
    });

    it('a chart takes only its own step lines; the rest keep running', () => {
        const lines = ['📋 Step 2 handed to a sub-agent', '✓ [Step 2] Running code', '[Step 3] Running code'];
        const { own, rest } = takeLinesFor(lines, '[Step 2] ');
        expect(own).toEqual(['✓ [Step 2] Running code']);
        expect(rest).toEqual(['📋 Step 2 handed to a sub-agent', '[Step 3] Running code']);
    });

    it('lists steps running at the same time, only when there are several', () => {
        expect(parallelSteps(plan)).toEqual([2, 3]);
        expect(parallelSteps([plan[0], plan[1]])).toEqual([]);
        expect(parallelSteps(undefined)).toEqual([]);
    });

    it('labels charts with their step', () => {
        expect(chartLabel('Churn by region', 'Step 2')).toBe('Step 2 · Churn by region');
        expect(chartLabel('Churn by region', null)).toBe('Churn by region');
        expect(chartLabel('', 'Step 2')).toBe('Step 2');
    });

    it('keeps values per step', () => {
        const v = new PerStep<string[]>(() => []);
        v.set('s2', ['a']);
        v.set(undefined, ['main']);
        expect(v.get('s2')).toEqual(['a']);
        expect(v.get('s3')).toEqual([]);
        expect(v.get(null)).toEqual(['main']);
        v.clear('s2');
        expect(v.get('s2')).toEqual([]);
    });
});
