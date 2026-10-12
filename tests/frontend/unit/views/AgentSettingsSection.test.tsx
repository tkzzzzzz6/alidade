import React from 'react';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  initReactI18next: { type: '3rdParty', init: () => {} },
  useTranslation: () => ({
    t: (key: string, params?: Record<string, any>) => {
      if (key === 'config.agent.serverDefaultIs') return `default (${params?.value})`;
      if (key === 'config.agent.on') return 'On';
      if (key === 'config.agent.off') return 'Off';
      if (key === 'config.agent.seconds') return `${params?.n} s`;
      if (key === 'config.agent.defaultShort') return `${params?.value} (default)`;
      return key;
    },
  }),
}));
vi.mock('../../../../src/app/apiClient', () => ({ apiRequest: vi.fn() }));

import { AgentSettingsSection } from '../../../../src/views/AgentSettingsSection';

const defaults = { subagents: false, max_parallel: 3, subagent_max_charts: 2, subagent_timeout_s: 180, plan_auto_advance: true };

function pick(testId: string, option: string) {
  const select = within(screen.getByTestId(testId)).getByRole('combobox');
  fireEvent.mouseDown(select);
  fireEvent.click(within(screen.getByRole('listbox')).getByText(option));
}

describe('AgentSettingsSection', () => {
  it('shows the server default and stores an explicit choice', () => {
    const onChange = vi.fn();
    render(<AgentSettingsSection value={{}} onChange={onChange} serverDefaults={defaults} />);
    expect(screen.getByTestId('agent-setting-subagents')).toHaveTextContent('default (Off)');
    pick('agent-setting-subagents', 'On');
    expect(onChange).toHaveBeenLastCalledWith({ subagents: true });
  });

  it('sub-agent details are disabled while sub-agents are off', () => {
    render(<AgentSettingsSection value={{}} onChange={vi.fn()} serverDefaults={defaults} />);
    const parallel = within(screen.getByTestId('agent-setting-maxParallel')).getByRole('combobox');
    expect(parallel).toHaveAttribute('aria-disabled', 'true');
  });

  it('numbers can be set and returned to the default', () => {
    const onChange = vi.fn();
    render(<AgentSettingsSection value={{ subagents: true, maxParallel: 2 }} onChange={onChange} serverDefaults={defaults} />);
    pick('agent-setting-subagentTimeoutS', '60 s');
    expect(onChange).toHaveBeenLastCalledWith({ subagents: true, maxParallel: 2, subagentTimeoutS: 60 });
    pick('agent-setting-maxParallel', 'default (3)');
    expect(onChange).toHaveBeenLastCalledWith({ subagents: true });
  });
});
