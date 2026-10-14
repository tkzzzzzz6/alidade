// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * The canvas while sub-agents work: main agent → dispatch → one column of
 * cards per round (steps a later round will take wait "locked" in theirs) →
 * merge, a timeline of the rounds and their event log. A finished run can be
 * played back (`replay`): the same view, read as of a moving clock that only
 * lives in this component (nothing is written to the store while it plays).
 *
 * Everything shown comes from the plan's sub-agent records (see
 * `app/dispatchView.ts`); the only motion that is not a state change is the
 * dots running along the links that are working right now (after the
 * live-panel idea: a diagram as the monitor of a running system). With
 * "reduce motion" switched on in the OS the dots and spinners are left out.
 */

import React, { FC, ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
    Box, Chip, GlobalStyles, IconButton, Popover, Slider, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
    alpha, useMediaQuery, useTheme,
} from '@mui/material';
import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import PauseIcon from '@mui/icons-material/Pause';
import ReplayIcon from '@mui/icons-material/Replay';
import AutorenewIcon from '@mui/icons-material/Autorenew';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ScheduleIcon from '@mui/icons-material/Schedule';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import RemoveCircleOutlineIcon from '@mui/icons-material/RemoveCircleOutline';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import StopIcon from '@mui/icons-material/Stop';
import ChevronRightIcon from '@mui/icons-material/ChevronRight';
import SmartToyOutlinedIcon from '@mui/icons-material/SmartToyOutlined';
import HubIcon from '@mui/icons-material/Hub';
import CallMergeIcon from '@mui/icons-material/CallMerge';
import TrendingDownIcon from '@mui/icons-material/TrendingDown';

import { AgentPlanStep } from '../components/ComponentType';
import {
    buildDispatchView, DispatchStepView, DispatchView, LogEntry, replaySpan, RoundView, StepPhase,
} from '../app/dispatchView';
import { dispatchLayout, DispatchLayout, Rect } from '../app/dispatchLayout';
import { latestActivity, lineTag } from './subagentEvents';

const MONO = 'var(--df-font-mono)';
/** smallest diagram scale before it scrolls sideways */
const MIN_SCALE = 0.7;
const SPIN = {
    animation: 'df-dispatch-spin 1.6s linear infinite',
    '@keyframes df-dispatch-spin': { from: { transform: 'rotate(0deg)' }, to: { transform: 'rotate(360deg)' } },
};

export interface DispatchPanelProps {
    plan: AgentPlanStep[];
    /** the run is still going (after the last step returns the main agent merges) */
    runActive: boolean;
    /** the running-step lines (for what each sub-agent is doing right now) */
    runningLines?: string[];
    modelName?: string;
    /** shown at the top right (the dispatch / charts switch) */
    toggle?: ReactNode;
    onStopStep?: (step: AgentPlanStep) => void;
    /** fixed clock, for tests */
    now?: number;
    /** play back a finished run (with a play / scrub control) */
    replay?: boolean;
}

