import React from 'react';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  initReactI18next: { type: '3rdParty', init: () => {} },
  useTranslation: () => ({
    t: (key: string, params?: Record<string, any>) => {
      const labels: Record<string, string> = {
        'dataThread.thinking': 'Thinking...',
        'chartRec.queue.edit': 'Edit',
        'chartRec.queue.save': 'Save',
        'chartRec.queue.cancelEdit': 'Cancel',
        'chartRec.queue.remove': 'Remove',
        'chartRec.queue.resume': 'Send next',
        'chartRec.queue.editAria': 'Edit queued message',
        'chartRec.queue.waitingRun': 'sends after this run',
        'chartRec.queue.waitingAnswer': 'answer first',
        'chartRec.queue.held': 'paused',
        'dataLoading.stopTooltip': 'Stop',
      };
      if (key === 'chartRec.queue.count') return `${params?.count} queued`;
      if (key === 'dataThread.planProgress') return `${params?.done}/${params?.total}`;
      return labels[key] || key;
    },
  }),
}));

import { AgentStatusLine, QueuedMessageList, latestStepText } from '../../../../src/views/AgentRunStatus';

const queue = [
  { id: 'q1', text: 'Compare regions' },
  { id: 'q2', text: 'Then show the trend' },
];

describe('AgentStatusLine', () => {
  it('shows only the latest step, without its status marker', () => {
    expect(latestStepText('✓ Inspecting data\x1E⚠ Chart self-check: too many bars')).toBe('Chart self-check: too many bars');
    expect(latestStepText(undefined)).toBe('');
  });

  it('renders the step, plan progress and a stop button', () => {
    const onStop = vi.fn();
    render(
      <AgentStatusLine
        runningPlan={'✓ Reading chart\x1ECreating chart: sales by region'}
        agentPlan={[
          { id: 'step-1', title: 'a', status: 'done' },
          { id: 'step-2', title: 'b', status: 'in_progress' },
        ]}
        startedAt={null}
        onStop={onStop}
      />,
    );
    const line = screen.getByTestId('agent-status-line');
    expect(line).toHaveTextContent('Creating chart: sales by region');
    expect(line).toHaveTextContent('1/2');
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it('falls back to a thinking label', () => {
    render(<AgentStatusLine startedAt={null} />);
    expect(screen.getByTestId('agent-status-line')).toHaveTextContent('Thinking...');
  });
});

describe('QueuedMessageList', () => {
  it('lists queued messages with the queue state', () => {
    render(<QueuedMessageList queue={queue} status="waiting-run" onUpdate={vi.fn()} onRemove={vi.fn()} />);
    expect(screen.getByText(/2 queued/)).toHaveTextContent('sends after this run');
    expect(screen.getAllByTestId('queued-message')).toHaveLength(2);
  });

  it('removes a message', () => {
    const onRemove = vi.fn();
    render(<QueuedMessageList queue={queue} status="waiting-run" onUpdate={vi.fn()} onRemove={onRemove} />);
    const second = screen.getAllByTestId('queued-message')[1];
    fireEvent.click(within(second).getByRole('button', { name: 'Remove' }));
    expect(onRemove).toHaveBeenCalledWith('q2');
  });

  it('edits a message inline and saves with Enter', () => {
    const onUpdate = vi.fn();
    render(<QueuedMessageList queue={queue} status="waiting-run" onUpdate={onUpdate} onRemove={vi.fn()} />);
    const first = screen.getAllByTestId('queued-message')[0];
    fireEvent.click(within(first).getByRole('button', { name: 'Edit' }));
    const input = screen.getByRole('textbox', { name: 'Edit queued message' });
    fireEvent.change(input, { target: { value: 'Compare regions by profit' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onUpdate).toHaveBeenCalledWith('q1', 'Compare regions by profit');
  });

  it('cancels an edit with Escape', () => {
    const onUpdate = vi.fn();
    render(<QueuedMessageList queue={queue} status="waiting-run" onUpdate={onUpdate} onRemove={vi.fn()} />);
    fireEvent.click(within(screen.getAllByTestId('queued-message')[0]).getByRole('button', { name: 'Edit' }));
    const input = screen.getByRole('textbox', { name: 'Edit queued message' });
    fireEvent.change(input, { target: { value: 'changed' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onUpdate).not.toHaveBeenCalled();
    expect(screen.getByText('Compare regions')).toBeInTheDocument();
  });

  it('offers to resume only when held', () => {
    const onResume = vi.fn();
    const { rerender } = render(
      <QueuedMessageList queue={queue} status="waiting-answer" onUpdate={vi.fn()} onRemove={vi.fn()} onResume={onResume} />,
    );
    expect(screen.queryByRole('button', { name: 'Send next' })).toBeNull();
    expect(screen.getByText(/2 queued/)).toHaveTextContent('answer first');
    rerender(<QueuedMessageList queue={queue} status="held" onUpdate={vi.fn()} onRemove={vi.fn()} onResume={onResume} />);
    fireEvent.click(screen.getByRole('button', { name: 'Send next' }));
    expect(onResume).toHaveBeenCalledOnce();
  });

  it('renders nothing for an empty queue', () => {
    const { container } = render(<QueuedMessageList queue={[]} status="waiting-run" onUpdate={vi.fn()} onRemove={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
