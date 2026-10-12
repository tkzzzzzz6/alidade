import React from 'react';
import '@testing-library/jest-dom/vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  initReactI18next: { type: '3rdParty', init: () => {} },
  useTranslation: () => ({
    t: (key: string, params?: Record<string, any>) => {
      if (key === 'dataThread.planTitle') return 'Plan';
      if (key === 'dataThread.planProgress') return `${params?.done}/${params?.total} done`;
      if (key === 'dataThread.stepTag') return `Step ${params?.n}`;
      if (key.startsWith('dataThread.subagentStatus.')) return key.split('.').pop()!;
      if (key === 'dataThread.subagentCharts') return `${params?.count} charts`;
      if (key === 'dataThread.subagentStopAria') return `Stop ${params?.title}`;
      return key;
    },
  }),
}));

import { AgentPlanChecklist, planProgress } from '../../../../src/views/AgentPlanChecklist';

const steps = [
  { id: 'step-1', title: 'Sales by region', status: 'done' as const },
  { id: 'step-2', title: 'Trend over time', status: 'in_progress' as const },
  { id: 'step-3', title: 'Forecast', status: 'skipped' as const },
  { id: 'step-4', title: 'Write summary', status: 'pending' as const },
];

describe('AgentPlanChecklist', () => {
  it('counts progress without skipped steps', () => {
    expect(planProgress(steps)).toEqual({ done: 1, total: 3 });
  });

  it('renders every step with its status', () => {
    render(<AgentPlanChecklist steps={steps} active />);
    expect(screen.getByText('Plan')).toBeInTheDocument();
    expect(screen.getByText('1/3 done')).toBeInTheDocument();
    const items = screen.getAllByRole('listitem');
    expect(items.map(item => item.getAttribute('data-status')))
      .toEqual(['done', 'in_progress', 'skipped', 'pending']);
    expect(screen.getByText('Forecast')).toHaveStyle({ textDecoration: 'line-through' });
  });

  it('collapses to a one-line summary when compact', () => {
    render(<AgentPlanChecklist steps={steps} compact />);
    expect(screen.getByTestId('agent-plan-compact')).toHaveTextContent('1/3 done');
    expect(screen.queryByRole('listitem')).toBeNull();
  });

  it('renders nothing without steps', () => {
    const { container } = render(<AgentPlanChecklist steps={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  describe('sub-agent steps', () => {
    const running = [
      { id: 's1', title: 'Margin by line', status: 'in_progress' as const,
        subagent: { status: 'running', run_id: 'run1', started_at: Date.now() - 12_000 } },
      { id: 's2', title: 'Churn by region', status: 'done' as const,
        subagent: { status: 'success', run_id: 'run1', duration_s: 14.2, charts: 2,
                    findings: 'North churns 4.1%.\nSouth 2.3%.' } },
      { id: 's3', title: 'Relate the two', status: 'pending' as const },
    ];
    const lines = ['✓ [Step 1] dataThread.inspectingData sales', '[Step 1] dataThread.runningCode margin by line',
                   '✓ [Step 2] other'].join('\x1E');

    it('shows what a running sub-agent is doing and lets the user stop it', async () => {
      const onStop = vi.fn().mockResolvedValue(true);
      render(<AgentPlanChecklist steps={running} active runningPlan={lines} onStopStep={onStop} />);
      const row = screen.getByTestId('subagent-s1');
      expect(row).toHaveAttribute('data-subagent-status', 'running');
      expect(row).toHaveTextContent('running');
      expect(row).toHaveTextContent(/1[0-9]s/);
      expect(row).toHaveTextContent('dataThread.runningCode margin by line');
      // only the running step can be stopped; three steps are still three list items
      expect(screen.getAllByRole('listitem')).toHaveLength(3);
      const stop = screen.getByRole('button', { name: 'Stop Margin by line' });
      await act(async () => { fireEvent.click(stop); });
      expect(onStop).toHaveBeenCalledWith(running[0]);
      expect(screen.getByRole('button', { name: 'Stop Margin by line' })).toBeDisabled();
      expect(row).toHaveTextContent('dataThread.subagentStopping');
    });

    it('re-enables the stop button when the step was no longer running', async () => {
      const onStop = vi.fn().mockResolvedValue(false);
      render(<AgentPlanChecklist steps={running} active runningPlan={lines} onStopStep={onStop} />);
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop Margin by line' })); });
      expect(screen.getByRole('button', { name: 'Stop Margin by line' })).not.toBeDisabled();
    });

    it('keeps a finished sub-agent\'s findings behind a toggle', () => {
      render(<AgentPlanChecklist steps={running} active runningPlan={lines} />);
      const done = screen.getByTestId('subagent-s2');
      expect(done).toHaveTextContent('success · 14s · 2 charts');
      expect(screen.queryByTestId('subagent-findings-s2')).toBeNull();
      const toggle = screen.getByRole('button', { name: 'dataThread.subagentShow' });
      expect(toggle).toHaveAttribute('aria-expanded', 'false');
      expect(toggle).toHaveTextContent('');                 // an icon only, no label text
      fireEvent.click(toggle);
      expect(screen.getByTestId('subagent-findings-s2')).toHaveTextContent('North churns 4.1%.');
      expect(screen.getByRole('button', { name: 'dataThread.subagentHide' })).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getByText('dataThread.subagentSummary')).toBeInTheDocument();
    });

    it('after the run there is nothing to stop', () => {
      render(<AgentPlanChecklist steps={running} onStopStep={vi.fn()} />);
      expect(screen.queryByRole('button', { name: /^Stop / })).toBeNull();
      expect(screen.getByTestId('subagent-s1')).not.toHaveTextContent('margin by line');
    });
  });
});
