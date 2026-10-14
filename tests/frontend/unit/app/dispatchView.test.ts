import { describe, expect, it } from 'vitest';

import { buildDispatchView, canReplay, hasLiveRound, phaseOf, replaySpan } from '../../../../src/app/dispatchView';
import { dispatchLayout, sideMid } from '../../../../src/app/dispatchLayout';
import { AgentPlanStep } from '../../../../src/components/ComponentType';

const T0 = 1_760_000_000_000;
const sec = (s: number) => T0 + s * 1000;

/** The v3 design frame: 4 steps, limit 2; s2 done at 9.8 s, s3 took its slot, s4 still waiting; now = 21 s. */
function designPlan(): AgentPlanStep[] {
  const q = { round: 1, queued_at: T0, run_id: 'r1', timeout_s: 180 };
  return [
    { id: 's1', title: '可再生能源占比变化', status: 'in_progress', subagent: { ...q, status: 'running', started_at: sec(0) } },
    { id: 's2', title: '各国 CO₂ 排放变化', status: 'done', subagent: { ...q, status: 'success', started_at: sec(0), duration_s: 9.8, charts: 1, chart_titles: ['CO₂'], findings: 'UK fell most.', calls: 2, prompt_tokens: 9000, cached_tokens: 8000, completion_tokens: 600, tool_calls: 1 } },
    { id: 's3', title: '人均排放对比', status: 'in_progress', subagent: { ...q, status: 'running', started_at: sec(9.8) } },
    { id: 's4', title: '能源结构与排放', status: 'in_progress', subagent: { ...q, status: 'queued' } },
    { id: 's5', title: '综合结论', status: 'pending', depends_on: ['s1', 's2', 's3', 's4'] },
  ];
}

/** Two rounds: a and b, then c and d, which need both. */
function twoRounds(): AgentPlanStep[] {
  const r1 = { round: 1, queued_at: T0, run_id: 'r1', max_parallel: 2 };
  const r2 = { round: 2, queued_at: sec(15), run_id: 'r1', max_parallel: 2 };
  return [
    { id: 'a', title: 'A', status: 'done', depends_on: [], subagent: { ...r1, status: 'success', started_at: sec(0), duration_s: 6, charts: 1 } },
    { id: 'b', title: 'B', status: 'done', depends_on: [], subagent: { ...r1, status: 'success', started_at: sec(0), duration_s: 10, charts: 1, findings: 'B found.' } },
    { id: 'c', title: 'C', status: 'done', depends_on: ['a', 'b'], subagent: { ...r2, status: 'success', started_at: sec(15), duration_s: 6, charts: 1 } },
    { id: 'd', title: 'D', status: 'done', depends_on: ['a', 'b'], subagent: { ...r2, status: 'success', started_at: sec(16), duration_s: 7, charts: 1 } },
    { id: 'e', title: 'Conclude', status: 'pending', depends_on: ['c', 'd'] },
  ];
}

