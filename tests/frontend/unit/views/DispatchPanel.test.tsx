import React from 'react';
import '@testing-library/jest-dom/vitest';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  initReactI18next: { type: '3rdParty', init: () => {} },
  useTranslation: () => ({
    t: (key: string, p?: Record<string, any>) => {
      if (key === 'dataThread.stepTag') return `Step ${p?.n}`;
      return p && Object.keys(p).length ? `${key}(${Object.entries(p).filter(([k]) => k !== 'defaultValue').map(([k, v]) => `${k}=${v}`).join(',')})` : key;
    },
  }),
}));
vi.mock('../../../../src/app/agentSettings', () => ({ stopSubagent: vi.fn(async () => true) }));

import { DispatchPanel, niceStep } from '../../../../src/views/DispatchPanel';
import { DispatchCanvasGate, DispatchReplayButton } from '../../../../src/views/DispatchCanvas';
import { closeReplay, getReplay } from '../../../../src/app/dispatchReplay';
import { stopSubagent } from '../../../../src/app/agentSettings';
import { AgentPlanStep } from '../../../../src/components/ComponentType';

// the canvas tests run on the real clock: keep the round recent
const T0 = Date.now() - 21_000;
const sec = (s: number) => T0 + s * 1000;
const q = { round: 1, queued_at: T0, run_id: 'r1', timeout_s: 180, max_parallel: 2 };
const plan: AgentPlanStep[] = [
  { id: 's1', title: 'Renewables', status: 'in_progress', subagent: { ...q, status: 'running', started_at: sec(0) } },
  { id: 's2', title: 'CO2', status: 'done', subagent: { ...q, status: 'success', started_at: sec(0), duration_s: 9.8, charts: 1, findings: 'UK fell most.\nUS -12%.', calls: 2, prompt_tokens: 9000, cached_tokens: 8000, completion_tokens: 600, tool_calls: 1 } },
  { id: 's3', title: 'Per capita', status: 'in_progress', subagent: { ...q, status: 'running', started_at: sec(9.8) } },
  { id: 's4', title: 'Energy mix', status: 'in_progress', subagent: { ...q, status: 'queued' } },
  { id: 's5', title: 'Conclude', status: 'pending' },
];

describe('DispatchPanel', () => {
  it('shows each step in its state with the round totals', () => {
    render(<DispatchPanel plan={plan} runActive now={sec(21)} runningLines={['[Step 1] visualize · chart 2']} />);
    expect(screen.getByTestId('dispatch-step-s1')).toHaveAttribute('data-phase', 'running');
    expect(screen.getByTestId('dispatch-step-s2')).toHaveAttribute('data-phase', 'done');
    expect(screen.getByTestId('dispatch-step-s4')).toHaveAttribute('data-phase', 'queued');
    expect(within(screen.getByTestId('dispatch-step-s1')).getByText('visualize · chart 2')).toBeInTheDocument();
    expect(within(screen.getByTestId('dispatch-step-s1')).getByText('dispatch.straggler')).toBeInTheDocument();
    expect(within(screen.getByTestId('dispatch-step-s3')).queryByText('dispatch.straggler')).toBeNull();
    expect(screen.getByTestId('dispatch-stats')).toHaveTextContent('dispatch.stats(s=21.0,k=1,n=4,c=1)dispatch.statsLimit(p=2)');
    expect(screen.getByTestId('dispatch-saved')).toHaveTextContent('serial=42.0,actual=21.0,saved=21.0');
    expect(screen.getByTestId('dispatch-main')).toHaveTextContent('Step 5 · Conclude');
    // a fixed clock (tests, replay) draws no moving dots
    expect(document.querySelectorAll('[data-packet]')).toHaveLength(0);
  });

  it('stops a running step and opens the findings of a finished one', () => {
    const onStop = vi.fn();
    render(<DispatchPanel plan={plan} runActive now={sec(21)} onStopStep={onStop} />);
    const stops = screen.getAllByLabelText('dispatch.stop');
    expect(stops).toHaveLength(2);                      // only the running steps
    fireEvent.click(stops[0]);
    expect(onStop).toHaveBeenCalledWith(plan[0]);
    fireEvent.click(within(screen.getByTestId('dispatch-step-s2')).getByText('dispatch.findings'));
    // the card shows the first line; the popover the whole text
    expect(screen.getByText((_, el) => el?.textContent === 'UK fell most.\nUS -12%.' && el.tagName === 'P')).toBeInTheDocument();
  });

  it('logs the round from the records', () => {
    render(<DispatchPanel plan={plan} runActive now={sec(21)} />);
    const log = screen.getByTestId('dispatch-log');
    expect(log).toHaveTextContent('dispatch.logDone(s=9.8,charts=1)');
    expect(log).toHaveTextContent('dispatch.logStraggler');
  });

  it('keeps the timeline to a handful of ticks for any length of round', () => {
    for (const span of [3, 21, 95, 700, 86_400 * 365]) {
      const step = niceStep(span);
      expect(span / step).toBeLessThanOrEqual(6);
      expect(span / step).toBeGreaterThan(1);
    }
    expect([niceStep(21), niceStep(5)]).toEqual([5, 1]);
  });

  it('renders nothing without sub-agents', () => {
    const { container } = render(<DispatchPanel plan={[{ id: 'a', title: 'A', status: 'pending' }]} runActive now={T0} />);
    expect(container).toBeEmptyDOMElement();
  });
});

