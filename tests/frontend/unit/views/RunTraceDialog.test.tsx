import React from 'react';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiRequest = vi.fn();

vi.mock('react-i18next', () => ({
    initReactI18next: { type: '3rdParty', init: () => {} },
    useTranslation: () => ({
        t: (key: string, params?: Record<string, any>) => {
            if (params?.defaultValue && key.startsWith('trace.labels.')) {
                return String(params.defaultValue).replace(/\{\{(\w+)\}\}/g, (_: string, k: string) => String(params[k] ?? ''));
            }
            if (key === 'trace.callsShort') return `${params?.n} calls`;
            if (key === 'trace.cacheHit') return `${params?.pct}% cached`;
            return key;
        },
    }),
}));

vi.mock('../../../../src/app/apiClient', () => ({
    apiRequest: (...args: any[]) => apiRequest(...args),
}));

vi.mock('../../../../src/app/utils', () => ({
    getUrls: () => ({ TRACES: '/api/traces' }),
}));

import { RunTraceDialog } from '../../../../src/views/RunTraceDialog';

const summary = {
    duration_ms: 4200, status: 'success', llm_calls: 3, llm_ms: 3500, prompt_tokens: 30_000, cached_tokens: 27_000,
    completion_tokens: 500, by_model: { 'big-model': { calls: 1, prompt: 10_000, cached: 9_000, completion: 200 },
                                        'small-model': { calls: 2, prompt: 20_000, cached: 18_000, completion: 300 } },
    tiers: { strong: 1, fast: 2 }, tool_calls: 1, charts: 1, chart_checks: 0, subagents: 0, asks: 0, errors: 0,
};

const trace = {
    run_id: 'abc123def', question: 'How are sales split?', started_at: '2026-10-11T10:00:00', summary,
    segments: [{
        kind: 'start', status: 'success', duration_ms: 4200, events: [
            { type: 'llm_call', t_ms: 1200, model: 'big-model', tier: 'strong', reason: 'read the question',
              duration_ms: 1100, prompt_tokens: 10_000, completion_tokens: 200, tool_calls: ['execute_python_script'] },
            { type: 'tool_start', t_ms: 1210, tool: 'execute_python_script', purpose: 'total' },
            { type: 'tool_result', t_ms: 1500, tool: 'execute_python_script', status: 'ok', stdout: '100' },
            { type: 'completion', t_ms: 4200, status: 'success', summary: 'Total 100.' },
        ],
    }],
};

describe('RunTraceDialog', () => {
    beforeEach(() => {
        apiRequest.mockReset();
        apiRequest.mockImplementation(async (url: string) => (
            url.startsWith('/api/traces?')
                ? { data: { traces: [{ ...trace, segments: 1 }] } }
                : { data: trace }
        ));
    });

    it('lists runs and shows the selected one as a timeline', async () => {
        render(<RunTraceDialog open hideTrigger />);
        await waitFor(() => expect(screen.getAllByText('How are sales split?').length).toBeGreaterThan(1));
        expect(apiRequest).toHaveBeenCalledWith('/api/traces/abc123def');
        expect(screen.getByText('90% cached')).toBeInTheDocument();
        expect(screen.getByText('small-model')).toBeInTheDocument();
        const rows = screen.getAllByTestId('trace-row');
        expect(rows.map(r => r.textContent)).toEqual([
            expect.stringContaining('big-model (read the question) → execute_python_script'),
            expect.stringContaining('execute_python_script: total'),
            expect.stringContaining('final answer (success)'),
        ]);
        fireEvent.click(rows[1]);
        expect(screen.getByText(/"stdout": "100"/)).toBeInTheDocument();
    });

    it('says when there is nothing yet', async () => {
        apiRequest.mockResolvedValue({ data: { traces: [] } });
        render(<RunTraceDialog open hideTrigger />);
        await waitFor(() => expect(screen.getByText('trace.empty')).toBeInTheDocument());
    });
});
