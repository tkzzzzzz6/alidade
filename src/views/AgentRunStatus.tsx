// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * Compact run status + queued follow-ups, shown at the top of the chat box.
 *
 * Replaces the old full-card "Agent is working…" veil: the input stays usable
 * while the agent runs, a single line says what it is doing right now, and
 * anything the user sends meanwhile waits in an editable queue.
 */

import React, { FC, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Box, CircularProgress, IconButton, InputBase, Tooltip, Typography, useTheme } from '@mui/material';
import { alpha } from '@mui/material/styles';
import StopIcon from '@mui/icons-material/Stop';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import CloseIcon from '@mui/icons-material/Close';
import CheckIcon from '@mui/icons-material/Check';
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded';
import ScheduleIcon from '@mui/icons-material/Schedule';
import { AgentPlanStep } from '../components/ComponentType';
import { QueuedMessage } from '../app/messageQueue';
import { iconVar, textVar } from '../app/layout';
import { planProgress } from './AgentPlanChecklist';

const STEP_MARKERS = /^(✓|✗|⚠|📋)\s*/u;

/** The latest step of the running plan, without its status marker. */
export function latestStepText(runningPlan: string | undefined): string {
    const step = (runningPlan ?? '')
        .split('\x1E')
        .map(s => s.trim())
        .filter(Boolean)
        .pop();
    return (step ?? '').replace(STEP_MARKERS, '').split('\n')[0].trim();
}

function useElapsedSeconds(startedAt: number | null): number {
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        if (startedAt == null) return;
        const timer = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, [startedAt]);
    return startedAt == null ? 0 : Math.max(0, Math.floor((now - startedAt) / 1000));
}

/** One line: spinner · current step · plan progress · elapsed · stop. */
export const AgentStatusLine: FC<{
    runningPlan?: string;
    agentPlan?: AgentPlanStep[];
    startedAt: number | null;
    onStop?: () => void;
}> = ({ runningPlan, agentPlan, startedAt, onStop }) => {
    const { t } = useTranslation();
    const theme = useTheme();
    const elapsed = useElapsedSeconds(startedAt);
    const step = latestStepText(runningPlan) || t('dataThread.thinking');
    const progress = agentPlan && agentPlan.length > 0 ? planProgress(agentPlan) : null;

    return (
        <Box
            data-testid="agent-status-line"
            role="status"
            aria-live="polite"
            sx={{ display: 'flex', alignItems: 'center', gap: 0.75, px: 0.5, pb: 0.5, minWidth: 0 }}
        >
            <CircularProgress size={11} thickness={6} sx={{ flexShrink: 0 }} />
            <Typography
                title={step}
                sx={{
                    fontSize: textVar.xs, color: 'text.secondary', minWidth: 0, flex: 1,
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}
            >
                {step}
            </Typography>
            {progress && progress.total > 0 && (
                <Typography sx={{ fontSize: textVar.xxs, color: 'text.disabled', flexShrink: 0 }}>
                    {t('dataThread.planProgress', progress)}
                </Typography>
            )}
            {elapsed > 0 && (
                <Typography sx={{ fontSize: textVar.xxs, color: 'text.disabled', flexShrink: 0, fontVariantNumeric: 'tabular-nums' }}>
                    {elapsed}s
                </Typography>
            )}
            {onStop && (
                <Tooltip title={t('dataLoading.stopTooltip', { defaultValue: 'Stop' })}>
                    <IconButton
                        size="small"
                        aria-label={t('dataLoading.stopTooltip', { defaultValue: 'Stop' })}
                        onClick={onStop}
                        sx={{
                            flexShrink: 0, width: 20, height: 20, p: 0,
                            color: 'error.main',
                            bgcolor: alpha(theme.palette.error.main, 0.08),
                            '&:hover': { bgcolor: alpha(theme.palette.error.main, 0.16) },
                        }}
                    >
                        <StopIcon sx={{ fontSize: iconVar.xs }} />
                    </IconButton>
                </Tooltip>
            )}
        </Box>
    );
};

const QueuedRow: FC<{
    message: QueuedMessage;
    index: number;
    onUpdate: (id: string, text: string) => void;
    onRemove: (id: string) => void;
}> = ({ message, index, onUpdate, onRemove }) => {
    const { t } = useTranslation();
    const theme = useTheme();
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(message.text);

    const save = () => { onUpdate(message.id, draft); setEditing(false); };
    const cancel = () => { setDraft(message.text); setEditing(false); };

    return (
        <Box
            component="li"
            data-testid="queued-message"
            sx={{
                display: 'flex', alignItems: 'center', gap: 0.5, minWidth: 0,
                px: 0.75, py: '2px', borderRadius: '6px',
                bgcolor: alpha(theme.palette.text.primary, 0.03),
                '& .queued-actions': { opacity: editing ? 1 : 0.55 },
                '&:hover .queued-actions': { opacity: 1 },
            }}
        >
            <Typography sx={{ fontSize: textVar.xxs, color: 'text.disabled', flexShrink: 0, width: 14 }}>
                {index + 1}.
            </Typography>
            {editing ? (
                <InputBase
                    autoFocus
                    multiline
                    maxRows={4}
                    value={draft}
                    onChange={e => setDraft(e.target.value)}
                    onKeyDown={e => {
                        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save(); }
                        if (e.key === 'Escape') { e.preventDefault(); cancel(); }
                    }}
                    inputProps={{ 'aria-label': t('chartRec.queue.editAria') }}
                    sx={{ flex: 1, fontSize: textVar.xs, py: 0 }}
                />
            ) : (
                <Typography
                    title={message.text}
                    onDoubleClick={() => setEditing(true)}
                    sx={{
                        flex: 1, minWidth: 0, fontSize: textVar.xs, color: 'text.primary',
                        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', cursor: 'text',
                    }}
                >
                    {message.text}
                </Typography>
            )}
            <Box className="queued-actions" sx={{ display: 'flex', flexShrink: 0, transition: 'opacity 0.15s' }}>
                {editing ? (
                    <>
                        <IconButton size="small" aria-label={t('chartRec.queue.save')} onClick={save} sx={{ p: '2px' }}>
                            <CheckIcon sx={{ fontSize: iconVar.xs }} />
                        </IconButton>
                        <IconButton size="small" aria-label={t('chartRec.queue.cancelEdit')} onClick={cancel} sx={{ p: '2px' }}>
                            <CloseIcon sx={{ fontSize: iconVar.xs }} />
                        </IconButton>
                    </>
                ) : (
                    <>
                        <Tooltip title={t('chartRec.queue.edit')}>
                            <IconButton size="small" aria-label={t('chartRec.queue.edit')} onClick={() => setEditing(true)} sx={{ p: '2px' }}>
                                <EditOutlinedIcon sx={{ fontSize: iconVar.xs }} />
                            </IconButton>
                        </Tooltip>
                        <Tooltip title={t('chartRec.queue.remove')}>
                            <IconButton size="small" aria-label={t('chartRec.queue.remove')} onClick={() => onRemove(message.id)} sx={{ p: '2px' }}>
                                <CloseIcon sx={{ fontSize: iconVar.xs }} />
                            </IconButton>
                        </Tooltip>
                    </>
                )}
            </Box>
        </Box>
    );
};

