// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * The canvas during a sub-agent round: while steps are being worked on in
 * parallel the canvas shows the dispatch panel, with a switch back to the
 * charts. When the round is over it goes back to the charts by itself (the
 * switch stays until the run ends). Without a running sub-agent round the
 * canvas is rendered exactly as before.
 *
 * A finished answer's dispatch can be replayed here too ("show how it was
 * dispatched" under the answer); a live run takes the canvas over from a
 * replay.
 */

import React, { FC, ReactNode, useEffect, useState } from 'react';
import { useSelector } from 'react-redux';
import { useTranslation } from 'react-i18next';
import { Box, Button, ToggleButton, ToggleButtonGroup } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AccountTreeOutlinedIcon from '@mui/icons-material/AccountTreeOutlined';

import { AlidadeState, dfSelectors } from '../app/dfSlice';
import { AgentPlanStep } from '../components/ComponentType';
import { buildDispatchView, canReplay, hasLiveRound } from '../app/dispatchView';
import { stopSubagent } from '../app/agentSettings';
import { closeReplay, openReplay, useReplay } from '../app/dispatchReplay';
import { DispatchPanel } from './DispatchPanel';

type Mode = 'dispatch' | 'charts';

/** The running draft whose plan has steps handed to sub-agents, if any. */
function useDispatchingDraft() {
    return useSelector((state: AlidadeState) => state.draftNodes.find(d =>
        d.derive?.status === 'running' && (d.derive.agentPlan || []).some(s => s.subagent)));
}

const stopStep = async (step: AgentPlanStep) => {
    const runId = step.subagent?.run_id;
    if (!runId) return;
    try { await stopSubagent(runId, step.id); } catch { /* it had already finished */ }
};

export const DispatchCanvasGate: FC<{ children: ReactNode }> = ({ children }) => {
    const { t } = useTranslation();
    const draft = useDispatchingDraft();
    const model = useSelector(dfSelectors.getActiveModel);
    const [choice, setChoice] = useState<{ key: string; mode: Mode } | null>(null);

    const replay = useReplay();
    const plan = draft?.derive?.agentPlan;
    const hasLive = !!(draft && plan);
    useEffect(() => { if (hasLive && replay) closeReplay(); }, [hasLive, replay]);
    useEffect(() => () => closeReplay(), []);

    if (!draft || !plan) {
        if (!replay) return <>{children}</>;
        const close = (
            <Button size="small" variant="outlined" startIcon={<CloseIcon sx={{ fontSize: 14 }} />} onClick={closeReplay}
                data-testid="dispatch-replay-close" sx={{ py: 0.25, fontSize: 12, textTransform: 'none' }}>
                {t('dispatch.replayClose')}
            </Button>
        );
        return (
            <Box sx={{ width: '100%', height: '100%', overflow: 'hidden' }}>
                <DispatchPanel key={replay.turnId} plan={replay.plan} runActive={false} toggle={close} replay />
            </Box>
        );
    }

    const live = hasLiveRound(plan);
    const round = Math.max(...plan.map(s => s.subagent?.round ?? (s.subagent ? 1 : 0)));
    // a new round, or the round finishing, resets the choice to the default
    const key = `${draft.id}:${round}:${live ? 'live' : 'over'}`;
    const mode: Mode = choice?.key === key ? choice.mode : live ? 'dispatch' : 'charts';
    const charts = buildDispatchView(plan, Date.now(), true)?.charts ?? 0;

    const toggle = (
        <ToggleButtonGroup size="small" exclusive value={mode} data-testid="dispatch-toggle"
            onChange={(_, value: Mode | null) => value && setChoice({ key, mode: value })}
            sx={{ bgcolor: 'background.paper', '& .MuiToggleButton-root': { py: 0.25, px: 1.5, fontSize: 12, textTransform: 'none' } }}>
            <ToggleButton value="dispatch">{t('dispatch.viewDispatch')}</ToggleButton>
            <ToggleButton value="charts">{t('dispatch.viewCharts', { n: charts })}</ToggleButton>
        </ToggleButtonGroup>
    );

    if (mode === 'dispatch') {
        const lines = (draft.derive?.runningPlan || '').split('\x1E').map(s => s.trim()).filter(Boolean);
        return (
            <Box sx={{ width: '100%', height: '100%', overflow: 'hidden' }}>
                <DispatchPanel plan={plan} runActive runningLines={lines} modelName={model?.model}
                    toggle={toggle} onStopStep={stopStep} />
            </Box>
        );
    }
    return (
        <Box sx={{ position: 'relative', width: '100%', height: '100%', display: 'flex', minHeight: 0, minWidth: 0 }}>
            {children}
            <Box sx={{ position: 'absolute', top: 8, right: 12, zIndex: 5 }}>{toggle}</Box>
        </Box>
    );
};

/** "Show how it was dispatched" under a finished answer that used sub-agents. */
export const DispatchReplayButton: FC<{ turnId: string; plan?: AgentPlanStep[] }> = ({ turnId, plan }) => {
    const { t } = useTranslation();
    const replay = useReplay();
    if (!plan || !canReplay(plan)) return null;
    const open = replay?.turnId === turnId;
    return (
        <Button size="small" data-testid="dispatch-replay-open" startIcon={<AccountTreeOutlinedIcon sx={{ fontSize: 14 }} />}
            onClick={(e) => { e.stopPropagation(); if (open) closeReplay(); else openReplay({ turnId, plan }); }}
            sx={{ py: 0, fontSize: 12, textTransform: 'none', color: open ? 'primary.main' : 'text.secondary' }}>
            {open ? t('dispatch.replayClose') : t('dispatch.replayOpen')}
        </Button>
    );
};
