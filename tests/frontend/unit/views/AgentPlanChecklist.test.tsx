import React from 'react';
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  initReactI18next: { type: '3rdParty', init: () => {} },
  useTranslation: () => ({
    t: (key: string, params?: Record<string, any>) => {
      if (key === 'dataThread.planTitle') return 'Plan';
      if (key === 'dataThread.planProgress') return `${params?.done}/${params?.total} done`;
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
});