function useNow(enabled: boolean, fixed?: number): number {
    const [now, setNow] = useState(() => fixed ?? Date.now());
    useEffect(() => {
        if (fixed != null) { setNow(fixed); return; }
        if (!enabled) { setNow(Date.now()); return; }
        const timer = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, [enabled, fixed]);
    return fixed ?? now;
}

function useWidth(ref: React.RefObject<HTMLElement>): number {
    const [width, setWidth] = useState(0);
    useLayoutEffect(() => {
        const el = ref.current;
        if (!el) return;
        setWidth(el.clientWidth);
        if (typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(() => setWidth(el.clientWidth));
        ro.observe(el);
        return () => ro.disconnect();
    }, [ref]);
    return width;
}

const fmtS = (s: number) => s.toFixed(1);
const fmtK = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
const clock = (ms: number) => new Date(ms).toTimeString().slice(0, 8);

// ── small pieces ─────────────────────────────────────────────────────────

const StatusIcon: FC<{ phase: StepPhase; motion: boolean; size?: number }> = ({ phase, motion, size = 14 }) => {
    const theme = useTheme();
    const sx = { fontSize: size, flexShrink: 0 };
    if (phase === 'running') return <AutorenewIcon sx={{ ...sx, color: theme.palette.primary.main, ...(motion ? SPIN : {}) }} />;
    if (phase === 'done') return <CheckCircleIcon sx={{ ...sx, color: theme.palette.success.main }} />;
    if (phase === 'queued') return <ScheduleIcon sx={{ ...sx, color: theme.palette.text.disabled }} />;
    if (phase === 'locked') return <LockOutlinedIcon sx={{ ...sx, color: theme.palette.text.disabled }} />;
    if (phase === 'stopped') return <RemoveCircleOutlineIcon sx={{ ...sx, color: theme.palette.text.disabled }} />;
    return <ErrorOutlineIcon sx={{ ...sx, color: theme.palette.error.main }} />;
};

/** Keyframes for the moving dots, defined once for the whole panel. */
const PACKET_KEYFRAMES = (
    <GlobalStyles styles={{
        '@keyframes df-dispatch-move': { from: { offsetDistance: '0%' }, to: { offsetDistance: '100%' } },
        '@keyframes df-dispatch-once': {
            '0%': { offsetDistance: '0%', opacity: 1 }, '85%': { offsetDistance: '100%', opacity: 1 },
            '100%': { offsetDistance: '100%', opacity: 0 },
        },
    }} />
);

/** A dot (and a fainter one behind it) running along an SVG path (CSS motion path, inline style). */
const Packet: FC<{ d: string; color: string; dur: number; delay?: number; once?: boolean }> = ({ d, color, dur, delay = 0, once }) => {
    const dot = (size: number, opacity: number, lag: number) => (
        <span aria-hidden data-packet style={{
            position: 'absolute', left: 0, top: 0, width: size, height: size, borderRadius: '50%', background: color, opacity,
            offsetPath: `path('${d}')`, offsetRotate: '0deg', offsetAnchor: 'center', pointerEvents: 'none',
            animation: `${once ? 'df-dispatch-once' : 'df-dispatch-move'} ${dur}s linear ${-delay + lag}s ${once ? '1 forwards' : 'infinite'}`,
        } as React.CSSProperties} />
    );
    return <>{dot(7, 1, 0)}{dot(4, 0.45, 0.1)}</>;
};

const boxAt = (r: Rect) => ({ position: 'absolute' as const, left: r.x, top: r.y, width: r.w, height: r.h, boxSizing: 'border-box' as const });

// ── step card ────────────────────────────────────────────────────────────

const StepCard: FC<{
    s: DispatchStepView; rect: Rect; activity: string; motion: boolean; maxParallel: number | null;
    onStop?: () => void;
}> = ({ s, rect, activity, motion, maxParallel, onStop }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    const [anchor, setAnchor] = useState<HTMLElement | null>(null);
    const live = s.phase === 'running' || s.phase === 'queued';
    const waitingLook = s.phase === 'queued' || s.phase === 'locked';
    const stepList = s.waitsFor.map(n => t('dataThread.stepTag', { n })).join(t('dispatch.listSep'));
    const tone = s.phase === 'running' ? theme.palette.primary.main
        : s.phase === 'failed' ? theme.palette.error.main : null;
    const firstLine = s.findings.split('\n').find(l => l.trim()) || '';

    const line2 = s.phase === 'locked' ? (stepList ? t('dispatch.lockedWaits', { steps: stepList }) : t('dispatch.lockedNoDeps'))
        : s.phase === 'running' ? (activity || t('dispatch.working'))
        : s.phase === 'queued' ? (maxParallel ? t('dispatch.waitingLimit', { p: maxParallel }) : t('dispatch.waiting'))
            : s.phase === 'done' ? (firstLine ? t('dispatch.doneLine', { charts: s.charts, findings: firstLine }) : t('dispatch.doneNoFindings', { charts: s.charts }))
                : (s.error || (s.status === 'timeout' && s.timeoutSeconds ? t('dispatch.timedOut', { limit: s.timeoutSeconds }) : t(`dataThread.subagentStatus.${s.status}`, { defaultValue: s.status })));
    const line3 = s.phase === 'locked' ? t('dispatch.lockedHint', { round: s.round - 1 })
        : s.phase === 'running'
        ? (s.timeoutSeconds ? t('dispatch.runClock', { s: fmtS(s.runSeconds), limit: s.timeoutSeconds }) : t('dispatch.runClockNoLimit', { s: fmtS(s.runSeconds) }))
          + (s.waitSeconds >= 1 ? t('dispatch.startedAfterWait', { w: fmtS(s.waitSeconds) }) : '')
        : s.phase === 'queued' ? t('dispatch.waited', { s: fmtS(s.waitSeconds) })
            : s.phase === 'done' ? t('dispatch.doneClock', { s: fmtS(s.runSeconds) })
                : t('dispatch.failedClock', { status: t(`dataThread.subagentStatus.${s.status}`, { defaultValue: t(`dispatch.phase.${s.phase}`) }), s: fmtS(s.runSeconds) });

    const details = (
        <Box sx={{ fontSize: 11, lineHeight: 1.6 }}>
            <Box sx={{ fontWeight: 700 }}>{t('dataThread.stepTag', { n: s.number })} · {s.title}</Box>
            {s.phase === 'locked' ? <Box sx={{ opacity: 0.8 }}>{t('dispatch.lockedHint', { round: s.round - 1 })}</Box> : s.usage ? (<>
                <Box>{t('dispatch.usage', { calls: s.usage.calls, tools: s.usage.toolCalls })}</Box>
                <Box>{t('dispatch.tokens', { input: fmtK(s.usage.promptTokens), cached: fmtK(s.usage.cachedTokens), output: fmtK(s.usage.completionTokens) })}</Box>
            </>) : <Box sx={{ opacity: 0.8 }}>{t('dispatch.usagePending')}</Box>}
            {s.chartTitles.filter(Boolean).map((title, i) => <Box key={i}>• {title}</Box>)}
            {s.straggler && <Box sx={{ opacity: 0.8 }}>{t('dispatch.stragglerHint')}</Box>}
        </Box>
    );

    return (
        <Tooltip title={details} placement="right" arrow={false} disableInteractive>
            <Box data-testid={`dispatch-step-${s.id}`} data-phase={s.phase} sx={{
                ...boxAt(rect), px: 1.5, py: 1, borderRadius: '6px', bgcolor: 'background.paper',
                border: waitingLook ? `1px dashed ${theme.palette.divider}` : `1px solid ${tone ?? theme.palette.divider}`,
                ...(tone ? { backgroundImage: `linear-gradient(${alpha(tone, 0.04)}, ${alpha(tone, 0.04)})` } : {}),
                ...(waitingLook ? { bgcolor: theme.palette.grey[50] } : {}),
                boxShadow: waitingLook ? 'none' : '0 1px 2px rgba(0,0,0,0.05)',
                display: 'flex', flexDirection: 'column', justifyContent: 'space-between', overflow: 'hidden',
            }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
                    <StatusIcon phase={s.phase} motion={motion} />
                    <Typography noWrap sx={{ fontSize: 13, fontWeight: 700, flex: 1, minWidth: 0, color: live || s.phase === 'done' ? 'text.primary' : 'text.secondary' }}>
                        {t('dataThread.stepTag', { n: s.number })} · {s.title}
                    </Typography>
                    {s.phase === 'running' && onStop && (
                        <Tooltip title={t('dispatch.stop')}>
                            <IconButton size="small" aria-label={t('dispatch.stop')} onClick={(e) => { e.stopPropagation(); onStop(); }}
                                sx={{ width: 18, height: 18, p: 0, color: 'error.main' }}>
                                <StopIcon sx={{ fontSize: 16 }} />
                            </IconButton>
                        </Tooltip>
                    )}
                </Box>
                <Typography noWrap sx={{ fontSize: 12, color: waitingLook ? 'text.disabled' : 'text.secondary' }}>{line2}</Typography>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
                    <Typography noWrap sx={{ fontSize: 11, fontFamily: MONO, color: 'text.disabled' }}>{line3}</Typography>
                    {s.straggler && (
                        <Chip size="small" variant="outlined" color="warning" icon={<TrendingDownIcon />} label={t('dispatch.straggler')}
                            sx={{ height: 17, fontSize: 10.5, '& .MuiChip-icon': { fontSize: 12, ml: 0.5 }, '& .MuiChip-label': { px: 0.75 } }} />
                    )}
                    {s.phase === 'done' && s.findings && (
                        <Box component="button" type="button" onClick={(e: React.MouseEvent<HTMLElement>) => { e.stopPropagation(); setAnchor(e.currentTarget); }}
                            sx={{ ml: 'auto', display: 'flex', alignItems: 'center', border: 0, bgcolor: 'transparent', p: 0, cursor: 'pointer',
                                  color: 'primary.main', fontSize: 11.5, fontFamily: 'inherit' }}>
                            {t('dispatch.findings')}<ChevronRightIcon sx={{ fontSize: 15 }} />
                        </Box>
                    )}
                </Box>
                <Popover open={!!anchor} anchorEl={anchor} onClose={() => setAnchor(null)}
                    anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }} transformOrigin={{ vertical: 'top', horizontal: 'right' }}>
                    <Typography sx={{ p: 1.5, maxWidth: 360, fontSize: 12, whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>{s.findings}</Typography>
                </Popover>
            </Box>
        </Tooltip>
    );
};

// ── main agent / dispatch / merge ───────────────────────────────────────

const MainCard: FC<{ v: DispatchView; rect: Rect; planLength: number; modelName?: string; motion: boolean }> = ({ v, rect, planLength, modelName, motion }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    const row = (state: 'done' | 'active' | 'todo', text: string) => (
        <Box sx={{
            display: 'flex', alignItems: 'center', gap: 0.75, px: 0.75, py: '3px', borderRadius: '4px',
            ...(state === 'active' ? { bgcolor: alpha(theme.palette.primary.main, 0.08) } : {}),
        }}>
            {state === 'done' ? <CheckCircleIcon sx={{ fontSize: 13, color: 'success.main' }} />
                : state === 'active' ? <AutorenewIcon sx={{ fontSize: 13, color: 'primary.main', ...(motion ? SPIN : {}) }} />
                    : <RadioButtonUncheckedIcon sx={{ fontSize: 13, color: 'text.disabled' }} />}
            <Typography noWrap sx={{ fontSize: 12, fontWeight: state === 'active' ? 700 : 400, color: state === 'todo' ? 'text.disabled' : state === 'active' ? 'text.primary' : 'text.secondary' }}>{text}</Typography>
        </Box>
    );
    const waiting = v.mainPhase === 'waiting';
    return (
        <Box data-testid="dispatch-main" sx={{ ...boxAt(rect), p: 1.25, borderRadius: '6px', bgcolor: 'background.paper', border: `1px solid ${theme.palette.divider}`, boxShadow: '0 1px 2px rgba(0,0,0,0.05)', display: 'flex', flexDirection: 'column', gap: 0.25 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, pb: 0.75, mb: 0.5, borderBottom: `1px solid ${theme.palette.divider}` }}>
                <SmartToyOutlinedIcon sx={{ fontSize: 16, color: 'primary.main' }} />
                <Typography noWrap sx={{ fontSize: 13, fontWeight: 700, flexShrink: 0 }}>{t('dispatch.main')}</Typography>
                {modelName && rect.w >= 200 && <Typography noWrap sx={{ ml: 'auto', fontSize: 10.5, fontFamily: MONO, color: 'text.disabled', minWidth: 0 }}>{modelName}</Typography>}
            </Box>
            {row('done', t('dispatch.phaseRead'))}
            {row('done', t('dispatch.phasePlan', { n: planLength }))}
            {row('done', v.rounds.length > 1 ? t('dispatch.phaseDispatchRounds', { r: v.rounds.length }) : t('dispatch.phaseDispatch'))}
            {row(waiting ? 'active' : 'done', t('dispatch.phaseWaiting', { k: v.finished, n: v.total }))}
            {v.locked > 0 && row('todo', t('dispatch.phaseLocked', { n: v.locked }))}
            {row(v.mainPhase === 'merging' ? 'active' : v.mainPhase === 'done' ? 'done' : 'todo', t('dispatch.phaseMerge'))}
            {v.ownSteps.length > 0 && (
                <Box sx={{ mt: 'auto', p: 1, borderRadius: '4px', bgcolor: 'rgba(0,0,0,0.03)' }}>
                    <Typography sx={{ fontSize: 11, color: 'text.secondary', mb: 0.25 }}>{t('dispatch.ownSteps')}</Typography>
                    {v.ownSteps.slice(0, 3).map(o => (
                        <Typography key={o.number} noWrap sx={{ fontSize: 11.5 }}>{t('dataThread.stepTag', { n: o.number })} · {o.title}</Typography>
                    ))}
                </Box>
            )}
        </Box>
    );
};

const DispatchNode: FC<{ v: DispatchView; rect: Rect }> = ({ v, rect }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    const c = theme.palette.secondary.main;
    return (
        <Box data-testid="dispatch-node" sx={{ ...boxAt(rect), px: 1.25, borderRadius: '6px', border: `1px solid ${c}`, bgcolor: alpha(c, 0.08), display: 'flex', alignItems: 'center', gap: 1 }}>
            <HubIcon sx={{ fontSize: 18, color: c }} />
            <Box sx={{ minWidth: 0 }}>
                <Typography sx={{ fontSize: 12.5, fontWeight: 700, color: c, lineHeight: 1.3 }}>dispatch</Typography>
                <Typography noWrap sx={{ fontSize: 11, color: 'text.secondary', lineHeight: 1.3 }}>
                    {t('dispatch.dispatchSub', { n: v.rounds[0]?.total ?? v.total })}
                </Typography>
            </Box>
        </Box>
    );
};

const MergeNode: FC<{ v: DispatchView; rect: Rect }> = ({ v, rect }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    const color = (p: StepPhase) => p === 'done' ? theme.palette.success.main : p === 'running' ? alpha(theme.palette.primary.main, 0.35)
        : p === 'queued' || p === 'locked' ? 'rgba(0,0,0,0.12)' : theme.palette.error.light;
    return (
        <Box data-testid="dispatch-merge" sx={{ ...boxAt(rect), px: 1.25, borderRadius: '6px', bgcolor: 'background.paper', border: `1px solid ${theme.palette.divider}`, boxShadow: '0 1px 2px rgba(0,0,0,0.05)', display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 0.5 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                <CallMergeIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
                <Typography sx={{ fontSize: 12.5, fontWeight: 700 }}>{t('dispatch.mergeTitle')}</Typography>
                <Typography sx={{ ml: 'auto', fontSize: 11, color: 'text.secondary' }}>{v.finished}/{v.total + v.locked}</Typography>
            </Box>
            <Box sx={{ display: 'flex', gap: '2px' }}>
                {v.rounds.flatMap(r => r.steps).map(s => <Box key={s.id} sx={{ flex: 1, height: 4, borderRadius: 2, bgcolor: color(s.phase) }} />)}
            </Box>
        </Box>
    );
};

/** Between two rounds: the main agent merges round k, then hands out round k + 1. */
const JointNode: FC<{ r: RoundView; next: RoundView; rect: Rect }> = ({ r, next, rect }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    const c = theme.palette.secondary.main;
    const on = next.started;
    return (
        <Box data-testid={`dispatch-joint-${r.round}`} sx={{
            ...boxAt(rect), px: 1, borderRadius: '6px', display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 0.25,
            border: on ? `1px solid ${c}` : `1px dashed ${theme.palette.divider}`, bgcolor: on ? alpha(c, 0.08) : theme.palette.grey[50],
        }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                <CallMergeIcon sx={{ fontSize: 14, color: on ? c : 'text.disabled' }} />
                <Typography noWrap sx={{ fontSize: 11.5, fontWeight: 700, color: on ? c : 'text.secondary' }}>{t('dispatch.jointTitle')}</Typography>
            </Box>
            <Typography noWrap sx={{ fontSize: 10.5, color: 'text.secondary' }}>
                {on ? t('dispatch.jointSent', { round: next.round, n: next.total }) : t('dispatch.jointWaiting', { k: r.finished, n: r.total })}
            </Typography>
        </Box>
    );
};

// ── diagram ──────────────────────────────────────────────────────────────

const Diagram: FC<{
    v: DispatchView; L: DispatchLayout; plan: AgentPlanStep[]; lines: string[]; modelName?: string;
    motion: boolean; now: number; onStopStep?: (step: AgentPlanStep) => void;
}> = ({ v, L, plan, lines, modelName, motion, now, onStopStep }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    const outer = useRef<HTMLDivElement>(null);
    const width = useWidth(outer);
    // below this the text gets too small to read: scroll sideways instead
    const scale = width > 0 ? Math.max(MIN_SCALE, Math.min(1.15, width / L.width)) : 1;
    const blue = theme.palette.primary.main;
    const green = theme.palette.success.main;
    const grey = theme.palette.grey[400];
    const red = theme.palette.error.main;

    const toStepStyle = (p: StepPhase) => p === 'running' ? { stroke: blue }
        : p === 'done' ? { stroke: green, strokeOpacity: 0.45 }
            : p === 'queued' || p === 'locked' ? { stroke: grey, strokeDasharray: '4 4' } : { stroke: red, strokeOpacity: 0.5 };
    const fromStepStyle = (p: StepPhase) => p === 'done' ? { stroke: green }
        : p === 'queued' || p === 'locked' ? { stroke: grey, strokeDasharray: '4 4' } : { stroke: grey };

    const arrow = (id: string, color: string) => (
        <marker id={id} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,1 L9,5 L0,9 z" fill={color} />
        </marker>
    );
    const head = (p: StepPhase, from: boolean) => from
        ? (p === 'done' ? 'url(#dpG)' : 'url(#dpN)')
        : (p === 'running' ? 'url(#dpB)' : p === 'done' ? 'url(#dpG)' : p === 'queued' || p === 'locked' ? 'url(#dpN)' : 'url(#dpR)');
    const rounds = v.rounds.map((r, k) => ({ r, col: L.columns[k] })).filter(x => x.col);
    let order = 0;

    return (
        <Box ref={outer} sx={{ width: '100%', overflowX: L.width * scale > width + 1 ? 'auto' : 'hidden', overflowY: 'hidden' }}>
          <Box sx={{ width: L.width * scale, height: L.height * scale, position: 'relative' }}>
            <Box sx={{ position: 'absolute', left: 0, top: 0, width: L.width, height: L.height, transform: `scale(${scale})`, transformOrigin: '0 0' }}>
                {motion && PACKET_KEYFRAMES}
                <svg width={L.width} height={L.height} style={{ position: 'absolute', left: 0, top: 0, overflow: 'visible' }} aria-hidden>
                    <defs>{arrow('dpB', blue)}{arrow('dpG', green)}{arrow('dpN', grey)}{arrow('dpR', red)}</defs>
                    <g fill="none" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
                        <path d={L.toMain.d} stroke={grey} markerEnd="url(#dpN)" />
                        {rounds.map(({ r, col }) => (
                            <React.Fragment key={`w-${r.round}`}>
                                <path d={col.trunk.d} stroke={r.steps.some(s => s.phase === 'running') ? blue : grey}
                                    {...(r.started ? {} : { strokeDasharray: '4 4' })} />
                                {r.steps.map((s, i) => <path key={`to-${s.id}`} d={col.toStep[i].d} {...toStepStyle(s.phase)} markerEnd={head(s.phase, false)} />)}
                                {r.steps.map((s, i) => <path key={`from-${s.id}`} d={col.fromStep[i].d} {...fromStepStyle(s.phase)} markerEnd={head(s.phase, true)} />)}
                            </React.Fragment>
                        ))}
                        <path d={L.back.d} stroke={grey} strokeDasharray="2 4" markerEnd="url(#dpN)" />
                    </g>
                </svg>
                <Typography sx={{
                    position: 'absolute', left: 0, width: L.width, top: L.backLabelY - 8, textAlign: 'center', pointerEvents: 'none',
                }}>
                    <Box component="span" sx={{ px: 1, bgcolor: 'background.paper', fontSize: 11, color: 'text.disabled' }}>{t('dispatch.backLabel')}</Box>
                </Typography>

                {motion && rounds.map(({ r, col }) => (
                    <React.Fragment key={`p-${r.round}`}>
                        {r.steps.some(s => s.phase === 'running') && <Packet d={col.trunk.d} color={blue} dur={0.7} />}
                        {r.steps.map((s, i) => {
                            const k = order++;
                            if (s.phase === 'running') return (
                                <React.Fragment key={`p-${s.id}`}>
                                    <Packet d={col.toStep[i].d} color={blue} dur={2} delay={k * 0.37} />
                                    <Packet d={col.toStep[i].d} color={blue} dur={2} delay={k * 0.37 + 1} />
                                </React.Fragment>
                            );
                            // a step that just came back sends its result on once
                            if (s.phase === 'done' && s.endedAt != null && now - s.endedAt >= 0 && now - s.endedAt < 3000) {
                                return <Packet key={`b-${s.id}-${s.endedAt}`} d={col.fromStep[i].d} color={green} dur={1.2} once />;
                            }
                            return null;
                        })}
                    </React.Fragment>
                ))}

                <MainCard v={v} rect={L.main} planLength={plan.length} modelName={modelName} motion={motion} />
                <DispatchNode v={v} rect={L.dispatch} />
                {rounds.map(({ r, col }, k) => (
                    <React.Fragment key={`c-${r.round}`}>
                        {r.steps.map((s, i) => {
                            const step = plan.find(p => p.id === s.id);
                            const activity = s.phase === 'running' ? latestActivity(lines, lineTag(t('dataThread.stepTag', { n: s.number }))) : '';
                            return (
                                <StepCard key={s.id} s={s} rect={col.cards[i]} activity={activity} motion={motion} maxParallel={v.maxParallel}
                                    onStop={onStopStep && step ? () => onStopStep(step) : undefined} />
                            );
                        })}
                        {k < rounds.length - 1 && L.joints[k] && <JointNode r={r} next={rounds[k + 1].r} rect={L.joints[k]} />}
                    </React.Fragment>
                ))}
                <MergeNode v={v} rect={L.merge} />
            </Box>
          </Box>
        </Box>
    );
};

// ── timeline & log ───────────────────────────────────────────────────────

/** A round tick step (1, 2, 5 × 10ⁿ s) giving at most 6 ticks over `span` seconds. */
export function niceStep(span: number): number {
    const raw = Math.max(span, 1) / 6;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) if (m * mag >= raw) return m * mag;
    return 10 * mag;
}

const Timeline: FC<{ v: DispatchView; axisEnd?: number }> = ({ v, axisEnd }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    // a replay keeps the whole run's axis, so the bars grow instead of the axis shrinking
    const spanS = Math.max(5, ((axisEnd ?? v.roundEnd) - v.roundStart) / 1000);
    const multi = v.rounds.filter(r => r.started).length > 1;
    const step = niceStep(spanS);
    const axisS = Math.ceil(spanS / step) * step + (spanS % step === 0 ? step : 0);
    const pct = (ms: number) => `${((ms - v.roundStart) / 1000 / axisS) * 100}%`;
    const fill = { running: alpha(theme.palette.primary.main, 0.18), done: alpha(theme.palette.success.main, 0.28), failed: alpha(theme.palette.error.main, 0.22) };
    const hatch = `repeating-linear-gradient(45deg, ${theme.palette.grey[100]} 0 3px, ${theme.palette.grey[300]} 3px 5px)`;
    return (
        <Box data-testid="dispatch-timeline" sx={{ border: `1px solid ${theme.palette.divider}`, borderRadius: '6px', p: 1.5, boxShadow: '0 1px 2px rgba(0,0,0,0.05)' }}>
            <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 1 }}>
                <Typography sx={{ fontSize: 12.5, fontWeight: 700 }}>{t('dispatch.timeline')}</Typography>
                <Typography sx={{ fontSize: 11, color: 'text.disabled' }}>{t('dispatch.timelineHint')}</Typography>
                {v.total > 1 && (
                    <Typography data-testid="dispatch-saved" sx={{ ml: 'auto', fontSize: 11.5, color: 'text.secondary' }}>
                        {multi ? t('dispatch.savedRounds', { serial: fmtS(v.serialSeconds), actual: fmtS(v.elapsedSeconds), saved: fmtS(v.savedSeconds) })
                        : v.savedSeconds > 0
                            ? t('dispatch.saved', { serial: fmtS(v.serialSeconds), actual: fmtS(v.elapsedSeconds), saved: fmtS(v.savedSeconds) })
                            : t('dispatch.savedNone', { serial: fmtS(v.serialSeconds), actual: fmtS(v.elapsedSeconds) })}
                    </Typography>
                )}
            </Box>
            <Box sx={{ display: 'grid', gridTemplateColumns: '56px 1fr', rowGap: '6px', alignItems: 'center' }}>
                {v.timeline.map((row, i) => (
                    <React.Fragment key={row.id}>
                        {multi && (i === 0 || v.timeline[i - 1].round !== row.round) && (() => {
                            const r = v.rounds.find(x => x.round === row.round)!;
                            return (
                                <Typography data-testid={`dispatch-round-${r.round}`} noWrap sx={{ gridColumn: '1 / -1', fontSize: 11, fontWeight: 700, color: 'text.secondary', mt: i === 0 ? 0 : 0.5 }}>
                                    {t('dispatch.roundLabel', { round: r.round })}
                                    <Box component="span" sx={{ fontWeight: 400, color: 'text.disabled' }}>
                                        {'  '}{r.savedSeconds > 0
                                            ? t('dispatch.roundSaved', { actual: fmtS(r.elapsedSeconds), saved: fmtS(r.savedSeconds) })
                                            : t('dispatch.roundTook', { actual: fmtS(r.elapsedSeconds) })}
                                    </Box>
                                </Typography>
                            );
                        })()}
                        <Typography noWrap sx={{ fontSize: 11, color: 'text.secondary' }}>{t('dataThread.stepTag', { n: row.number })}</Typography>
                        <Box sx={{ position: 'relative', height: 9 }}>
                            {row.segments.map((seg, i) => (
                                <Box key={i} data-kind={seg.kind} sx={{
                                    position: 'absolute', top: 0, height: 9, borderRadius: '2px', left: pct(seg.from),
                                    width: `calc(${pct(seg.to)} - ${pct(seg.from)})`, minWidth: 2,
                                    background: seg.kind === 'queued' ? hatch : fill[seg.kind],
                                }} />
                            ))}
                        </Box>
                    </React.Fragment>
                ))}
                <Box />
                <Box sx={{ position: 'relative', height: 14 }}>
                    {Array.from({ length: Math.round(axisS / step) + 1 }, (_, i) => (
                        <Typography key={i} sx={{ position: 'absolute', left: `${(i * step / axisS) * 100}%`, whiteSpace: 'nowrap',
                            transform: i === 0 ? 'none' : i === Math.round(axisS / step) ? 'translateX(-100%)' : 'translateX(-50%)', fontSize: 10, color: 'text.disabled' }}>
                            {i * step}{i === Math.round(axisS / step) ? ' s' : ''}
                        </Typography>
                    ))}
                </Box>
            </Box>
        </Box>
    );
};

const EventLog: FC<{ v: DispatchView; rows?: number }> = ({ v, rows = 5 }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    const text = (e: LogEntry) => ({
        dispatch: () => t(Number(e.params.round) > 1 ? 'dispatch.logDispatchRound' : 'dispatch.logDispatch', e.params), queued: () => t('dispatch.logQueued'), started: () => t('dispatch.logStarted'),
        done: () => t('dispatch.logDone', e.params), failed: () => t('dispatch.logFailed', e.params),
        timeout: () => t('dispatch.logTimeout', e.params), cancelled: () => t('dispatch.logCancelled', e.params),
        straggler: () => t('dispatch.logStraggler'),
    }[e.kind])();
    const whoColor = (e: LogEntry) => e.kind === 'done' ? 'success.main' : e.kind === 'straggler' ? 'warning.dark'
        : e.kind === 'failed' || e.kind === 'timeout' ? 'error.main' : e.stepNumber == null ? 'primary.main' : 'text.secondary';
    const shown = v.log.slice(-rows);
    return (
        <Box data-testid="dispatch-log" sx={{ borderRadius: '6px', p: 1.5, bgcolor: 'rgba(0,0,0,0.03)', border: `1px solid ${alpha(theme.palette.text.primary, 0.08)}` }}>
            <Typography sx={{ fontSize: 12.5, fontWeight: 700, mb: 0.5 }}>{t('dispatch.log')}</Typography>
            {shown.map((e, i) => (
                <Typography key={`${e.at}-${i}`} noWrap sx={{ fontFamily: MONO, fontSize: 11.5, lineHeight: 1.7, color: i === shown.length - 1 ? 'text.primary' : 'text.disabled' }}>
                    {clock(e.at)}{'  '}
                    <Box component="span" sx={{ color: whoColor(e) }}>{e.stepNumber == null ? t('dispatch.who') : t('dataThread.stepTag', { n: e.stepNumber })}</Box>
                    {'  '}{text(e)}
                </Typography>
            ))}
        </Box>
    );
};

// ── replay ───────────────────────────────────────────────────────────────

const SPEEDS = [1, 2, 4] as const;
const TICK_MS = 100;

/**
 * The replay clock: an offset into the run that only this component holds
 * (ticking 10 × a second, paused while the tab is hidden). Starts at the end,
 * so the panel first shows how the run finished.
 */
function useReplayClock(span: { start: number; end: number } | null) {
    const duration = span ? span.end - span.start : 0;
    const [pos, setPos] = useState(duration);
    const [playing, setPlaying] = useState(false);
    const [speed, setSpeed] = useState<number>(2);
    useEffect(() => { setPos(duration); setPlaying(false); }, [span?.start, duration]);
    useEffect(() => {
        if (!playing) return;
        const timer = setInterval(() => {
            if (typeof document !== 'undefined' && document.hidden) return;
            setPos(p => Math.min(duration, p + TICK_MS * speed));
        }, TICK_MS);
        return () => clearInterval(timer);
    }, [playing, speed, duration]);
    useEffect(() => { if (playing && pos >= duration) setPlaying(false); }, [playing, pos, duration]);
    const play = () => {
        if (pos >= duration) setPos(0);
        setPlaying(true);
    };
    return { pos, setPos, playing, setPlaying, play, speed, setSpeed, duration };
}

const ReplayBar: FC<{ clock: ReturnType<typeof useReplayClock>; marks: number[] }> = ({ clock, marks }) => {
    const theme = useTheme();
    const { t } = useTranslation();
    const { pos, setPos, playing, setPlaying, play, speed, setSpeed, duration } = clock;
    const atEnd = pos >= duration;
    const label = playing ? t('dispatch.replayPause') : atEnd ? t('dispatch.replayAgain') : t('dispatch.replayPlay');
    return (
        <Box data-testid="dispatch-replay" sx={{
            display: 'flex', alignItems: 'center', gap: 1.5, px: 1.5, py: 0.5, borderRadius: '6px',
            border: `1px solid ${theme.palette.divider}`, bgcolor: alpha(theme.palette.primary.main, 0.03),
        }}>
            <Tooltip title={label}>
                <IconButton size="small" aria-label={label} data-testid="dispatch-replay-play" color="primary"
                    onClick={() => (playing ? setPlaying(false) : play())}>
                    {playing ? <PauseIcon fontSize="small" /> : atEnd ? <ReplayIcon fontSize="small" /> : <PlayArrowIcon fontSize="small" />}
                </IconButton>
            </Tooltip>
            <Slider size="small" aria-label={t('dispatch.replayPosition')} data-testid="dispatch-replay-slider"
                min={0} max={Math.max(duration, 1)} step={TICK_MS} value={Math.min(pos, duration)}
                marks={marks.map(m => ({ value: m }))}
                onChange={(_, value) => { setPlaying(false); setPos(value as number); }}
                sx={{ flex: 1, mx: 0.5, '& .MuiSlider-mark': { height: 8, width: 2, bgcolor: 'text.disabled' } }} />
            <Typography sx={{ fontSize: 11.5, fontFamily: MONO, color: 'text.secondary', whiteSpace: 'nowrap' }}>
                {fmtS(Math.min(pos, duration) / 1000)} / {fmtS(duration / 1000)} s
            </Typography>
            <ToggleButtonGroup size="small" exclusive value={speed} onChange={(_, value: number | null) => value && setSpeed(value)}
                aria-label={t('dispatch.replaySpeed')}
                sx={{ '& .MuiToggleButton-root': { py: 0, px: 0.75, fontSize: 11, textTransform: 'none' } }}>
                {SPEEDS.map(x => <ToggleButton key={x} value={x}>{x}×</ToggleButton>)}
            </ToggleButtonGroup>
        </Box>
    );
};

// ── panel ────────────────────────────────────────────────────────────────

export const DispatchPanel: FC<DispatchPanelProps> = ({ plan, runActive, runningLines = [], modelName, toggle, onStopStep, now: fixedNow, replay }) => {
    const { t } = useTranslation();
    const reduce = useMediaQuery('(prefers-reduced-motion: reduce)');
    const span = useMemo(() => (replay ? replaySpan(plan) : null), [plan, replay]);
    const clock = useReplayClock(span);
    const replayNow = span ? span.start + Math.min(clock.pos, clock.duration) : undefined;
    const fixed = replayNow ?? fixedNow;
    const live = !replay && runActive;
    const probe = useMemo(() => buildDispatchView(plan, fixed ?? Date.now(), live), [plan, live, fixed]);
    const now = useNow(!replay && (!!probe?.live || runActive), fixed);
    const v = useMemo(() => buildDispatchView(plan, now, live, { replay: !!span }), [plan, now, live, span]);
    const shape = v ? v.rounds.map(r => r.steps.length).join(',') : '1';
    const L = useMemo(() => dispatchLayout(shape.split(',').map(Number)), [shape]);
    if (!v) return null;
    const motion = !reduce && (replay ? clock.playing : fixedNow == null);
    const marks = span ? v.keyTimes.map(x => x - span.start) : [];
    const shownRounds = v.rounds.length;

    return (
        <Box data-testid="dispatch-panel" data-replay={replay ? 'true' : undefined} sx={{ p: 2, display: 'flex', flexDirection: 'column', gap: 1.5, overflow: 'auto', height: '100%', boxSizing: 'border-box', bgcolor: 'background.paper' }}>
            <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2 }}>
                <Box sx={{ minWidth: 0 }}>
                    <Typography sx={{ fontSize: 16, fontWeight: 700 }}>{replay ? t('dispatch.replayTitle') : t('dispatch.title')}</Typography>
                    <Typography sx={{ fontSize: 12, color: 'text.secondary' }}>
                        {shownRounds > 1
                            ? t('dispatch.subtitleRounds', { rounds: shownRounds, n: v.total + v.locked })
                            : t('dispatch.subtitle', { round: v.round, n: v.total })}
                    </Typography>
                </Box>
                <Box sx={{ ml: 'auto', display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 0.75 }}>
                    {toggle}
                    <Typography data-testid="dispatch-stats" sx={{ fontSize: 11.5, color: 'text.secondary' }}>
                        {t('dispatch.stats', { s: fmtS(v.elapsedSeconds), k: v.finished, n: v.total, c: v.charts })}
                        {v.maxParallel ? t('dispatch.statsLimit', { p: v.maxParallel }) : ''}
                    </Typography>
                </Box>
            </Box>
            <Diagram v={v} L={L} plan={plan} lines={replay ? [] : runningLines} modelName={modelName} motion={motion} now={now}
                onStopStep={replay ? undefined : onStopStep} />
            {span && <ReplayBar clock={clock} marks={marks} />}
            <Timeline v={v} axisEnd={span?.end} />
            <EventLog v={replay ? { ...v, log: v.log.filter(e => e.at <= now) } : v} />
        </Box>
    );
};
