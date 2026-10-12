// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * The analyst's plan as a compact checklist (plan-then-execute).
 *
 * Rendered live above the thinking steps while a run is in progress, and on
 * the run's closing / question turn so the thread keeps a record of what the
 * agent set out to do and what it finished.
 *
 * A step handed to a sub-agent shows that sub-agent: live, its status, how
 * long it has been working, what it is doing right now and a button to stop
 * just that step; afterwards, how it ended and (on click) what it found.
 */

import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Box, CircularProgress, IconButton, Tooltip, Typography, useTheme } from '@mui/material';
import StopIcon from '@mui/icons-material/Stop';
import ChevronRightIcon from '@mui/icons-material/ChevronRight';
import { alpha } from '@mui/material/styles';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import RemoveCircleOutlineIcon from '@mui/icons-material/RemoveCircleOutline';
import AutorenewIcon from '@mui/icons-material/Autorenew';
import { AgentPlanStep, SubagentRecord } from '../components/ComponentType';
import { iconVar, textVar } from '../app/layout';
import { subagentIsLive } from '../app/agentSettings';
import { latestActivity, lineTag } from './subagentEvents';

export function planProgress(steps: AgentPlanStep[]): { done: number; total: number } {
    const counted = steps.filter(s => s.status !== 'skipped');
    return { done: counted.filter(s => s.status === 'done').length, total: counted.length };
}

const StepIcon: React.FC<{ status: AgentPlanStep['status']; active: boolean }> = ({ status, active }) => {
    const theme = useTheme();
    const sx = { width: 12, height: 12, flexShrink: 0, mt: '2px' };
    if (status === 'done') return <CheckCircleIcon sx={{ ...sx, color: theme.palette.success.main }} />;
    if (status === 'skipped') return <RemoveCircleOutlineIcon sx={{ ...sx, color: theme.palette.text.disabled }} />;
    if (status === 'in_progress') {
        return (
            <AutorenewIcon sx={{
                ...sx,
                color: theme.palette.primary.main,
                ...(active ? {
                    animation: 'df-plan-spin 1.6s linear infinite',
                    '@keyframes df-plan-spin': { from: { transform: 'rotate(0deg)' }, to: { transform: 'rotate(360deg)' } },
                } : {}),
            }} />
        );
    }
    return <RadioButtonUncheckedIcon sx={{ ...sx, color: theme.palette.text.disabled }} />;
};

function useNow(enabled: boolean): number {
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        if (!enabled) return;
        const timer = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, [enabled]);
    return now;
}

