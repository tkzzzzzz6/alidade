// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * "Agent" part of the settings dialog: parallel sub-agents and plan behaviour.
 *
 * Each setting can follow the server default (its env var) or be set here;
 * the server default is shown next to the "default" choice so it is clear
 * what an unset value means.
 */

import React, { FC, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Box, FormControl, InputLabel, MenuItem, Select, Typography } from '@mui/material';
import { AGENT_SETTING_LIMITS, AgentSettings, RunSettingsWire, compactSettings, fetchRunSettingsInfo } from '../app/agentSettings';
import { textVar } from '../app/layout';

const DEFAULT = '__default__';

function range(min: number, max: number, step = 1): number[] {
    const out: number[] = [];
    for (let v = min; v <= max; v += step) out.push(v);
    return out;
}

const TIMEOUT_CHOICES = [30, 60, 120, 180, 300, 600];

export const AgentSettingsSection: FC<{
    value: AgentSettings;
    onChange: (next: AgentSettings) => void;
    /** for tests; fetched from the server otherwise */
    serverDefaults?: RunSettingsWire | null;
}> = ({ value, onChange, serverDefaults }) => {
    const { t } = useTranslation();
    const [defaults, setDefaults] = useState<RunSettingsWire | null>(serverDefaults ?? null);

    useEffect(() => {
        if (serverDefaults !== undefined) return;
        let alive = true;
        fetchRunSettingsInfo()
            .then(info => { if (alive) setDefaults(info.defaults); })
            .catch(() => { /* offline / older server: labels just omit the default */ });
        return () => { alive = false; };
    }, [serverDefaults]);

    const set = (patch: Partial<AgentSettings>) => onChange(compactSettings({ ...value, ...patch }));
    const onOff = (v: boolean | undefined) => (v === undefined ? '' : v ? t('config.agent.on') : t('config.agent.off'));
    const defaultLabel = (shown: string) =>
        shown ? t('config.agent.serverDefaultIs', { value: shown }) : t('config.agent.serverDefault');

    const boolSelect = (key: 'subagents' | 'planAutoAdvance', serverValue: boolean | undefined, label: string, hint: string) => {
        const current = value[key];
        return (
            <Box>
                <FormControl fullWidth size="small">
                    <InputLabel id={`agent-${key}-label`} sx={{ fontSize: textVar.md }}>{label}</InputLabel>
                    <Select
                        labelId={`agent-${key}-label`}
                        data-testid={`agent-setting-${key}`}
                        value={current === undefined ? DEFAULT : current ? 'on' : 'off'}
                        label={label}
                        onChange={e => {
                            const v = e.target.value;
                            set({ [key]: v === DEFAULT ? undefined : v === 'on' });
                        }}
                        sx={{ fontSize: textVar.md }}
                    >
                        <MenuItem value={DEFAULT}>{defaultLabel(onOff(serverValue))}</MenuItem>
                        <MenuItem value="on">{t('config.agent.on')}</MenuItem>
                        <MenuItem value="off">{t('config.agent.off')}</MenuItem>
                    </Select>
                </FormControl>
                <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: 'block' }}>{hint}</Typography>
            </Box>
        );
    };

    const numberSelect = (
        key: 'maxParallel' | 'subagentMaxCharts' | 'subagentTimeoutS',
        choices: number[], serverValue: number | undefined, label: string, unit: (n: number) => string,
    ) => (
        <FormControl size="small" disabled={subagentsOff} sx={{ minWidth: 0 }}>
            <InputLabel id={`agent-${key}-label`} sx={{ fontSize: textVar.md }}>{label}</InputLabel>
            <Select
                labelId={`agent-${key}-label`}
                data-testid={`agent-setting-${key}`}
                value={value[key] === undefined ? DEFAULT : String(value[key])}
                label={label}
                onChange={e => {
                    const v = e.target.value;
                    set({ [key]: v === DEFAULT ? undefined : Number(v) });
                }}
                // narrow field: a short "2 (default)" instead of the full menu wording
                renderValue={v => v === DEFAULT
                    ? (serverValue === undefined ? t('config.agent.serverDefault') : t('config.agent.defaultShort', { value: unit(serverValue) }))
                    : unit(Number(v))}
                sx={{ fontSize: textVar.md }}
            >
                <MenuItem value={DEFAULT}>{defaultLabel(serverValue === undefined ? '' : unit(serverValue))}</MenuItem>
                {choices.map(n => <MenuItem key={n} value={String(n)}>{unit(n)}</MenuItem>)}
            </Select>
        </FormControl>
    );

    // the sub-agent details matter only when sub-agents can be on
    const effectiveSubagents = value.subagents ?? defaults?.subagents ?? false;
    const subagentsOff = !effectiveSubagents;
    const L = AGENT_SETTING_LIMITS;

    return (
        <Box data-testid="agent-settings" sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {boolSelect('subagents', defaults?.subagents, t('config.agent.subagents'), t('config.agent.subagentsHint'))}
            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1.5 }}>
                {numberSelect('maxParallel', range(L.maxParallel.min, L.maxParallel.max), defaults?.max_parallel,
                    t('config.agent.maxParallel'), n => String(n))}
                {numberSelect('subagentMaxCharts', range(L.subagentMaxCharts.min, L.subagentMaxCharts.max),
                    defaults?.subagent_max_charts, t('config.agent.maxCharts'), n => String(n))}
                {numberSelect('subagentTimeoutS', TIMEOUT_CHOICES, defaults?.subagent_timeout_s,
                    t('config.agent.timeout'), n => t('config.agent.seconds', { n: Math.round(n) }))}
            </Box>
            {boolSelect('planAutoAdvance', defaults?.plan_auto_advance, t('config.agent.autoAdvance'), t('config.agent.autoAdvanceHint'))}
        </Box>
    );
};
