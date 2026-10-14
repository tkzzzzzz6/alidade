import { describe, expect, it, vi, beforeEach } from 'vitest';

const apiRequest = vi.fn();
vi.mock('../../../../src/app/apiClient', () => ({ apiRequest: (...args: any[]) => apiRequest(...args) }));

import {
    compactSettings, sameSettings, stopSubagent, subagentIsLive, toRequestSettings,
} from '../../../../src/app/agentSettings';
import { alidadeReducer, dfActions } from '../../../../src/app/dfSlice';

describe('agent settings', () => {
    beforeEach(() => { apiRequest.mockReset(); });

    it('sends only what the user set, in wire names, clamped', () => {
        expect(toRequestSettings(undefined)).toBeUndefined();
        expect(toRequestSettings({})).toBeUndefined();
        expect(toRequestSettings({ subagents: false })).toEqual({ subagents: false });
        expect(toRequestSettings({
            subagents: true, maxParallel: 9, subagentMaxCharts: 0, subagentTimeoutS: 5, planAutoAdvance: false,
        })).toEqual({
            subagents: true, max_parallel: 4, subagent_max_charts: 1, subagent_timeout_s: 30, plan_auto_advance: false,
        });
        expect(toRequestSettings({ maxParallel: NaN })).toBeUndefined();
    });

    it('stores "server default" as absence', () => {
        expect(compactSettings({ subagents: undefined, maxParallel: 2 })).toEqual({ maxParallel: 2 });
        expect(sameSettings({ subagents: undefined }, {})).toBe(true);
        expect(sameSettings({ subagents: true }, {})).toBe(false);
        expect(sameSettings(undefined, undefined)).toBe(true);
    });

    it('knows which sub-agent states are still going', () => {
        expect(['queued', 'running'].map(subagentIsLive)).toEqual([true, true]);
        expect(['success', 'timeout', 'cancelled', 'failed', undefined].map(subagentIsLive))
            .toEqual([false, false, false, false, false]);
    });

    it('stops one sub-agent through the API', async () => {
        apiRequest.mockResolvedValue({ data: { stopped: true, running: [] } });
        await expect(stopSubagent('run1', 's2')).resolves.toBe(true);
        const [url, init] = apiRequest.mock.calls[0];
        expect(url).toContain('/api/agent/subagent-stop');
        expect(init.method).toBe('POST');
        expect(JSON.parse(init.body)).toEqual({ run_id: 'run1', step_id: 's2' });
    });

    it('setConfig merges, so saving other settings keeps the agent settings', () => {
        let state = alidadeReducer(undefined, { type: '@@init' });
        state = alidadeReducer(state, dfActions.setConfig({ agentSettings: { subagents: true } }));
        state = alidadeReducer(state, dfActions.setConfig({ defaultChartWidth: 500 }));
        expect(state.config.agentSettings).toEqual({ subagents: true });
        expect(state.config.defaultChartWidth).toBe(500);
        expect(state.config.formulateTimeoutSeconds).toBe(180);
    });
});