function seconds(s: number | null | undefined): string {
    if (s == null || !Number.isFinite(s)) return '';
    return s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

const STATUS_COLOR: Record<string, 'success' | 'warning' | 'error' | 'primary' | 'text'> = {
    queued: 'text', running: 'primary', success: 'success', budget_exhausted: 'success',
    timeout: 'warning', cancelled: 'text', stopped: 'text', failed: 'error',
};

/** One sub-agent's line under its plan step. */
const SubagentDetail: React.FC<{
    step: AgentPlanStep;
    record: SubagentRecord;
    stepNumber: number;
    active: boolean;
    now: number;
    runningLines: string[];
    onStop?: (step: AgentPlanStep) => Promise<boolean> | void;
}> = ({ step, record, stepNumber, active, now, runningLines, onStop }) => {
    const { t } = useTranslation();
    const theme = useTheme();
    const [open, setOpen] = useState(false);
    const [stopping, setStopping] = useState(false);
    const live = active && subagentIsLive(record.status);
    const statusText = t(`dataThread.subagentStatus.${record.status}`, { defaultValue: record.status });
    const color = STATUS_COLOR[record.status] ?? 'text';
    const tone = color === 'text' ? theme.palette.text.secondary : theme.palette[color].main;
    const elapsed = live && record.started_at ? (now - record.started_at) / 1000 : record.duration_s;
    const activity = live ? latestActivity(runningLines, lineTag(t('dataThread.stepTag', { n: stepNumber }))) : '';
    const hasDetail = !live && !!(record.findings || record.error);
    const meta = [
        statusText,
        seconds(elapsed),
        !live && record.charts ? t('dataThread.subagentCharts', { count: record.charts }) : '',
    ].filter(Boolean).join(' · ');

    const stop = async () => {
        if (!onStop || stopping) return;
        setStopping(true);
        try {
            const ok = await onStop(step);
            if (ok === false) setStopping(false);
        } catch {
            setStopping(false);
        }
    };

    return (
        <Box data-testid={`subagent-${step.id}`} data-subagent-status={record.status} sx={{ pl: '20px', minWidth: 0 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, minWidth: 0 }}>
                {live && <CircularProgress size={9} thickness={6} sx={{ color: tone, flexShrink: 0 }} />}
                <Typography title={meta} sx={{
                    fontSize: textVar.xxs, color: tone, fontVariantNumeric: 'tabular-nums',
                    // shrinks (with "…") when the panel is narrow, so the icons stay visible;
                    // while working, the activity text gives way first
                    flexShrink: activity ? 0 : 1, minWidth: 0,
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                    {t('dataThread.subagentBadge')} · {stopping && live ? t('dataThread.subagentStopping') : meta}
                </Typography>
                {activity && (
                    <Typography title={activity} sx={{
                        fontSize: textVar.xxs, color: 'text.disabled', minWidth: 0, flex: 1,
                        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    }}>
                        {activity}
                    </Typography>
                )}
                {live && onStop && (
                    <Tooltip title={t('dataThread.subagentStop')}>
                        <span style={{ marginLeft: 'auto', flexShrink: 0 }}>
                            <IconButton
                                size="small"
                                aria-label={t('dataThread.subagentStopAria', { title: step.title })}
                                disabled={stopping}
                                onClick={stop}
                                sx={{ width: 16, height: 16, p: 0, color: 'error.main' }}
                            >
                                <StopIcon sx={{ fontSize: iconVar.xs }} />
                            </IconButton>
                        </span>
                    </Tooltip>
                )}
                {hasDetail && (
                    <Tooltip title={open ? t('dataThread.subagentHide') : t('dataThread.subagentShow')}>
                        <IconButton
                            size="small"
                            aria-expanded={open}
                            aria-label={open ? t('dataThread.subagentHide') : t('dataThread.subagentShow')}
                            onClick={() => setOpen(o => !o)}
                            sx={{ ml: 'auto', flexShrink: 0, width: 16, height: 16, p: 0, color: 'primary.main' }}
                        >
                            {/* ">" collapsed, pointing down when open */}
                            <ChevronRightIcon sx={{
                                fontSize: iconVar.sm, transition: 'transform 0.15s',
                                transform: open ? 'rotate(90deg)' : 'none',
                            }} />
                        </IconButton>
                    </Tooltip>
                )}
            </Box>
            {hasDetail && open && (
                <Box data-testid={`subagent-findings-${step.id}`} sx={{
                    mt: '2px', px: 0.75, py: 0.5, borderRadius: '4px',
                    bgcolor: theme.palette.action.hover,
                }}>
                    {record.findings && (
                        <Typography sx={{ fontSize: textVar.xxs, color: 'text.primary', whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
                            {record.findings}
                        </Typography>
                    )}
                    {record.error && (
                        <Typography sx={{ fontSize: textVar.xxs, color: 'error.main', mt: record.findings ? 0.5 : 0 }}>
                            {record.error}
                        </Typography>
                    )}
                </Box>
            )}
        </Box>
    );
};

export const AgentPlanChecklist: React.FC<{
    steps: AgentPlanStep[];
    /** True while the run is still working (animates the current step). */
    active?: boolean;
    /** One-line "Plan 2/4" summary instead of the full list. */
    compact?: boolean;
    /** The running step list (sub-agent lines carry a "[Step n] " tag). */
    runningPlan?: string;
    /** Stop one sub-agent; resolve to false when it was no longer running. */
    onStopStep?: (step: AgentPlanStep) => Promise<boolean> | void;
}> = ({ steps, active = false, compact = false, runningPlan, onStopStep }) => {
    const { t } = useTranslation();
    const theme = useTheme();
    const anyLive = active && (steps || []).some(s => subagentIsLive(s.subagent?.status));
    const now = useNow(anyLive);
    if (!steps || steps.length === 0) return null;
    const runningLines = (runningPlan ?? '').split('\x1E').filter(l => l.trim());
    const subagentCount = steps.filter(s => s.subagent).length;
    const { done, total } = planProgress(steps);

    if (compact) {
        return (
            <Typography
                data-testid="agent-plan-compact"
                sx={{ fontSize: textVar.xs, color: 'text.secondary', lineHeight: 1.4 }}
            >
                {t('dataThread.planProgress', { done, total })}
            </Typography>
        );
    }

    return (
        <Box
            data-testid="agent-plan"
            sx={{
                border: `1px solid ${alpha(theme.palette.primary.main, 0.18)}`,
                bgcolor: alpha(theme.palette.primary.main, 0.03),
                borderRadius: '6px',
                px: 1, py: 0.75,
            }}
        >
            <Typography sx={{
                fontSize: textVar.xs, fontWeight: 600, color: 'text.secondary',
                mb: 0.5, display: 'flex', justifyContent: 'space-between', gap: 1, whiteSpace: 'nowrap',
            }}>
                <Box component="span" sx={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {t('dataThread.planTitle')}
                    {subagentCount > 0 && (
                        <Box component="span" sx={{ fontWeight: 400, color: 'text.disabled', ml: 0.75 }}>
                            {t('dataThread.subagentSummary', { count: subagentCount })}
                        </Box>
                    )}
                </Box>
                <Box component="span" sx={{ flexShrink: 0 }}>{t('dataThread.planProgress', { done, total })}</Box>
            </Typography>
            <Box component="ol" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: '3px' }}>
                {steps.map((step, index) => (
                    <Box
                        component="li"
                        key={step.id}
                        data-status={step.status}
                        sx={{ display: 'flex', flexDirection: 'column', gap: '1px' }}
                    >
                        <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 0.75 }}>
                            <StepIcon status={step.status} active={active} />
                            <Typography sx={{
                                fontSize: textVar.xs,
                                lineHeight: 1.45,
                                wordBreak: 'break-word',
                                color: step.status === 'in_progress' ? 'text.primary' : 'text.secondary',
                                fontWeight: step.status === 'in_progress' ? 500 : 400,
                                textDecoration: step.status === 'skipped' ? 'line-through' : 'none',
                            }}>
                                {step.title}
                            </Typography>
                        </Box>
                        {step.subagent && (
                            <SubagentDetail
                                step={step}
                                record={step.subagent}
                                stepNumber={index + 1}
                                active={active}
                                now={now}
                                runningLines={runningLines}
                                onStop={onStopStep}
                            />
                        )}
                    </Box>
                ))}
            </Box>
        </Box>
    );
};