describe('buildDispatchView', () => {
  it('is null without sub-agents', () => {
    expect(buildDispatchView([{ id: 'a', title: 'A', status: 'pending' }], T0, true)).toBeNull();
    expect(buildDispatchView(undefined, T0, true)).toBeNull();
  });

  it('derives the design frame from the records', () => {
    const v = buildDispatchView(designPlan(), sec(21), true)!;
    expect(v.steps.map(s => s.phase)).toEqual(['running', 'done', 'running', 'queued']);
    expect(v.steps.map(s => s.number)).toEqual([1, 2, 3, 4]);
    expect([v.total, v.finished, v.running, v.queued, v.charts]).toEqual([4, 1, 2, 1, 1]);
    expect(v.live).toBe(true);
    expect(v.mainPhase).toBe('waiting');
    expect(v.ownSteps).toEqual([{ number: 5, title: '综合结论' }]);
    expect(v.elapsedSeconds).toBe(21);
    expect(v.serialSeconds).toBe(42);                 // 21 + 9.8 + 11.2
    expect(v.savedSeconds).toBe(21);
    const s3 = v.steps[2];
    expect([s3.runSeconds, s3.waitSeconds]).toEqual([11.2, 9.8]);
    expect(v.steps[3].waitSeconds).toBe(21);
    expect(v.steps[1].usage).toEqual({ calls: 2, promptTokens: 9000, cachedTokens: 8000, completionTokens: 600, toolCalls: 1 });
    expect(v.steps[0].usage).toBeNull();               // not measured yet
  });

  it('flags only the step far slower than its finished siblings', () => {
    const v = buildDispatchView(designPlan(), sec(21), true)!;
    expect(v.steps.map(s => s.straggler)).toEqual([true, false, false, false]);
    const early = buildDispatchView(designPlan(), sec(15), true)!;   // 15 s < 2 × 9.8 s
    expect(early.steps[0].straggler).toBe(false);
    const straggle = early.log.find(e => e.kind === 'straggler');
    expect(straggle).toBeUndefined();
    const at21 = buildDispatchView(designPlan(), sec(21), true)!.log.find(e => e.kind === 'straggler')!;
    expect(at21.at).toBe(sec(19.6));                   // when it crossed 2 × 9.8 s
  });

  it('draws queued and running segments on the timeline', () => {
    const v = buildDispatchView(designPlan(), sec(21), true)!;
    const row = (n: number) => v.timeline[n].segments.map(s => [s.kind, (s.from - T0) / 1000, (s.to - T0) / 1000]);
    expect(row(0)).toEqual([['running', 0, 21]]);
    expect(row(1)).toEqual([['done', 0, 9.8]]);
    expect(row(2)).toEqual([['queued', 0, 9.8], ['running', 9.8, 21]]);
    expect(row(3)).toEqual([['queued', 0, 21]]);
  });

  it('logs in time order from the records only', () => {
    const v = buildDispatchView(designPlan(), sec(21), true)!;
    expect(v.log.map(e => [e.kind, e.stepNumber])).toEqual([
      ['dispatch', null], ['queued', 3], ['queued', 4], ['done', 2], ['started', 3], ['straggler', 1],
    ]);
    expect(v.log.find(e => e.kind === 'done')!.params).toEqual({ s: '9.8', charts: 1 });
  });

  it('ends the round when every step is back, and keeps the numbers for replay', () => {
    const plan = designPlan().map(s => s.subagent ? { ...s, subagent: { ...s.subagent, status: s.id === 's4' ? 'timeout' : 'success', started_at: s.subagent.started_at ?? sec(21), duration_s: s.subagent.duration_s ?? 5 } } : s);
    const v = buildDispatchView(plan, sec(99), true)!;
    expect(v.live).toBe(false);
    expect(v.mainPhase).toBe('merging');
    expect(v.steps[3].phase).toBe('failed');
    expect(v.elapsedSeconds).toBe(26);                 // s4: queued until 21 s, ran 5 s
    expect(buildDispatchView(plan, sec(99), false)!.mainPhase).toBe('done');
    expect(v.log.at(-1)!.kind).toBe('timeout');
  });

  it('draws every round, one column each, with per-round savings', () => {
    const v = buildDispatchView(twoRounds(), sec(40), false)!;
    expect(v.rounds.map(r => [r.round, r.steps.map(s => s.id)])).toEqual([[1, ['a', 'b']], [2, ['c', 'd']]]);
    expect(v.round).toBe(2);
    expect(v.steps.map(s => s.round)).toEqual([1, 1, 2, 2]);
    expect(v.rounds.map(r => [r.elapsedSeconds, r.serialSeconds, r.savedSeconds])).toEqual([[10, 16, 6], [8, 13, 5]]);
    expect(v.savedSeconds).toBe(11);                  // per round; the gap between rounds is the main agent's
    expect(v.elapsedSeconds).toBe(23);                // 0 → 23 s
    expect(v.serialSeconds).toBe(29);
    expect(v.timeline.map(r => r.round)).toEqual([1, 1, 2, 2]);
    expect(v.log.filter(e => e.kind === 'dispatch').map(e => e.params)).toEqual([{ n: 2, round: 1 }, { n: 2, round: 2 }]);
    expect(v.steps.find(s => s.id === 'c')!.waitsFor).toEqual([1, 2]);
  });

  it('shows the steps a later round will take as locked while the first runs', () => {
    const plan = twoRounds().map(s => s.id === 'c' || s.id === 'd' ? { ...s, status: 'pending' as const, subagent: undefined } : s);
    plan[0] = { ...plan[0], subagent: { ...plan[0].subagent!, status: 'running', duration_s: undefined } };
    const v = buildDispatchView(plan, sec(8), true)!;
    expect(v.rounds.map(r => [r.round, r.started, r.steps.map(s => s.phase)])).toEqual([
      [1, true, ['running', 'done']], [2, false, ['locked', 'locked']],
    ]);
    expect([v.total, v.locked]).toEqual([2, 2]);
    expect(v.ownSteps.map(o => o.number)).toEqual([5]);   // the closing step alone: the main agent's
    // once the run is over nothing is locked any more
    expect(buildDispatchView(plan, sec(8), false)!.rounds).toHaveLength(1);
  });

  it('leaves a single dependent step to the main agent (no second column)', () => {
    const v = buildDispatchView(designPlan(), sec(21), true)!;
    expect(v.rounds).toHaveLength(1);
    expect(v.locked).toBe(0);
  });

  it('replays the records as of an earlier time', () => {
    const plan = twoRounds();
    const at = (s: number) => buildDispatchView(plan, sec(s), false, { replay: true })!;
    expect(at(0.5).steps.map(s => s.phase)).toEqual(['running', 'running']);
    expect(at(0.5).rounds[1].steps.map(s => s.phase)).toEqual(['locked', 'locked']);
    expect(at(0.5).mainPhase).toBe('waiting');
    expect(at(7).steps.map(s => s.phase)).toEqual(['done', 'running']);
    expect(at(7).charts).toBe(1);                       // b's chart is not back yet
    expect(at(7).steps[1].findings).toBe('');
    expect(at(12).mainPhase).toBe('merging');           // between the rounds
    expect(at(12).rounds[1].started).toBe(false);
    expect(at(18).rounds[1].steps.map(s => s.phase)).toEqual(['running', 'running']);
    expect(at(18).steps[2].runSeconds).toBe(3);
    expect(at(15.5).rounds[1].steps.map(s => s.phase)).toEqual(['running', 'queued']);   // d starts at 16 s
    expect(at(99).mainPhase).toBe('done');
    expect(at(99).steps.every(s => s.phase === 'done')).toBe(true);
    // the log of a replay has only what had happened
    expect(at(7).log.every(e => e.at <= sec(7) || e.kind === 'queued')).toBe(true);
  });

  it('gives the replay its span and key times', () => {
    expect(replaySpan(twoRounds())).toEqual({ start: sec(0), end: sec(23) });
    expect(buildDispatchView(twoRounds(), sec(99), false)!.keyTimes.map(x => (x - T0) / 1000)).toEqual([0, 6, 10, 15, 16, 21, 23]);
    expect(canReplay(twoRounds())).toBe(true);
    expect(canReplay(designPlan())).toBe(false);        // still running
    expect(canReplay([{ id: 'a', title: 'A', status: 'done' }])).toBe(false);
  });

  it('maps backend statuses to phases', () => {
    expect(['queued', 'running', 'success', 'budget_exhausted', 'timeout', 'failed', 'stopped', 'cancelled'].map(phaseOf))
      .toEqual(['queued', 'running', 'done', 'done', 'failed', 'failed', 'failed', 'stopped']);
    expect(hasLiveRound(designPlan())).toBe(true);
    expect(hasLiveRound([{ id: 'a', title: 'A', status: 'done', subagent: { status: 'success' } }])).toBe(false);
  });
});