function storeWith(draftPlan: AgentPlanStep[] | undefined, status = 'running') {
  const state = {
    draftNodes: draftPlan ? [{ id: 'd1', derive: { status, agentPlan: draftPlan, runningPlan: '' } }] : [],
    models: [{ id: 'm', model: 'deepseek-flash' }], globalModels: [], selectedModelId: 'm',
  };
  return configureStore({ reducer: (s: any = state) => s });
}

describe('DispatchCanvasGate', () => {
  const canvas = <div data-testid="charts-canvas">charts</div>;

  it('leaves the canvas alone without a sub-agent round', () => {
    render(<Provider store={storeWith(undefined)}><DispatchCanvasGate>{canvas}</DispatchCanvasGate></Provider>);
    expect(screen.getByTestId('charts-canvas')).toBeInTheDocument();
    expect(screen.queryByTestId('dispatch-toggle')).toBeNull();
  });

  it('takes over the canvas while steps run, and the switch goes back to the charts', () => {
    render(<Provider store={storeWith(plan)}><DispatchCanvasGate>{canvas}</DispatchCanvasGate></Provider>);
    expect(screen.getByTestId('dispatch-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('charts-canvas')).toBeNull();
    expect(document.querySelectorAll('[data-packet]').length).toBeGreaterThan(0);   // live: dots on the working links
    fireEvent.click(within(screen.getByTestId('dispatch-toggle')).getByText('dispatch.viewCharts(n=1)'));
    expect(screen.getByTestId('charts-canvas')).toBeInTheDocument();
    fireEvent.click(within(screen.getByTestId('dispatch-toggle')).getByText('dispatch.viewDispatch'));
    expect(screen.getByTestId('dispatch-panel')).toBeInTheDocument();
  });

  it('shows the charts once the round is over, keeping the switch while the run goes on', () => {
    const over = plan.map(s => s.subagent ? { ...s, subagent: { ...s.subagent, status: 'success', started_at: sec(0), duration_s: 5 } } : s);
    render(<Provider store={storeWith(over)}><DispatchCanvasGate>{canvas}</DispatchCanvasGate></Provider>);
    expect(screen.getByTestId('charts-canvas')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-toggle')).toBeInTheDocument();
  });

  it('stops a step through the API', async () => {
    render(<Provider store={storeWith(plan)}><DispatchCanvasGate>{canvas}</DispatchCanvasGate></Provider>);
    fireEvent.click(screen.getAllByLabelText('dispatch.stop')[0]);
    expect(stopSubagent).toHaveBeenCalledWith('r1', 's1');
  });
});

/** A finished run with two rounds: a, b → c, d (c and d need a and b). */
const F0 = 1_760_000_000_000;
const fs = (x: number) => F0 + x * 1000;
const r1 = { round: 1, queued_at: F0, run_id: 'r9', max_parallel: 2 };
const r2 = { round: 2, queued_at: fs(15), run_id: 'r9', max_parallel: 2 };
const finished: AgentPlanStep[] = [
  { id: 'a', title: 'A', status: 'done', depends_on: [], subagent: { ...r1, status: 'success', started_at: fs(0), duration_s: 6, charts: 1 } },
  { id: 'b', title: 'B', status: 'done', depends_on: [], subagent: { ...r1, status: 'success', started_at: fs(0), duration_s: 10, charts: 1 } },
  { id: 'c', title: 'C', status: 'done', depends_on: ['a', 'b'], subagent: { ...r2, status: 'success', started_at: fs(15), duration_s: 6, charts: 1 } },
  { id: 'd', title: 'D', status: 'done', depends_on: ['a', 'b'], subagent: { ...r2, status: 'success', started_at: fs(16), duration_s: 7, charts: 1 } },
  { id: 'e', title: 'Conclude', status: 'done', depends_on: ['c', 'd'] },
];

describe('DispatchPanel rounds', () => {
  it('draws the second round as a second column behind a merge-and-dispatch joint', () => {
    render(<DispatchPanel plan={finished} runActive={false} now={fs(99)} />);
    expect(screen.getByTestId('dispatch-joint-1')).toHaveTextContent('dispatch.jointSent(round=2,n=2)');
    expect(screen.getByTestId('dispatch-round-1')).toHaveTextContent('dispatch.roundLabel(round=1)');
    expect(screen.getByTestId('dispatch-round-2')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-saved')).toHaveTextContent('dispatch.savedRounds(serial=29.0,actual=23.0,saved=11.0)');
  });

  it('shows the next round locked while the first is running', () => {
    const live = finished.map(s => s.id === 'c' || s.id === 'd' ? { ...s, status: 'pending' as const, subagent: undefined }
      : s.id === 'e' ? { ...s, status: 'pending' as const } : s);
    render(<DispatchPanel plan={live} runActive now={fs(8)} />);
    const c = screen.getByTestId('dispatch-step-c');
    expect(c).toHaveAttribute('data-phase', 'locked');
    expect(c).toHaveTextContent('dispatch.lockedWaits(steps=Step 1dispatch.listSepStep 2)');
    expect(screen.getByTestId('dispatch-joint-1')).toHaveTextContent('dispatch.jointWaiting(k=2,n=2)');
    expect(within(c).queryByLabelText('dispatch.stop')).toBeNull();
  });
});

describe('DispatchPanel replay', () => {
  afterEach(() => { vi.useRealTimers(); closeReplay(); });

  it('starts on the finished picture and plays the run from the start on its own clock', () => {
    vi.useFakeTimers();
    render(<DispatchPanel plan={finished} runActive={false} replay />);
    expect(screen.getByTestId('dispatch-panel')).toHaveAttribute('data-replay', 'true');
    expect(screen.getByTestId('dispatch-step-d')).toHaveAttribute('data-phase', 'done');
    fireEvent.click(screen.getByTestId('dispatch-replay-play'));          // at the end: plays again from 0
    expect(screen.getByTestId('dispatch-step-c')).toHaveAttribute('data-phase', 'locked');
    act(() => { vi.advanceTimersByTime(4000); });                         // 2× → 8 s into the run
    expect(screen.getByTestId('dispatch-step-a')).toHaveAttribute('data-phase', 'done');
    expect(screen.getByTestId('dispatch-step-b')).toHaveAttribute('data-phase', 'running');
    expect(screen.getByTestId('dispatch-log')).not.toHaveTextContent('round=2');
    fireEvent.click(screen.getByTestId('dispatch-replay-play'));          // pause
    act(() => { vi.advanceTimersByTime(4000); });
    expect(screen.getByTestId('dispatch-step-b')).toHaveAttribute('data-phase', 'running');
    fireEvent.click(screen.getByText('4×'));
    fireEvent.click(screen.getByTestId('dispatch-replay-play'));
    act(() => { vi.advanceTimersByTime(5000); });                         // + 20 s → the end, stops there
    expect(screen.getByTestId('dispatch-step-d')).toHaveAttribute('data-phase', 'done');
    expect(screen.getByLabelText('dispatch.replayAgain')).toBeInTheDocument();
    // no stop buttons in a replay
    expect(screen.queryByLabelText('dispatch.stop')).toBeNull();
  });

  it('opens from the answer, plays without touching the store, and closes', () => {
    vi.useFakeTimers();
    const store = storeWith(undefined);
    const spy = vi.spyOn(store, 'dispatch');
    render(<Provider store={store}>
      <DispatchReplayButton turnId="t1" plan={finished} />
      <DispatchCanvasGate><div data-testid="charts-canvas">charts</div></DispatchCanvasGate>
    </Provider>);
    expect(screen.getByTestId('charts-canvas')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('dispatch-replay-open'));
    expect(getReplay()?.turnId).toBe('t1');
    expect(screen.getByTestId('dispatch-panel')).toHaveAttribute('data-replay', 'true');
    fireEvent.click(screen.getByTestId('dispatch-replay-play'));
    act(() => { vi.advanceTimersByTime(12_000); });
    expect(spy).not.toHaveBeenCalled();                                   // the replay clock never reaches Redux
    fireEvent.click(screen.getByTestId('dispatch-replay-close'));
    expect(screen.getByTestId('charts-canvas')).toBeInTheDocument();
  });

  it('offers no replay for a run without sub-agents or one still going', () => {
    const { container } = render(<DispatchReplayButton turnId="t" plan={plan} />);
    expect(container).toBeEmptyDOMElement();
    const { container: c2 } = render(<DispatchReplayButton turnId="t" plan={[{ id: 'x', title: 'X', status: 'done' }]} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it('gives the canvas back to a live run', () => {
    render(<Provider store={storeWith(plan)}>
      <DispatchReplayButton turnId="t1" plan={finished} />
      <DispatchCanvasGate><div data-testid="charts-canvas">charts</div></DispatchCanvasGate>
    </Provider>);
    fireEvent.click(screen.getByTestId('dispatch-replay-open'));
    expect(getReplay()).toBeNull();
    expect(screen.getByTestId('dispatch-panel')).not.toHaveAttribute('data-replay');
  });
});