/** Queued follow-ups, each editable / removable, plus the queue's state. */
export const QueuedMessageList: FC<{
    queue: QueuedMessage[];
    /** Why the queue is not sending right now (null = it will send automatically). */
    status: 'waiting-run' | 'waiting-answer' | 'held';
    onUpdate: (id: string, text: string) => void;
    onRemove: (id: string) => void;
    onResume?: () => void;
}> = ({ queue, status, onUpdate, onRemove, onResume }) => {
    const { t } = useTranslation();
    if (queue.length === 0) return null;
    const statusText = status === 'held'
        ? t('chartRec.queue.held')
        : status === 'waiting-answer'
            ? t('chartRec.queue.waitingAnswer')
            : t('chartRec.queue.waitingRun');
    return (
        <Box data-testid="queued-messages" sx={{ px: 0.5, pb: 0.5 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mb: '2px' }}>
                <ScheduleIcon sx={{ fontSize: iconVar.xs, color: 'text.disabled' }} />
                <Typography sx={{ fontSize: textVar.xxs, color: 'text.secondary', flex: 1 }}>
                    {t('chartRec.queue.count', { count: queue.length })} · {statusText}
                </Typography>
                {status === 'held' && onResume && (
                    <Tooltip title={t('chartRec.queue.resume')}>
                        <IconButton size="small" aria-label={t('chartRec.queue.resume')} onClick={onResume} sx={{ p: '2px', color: 'primary.main' }}>
                            <PlayArrowRoundedIcon sx={{ fontSize: iconVar.sm }} />
                        </IconButton>
                    </Tooltip>
                )}
            </Box>
            <Box component="ol" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {queue.map((message, index) => (
                    <QueuedRow key={message.id} message={message} index={index} onUpdate={onUpdate} onRemove={onRemove} />
                ))}
            </Box>
        </Box>
    );
};