describe('dispatchLayout', () => {
  const near = (a: [number, number], b: [number, number]) => Math.abs(a[0] - b[0]) < 0.01 && Math.abs(a[1] - b[1]) < 0.01;

  it.each([1, 2, 3, 4, 6])('links %i step(s) at the middle of the box sides', n => {
    const L = dispatchLayout(n);
    expect(L.cards).toHaveLength(n);
    expect(near(L.toMain.from, sideMid(L.main, 'right'))).toBe(true);
    expect(near(L.toMain.to, sideMid(L.dispatch, 'left'))).toBe(true);
    expect(near(L.trunk.from, sideMid(L.dispatch, 'right'))).toBe(true);
    L.cards.forEach((card, i) => {
      expect(near(L.toStep[i].to, sideMid(card, 'left'))).toBe(true);
      expect(near(L.fromStep[i].from, sideMid(card, 'right'))).toBe(true);
      expect(near(L.fromStep[i].to, sideMid(L.merge, 'left'))).toBe(true);
      expect(L.toStep[i].d.endsWith(`H${card.x}`)).toBe(true);
      expect(L.fromStep[i].d.endsWith(`H${L.merge.x}`)).toBe(true);
    });
    expect(near(L.back.from, sideMid(L.merge, 'bottom'))).toBe(true);
    expect(near(L.back.to, sideMid(L.main, 'bottom'))).toBe(true);
    // main, dispatch and merge share one centre line
    expect(sideMid(L.main, 'right')[1]).toBeCloseTo(sideMid(L.dispatch, 'left')[1]);
    expect(sideMid(L.dispatch, 'right')[1]).toBeCloseTo(sideMid(L.merge, 'left')[1]);
  });

  it('puts each round in its own column, joined at the side middles', () => {
    const L = dispatchLayout([2, 3]);
    expect(L.columns.map(c => c.cards.length)).toEqual([2, 3]);
    expect(L.joints).toHaveLength(1);
    const [c1, c2] = L.columns;
    expect(c2.cards[0].x).toBeGreaterThan(L.joints[0].x + L.joints[0].w);
    expect(L.joints[0].x).toBeGreaterThan(c1.cards[0].x + c1.cards[0].w);
    c1.fromStep.forEach(w => expect(near(w.to, sideMid(L.joints[0], 'left'))).toBe(true));
    expect(near(c2.trunk.from, sideMid(L.joints[0], 'right'))).toBe(true);
    c2.cards.forEach((card, i) => {
      expect(near(c2.toStep[i].to, sideMid(card, 'left'))).toBe(true);
      expect(near(c2.fromStep[i].to, sideMid(L.merge, 'left'))).toBe(true);
    });
    expect(L.width).toBe(L.merge.x + L.merge.w);
    expect(L.width).toBeLessThan(1150);                // two rounds still fit a canvas at ~0.75
  });

  it('never overlaps boxes and keeps everything inside', () => {
    for (const n of [1, 3, 4, 8, [2, 2], [4, 1], [3, 2, 2]] as (number | number[])[]) {
      const L = dispatchLayout(n);
      const boxes = [L.main, L.dispatch, L.merge, ...L.joints, ...L.cards];
      for (let i = 0; i < boxes.length; i++) {
        const a = boxes[i];
        expect(a.x >= 0 && a.y >= 0 && a.x + a.w <= L.width && a.y + a.h <= L.height).toBe(true);
        for (let j = i + 1; j < boxes.length; j++) {
          const b = boxes[j];
          const overlap = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
          expect(overlap).toBe(false);
        }
      }
    }
  });
});
