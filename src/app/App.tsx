// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { AgentSettings, compactSettings, sameSettings } from './agentSettings';
import { AgentSettingsSection } from '../views/AgentSettingsSection';
import React, { FC, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import '../scss/App.scss';

import { useDispatch, useSelector } from "react-redux";
import {
    AlidadeState,
    dfActions,
    dfSelectors,
    fetchGlobalModelList,
    DEFAULT_ROW_LIMIT,
} from './dfSlice'
import { getBrowserId, generateUUID } from './identity';
import type { AuthInfo } from './oidcConfig';
import { OidcCallback } from './OidcCallback';
import { AuthButton } from './AuthButton';
import { IdentityMigrationDialog } from './IdentityMigrationDialog';

import { red, purple, blue, brown, yellow, orange, } from '@mui/material/colors';
import { palettes, defaultPaletteKey, paletteKeys, bgAlpha } from './tokens';

import _ from 'lodash';

import {
    Button,
    Tooltip,
    Typography,
    Box,
    Toolbar,
    Divider,
    DialogTitle,
    Dialog,
    DialogContent,
    Link,
    DialogContentText,
    DialogActions,
    ToggleButtonGroup,
    ToggleButton,
    Menu,
    MenuItem,
    TextField,
    IconButton,
    Select,
    FormControl,
    InputLabel,
    ListItemIcon,
    ListItemText,
    CircularProgress,
    LinearProgress,
} from '@mui/material';


import MuiAppBar from '@mui/material/AppBar';
import { alpha, createTheme, styled, ThemeProvider, useTheme } from '@mui/material/styles';
import LogoutIcon from '@mui/icons-material/Logout';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import ClearIcon from '@mui/icons-material/Clear';

import { AlidadeFC } from '../views/Alidade';
import { LayoutProvider } from './LayoutProvider';
import { MIN_SUPPORTED } from './layout';
import { useAutoSave } from './useAutoSave';
import { useWorkspaceAutoName } from './useWorkspaceAutoName';

import GridViewIcon from '@mui/icons-material/GridView';
import ViewSidebarIcon from '@mui/icons-material/ViewSidebar';
import SettingsOutlinedIcon from '@mui/icons-material/SettingsOutlined';
import {
    createBrowserRouter,
    Link as RouterLink,
    Outlet,
    RouterProvider,
    useLocation,
    useNavigate,
    useRouteError,
    useSearchParams,
} from "react-router-dom";
import { About } from '../views/About';
import { MessageSnackbar } from '../views/MessageSnackbar';
import { ChartRenderService } from '../views/ChartRenderService';
import { DictTable } from '../components/ComponentType';
import { AppDispatch } from './store';
import dfLogo from '../assets/alidade-logo.svg';
import { AnvilLoader } from '../components/AnvilLoader';
import { ModelSelectionButton } from '../views/ModelSelectionDialog';
import { LogViewerDialog } from '../views/LogViewerDialog';
import { RunTraceDialog } from '../views/RunTraceDialog';
import TimelineOutlinedIcon from '@mui/icons-material/TimelineOutlined';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import DownloadIcon from '@mui/icons-material/Download';
import SaveIcon from '@mui/icons-material/Save';
import FolderOpenIcon from '@mui/icons-material/FolderOpen';
import RefreshIcon from '@mui/icons-material/Refresh';
import { getUrls } from './utils';
import { apiRequest } from './apiClient';
import { listWorkspaces, loadWorkspace, deleteWorkspace, saveWorkspaceState, onWorkspaceListChanged, WorkspaceLoadSupersededError } from './workspaceService';
import { getSerializableState } from './useAutoSave';
import store, { persistor } from './store';
import { UnifiedDataUploadDialog } from '../views/UnifiedDataUploadDialog';
import ChatIcon from '@mui/icons-material/Chat';
import ArticleIcon from '@mui/icons-material/Article';
import EditIcon from '@mui/icons-material/Edit';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import UploadIcon from '@mui/icons-material/Upload';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import PublicIcon from '@mui/icons-material/Public';
import MoreVertIcon from '@mui/icons-material/MoreVert';
import TerminalOutlinedIcon from '@mui/icons-material/TerminalOutlined';
import TranslateIcon from '@mui/icons-material/Translate';
import CheckIcon from '@mui/icons-material/Check';
import { useTranslation } from 'react-i18next';
import { syncVegaLocale } from '../i18n/vega-locale';
import { buttonVar, iconVar, textVar } from './layout';

const AppBar = styled(MuiAppBar)(({ theme }) => ({
    color: 'black',
    backgroundColor: "transparent",
    //borderBottom: "1px solid #C3C3C3",
    boxShadow: "none",
    transition: theme.transitions.create(['margin', 'width'], {
        easing: theme.transitions.easing.sharp,
        duration: theme.transitions.duration.leavingScreen,
    }),
}));

const TopNavButton: FC<{ to: string; label: string; selected: boolean }> = ({ to, label, selected }) => (
    <Button
        component={RouterLink}
        to={to}
        aria-current={selected ? 'page' : undefined}
        onClick={(event) => {
            if (selected) {
                event.preventDefault();
            }
        }}
        sx={{
            textDecoration: 'none',
            textTransform: 'none',
            fontSize: textVar.md,
            fontWeight: 400,
            border: 'none',
            borderRadius: 0,
            px: 1.5,
            py: 0.5,
            minWidth: 'auto',
            cursor: selected ? 'default' : 'pointer',
            color: selected ? 'text.primary' : 'text.secondary',
            backgroundColor: selected ? 'rgba(0, 0, 0, 0.08)' : 'transparent',
            '&:hover': {
                color: 'text.primary',
                backgroundColor: selected ? 'rgba(0, 0, 0, 0.08)' : 'rgba(0, 0, 0, 0.04)',
            },
        }}
    >
        {label}
    </Button>
);

declare module '@mui/material/styles' {
    interface PaletteColor {
        bgcolor?: string;
        textColor?: string;
    }
    interface SimplePaletteColorOptions {
        bgcolor?: string;
        textColor?: string;
    }
    interface Palette {
        derived: Palette['primary'];
        custom: Palette['primary'];
    }
    interface PaletteOptions {
        derived: PaletteOptions['primary'];
        custom: PaletteOptions['primary'];
    }
}

export const toolName = "Alidade"

const LANGUAGE_LABELS: Record<string, string> = {
    en: 'EN',
    zh: '中文',
    ja: '日本語',
    ko: '한국어',
    fr: 'FR',
    de: 'DE',
};

const LanguageSwitcher: React.FC = () => {
    const { i18n } = useTranslation();
    const availableLanguages = useSelector(
        (state: AlidadeState) => state.serverConfig.AVAILABLE_LANGUAGES
    );

    if (!availableLanguages || availableLanguages.length <= 1) return null;

    return (
        <ToggleButtonGroup
            value={i18n.language.split('-')[0]}
            exclusive
            onChange={(_, value) => value && i18n.changeLanguage(value)}
            size="small"
            sx={{ 
                height: '28px', 
                my: 'auto',
                '& .MuiToggleButton-root': {
                    textTransform: 'none',
                    fontSize: textVar.sm,
                    py: 0,
                    minWidth: '40px',
                    color: 'text.secondary',
                    borderColor: 'divider',
                    '&.Mui-selected': {
                        color: 'text.primary',
                    },
                },
            }}
        >
            {availableLanguages.map(lang => (
                <ToggleButton key={lang} value={lang}>
                    {LANGUAGE_LABELS[lang] || lang.toUpperCase()}
                </ToggleButton>
            ))}
        </ToggleButtonGroup>
    );
};

/**
 * Below this toolbar width the app bar collapses to a phone-style layout:
 * the page switcher becomes a dropdown and the trailing controls fold into
 * a single overflow menu. Measured on the toolbar itself (not the viewport)
 * because the app shell floors its content width and scrolls horizontally.
 */
const COMPACT_TOOLBAR_WIDTH = 900;

const useIsNarrow = (ref: React.RefObject<HTMLElement | null>, threshold: number) => {
    const [narrow, setNarrow] = useState(false);
    useEffect(() => {
        const element = ref.current;
        if (!element || typeof ResizeObserver === 'undefined') return;
        const update = () => setNarrow(element.getBoundingClientRect().width < threshold);
        update();
        const ro = new ResizeObserver(update);
        ro.observe(element);
        return () => ro.disconnect();
    }, [ref, threshold]);
    return narrow;
};

const menuItemSx = { fontSize: textVar.md, minHeight: 34, py: 0.5 };

/** Language options rendered as menu rows for the compact overflow menu. */
const LanguageMenuItems: React.FC<{ onSelect: () => void }> = ({ onSelect }) => {
    const { i18n } = useTranslation();
    const availableLanguages = useSelector(
        (state: AlidadeState) => state.serverConfig.AVAILABLE_LANGUAGES
    );

    if (!availableLanguages || availableLanguages.length <= 1) return null;
    const current = i18n.language.split('-')[0];

    return (
        <>
            {availableLanguages.map(lang => (
                <MenuItem
                    key={lang}
                    selected={lang === current}
                    onClick={() => { i18n.changeLanguage(lang); onSelect(); }}
                    sx={menuItemSx}
                >
                    <ListItemIcon>
                        {lang === current
                            ? <CheckIcon fontSize="small" />
                            : <TranslateIcon fontSize="small" sx={{ opacity: 0.3 }} />}
                    </ListItemIcon>
                    <ListItemText primaryTypographyProps={{ fontSize: textVar.md }}>
                        {LANGUAGE_LABELS[lang] || lang.toUpperCase()}
                    </ListItemText>
                </MenuItem>
            ))}
        </>
    );
};

/** Compact replacement for the About / App top-nav buttons. */
const PageNavMenu: React.FC<{ isAboutPage: boolean }> = ({ isAboutPage }) => {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);
    const pages = [
        { to: '/about', label: t('appBar.about'), selected: isAboutPage },
        { to: '/app', label: t('appBar.app'), selected: !isAboutPage },
    ];
    const currentLabel = pages.find(page => page.selected)?.label ?? '';

    return (
        <>
            <Button
                color="inherit"
                onClick={(event) => setAnchorEl(event.currentTarget)}
                endIcon={<KeyboardArrowDownIcon sx={{ fontSize: iconVar.md, color: 'text.secondary' }} />}
                aria-haspopup="menu"
                sx={{
                    textTransform: 'none',
                    minWidth: 0,
                    px: 0.75,
                    gap: 0.25,
                    '&:hover': { backgroundColor: 'rgba(0, 0, 0, 0.04)' },
                }}
            >
                <Typography noWrap component="h1" sx={{ fontSize: textVar.xl, fontWeight: 300, letterSpacing: '0.03em' }}>
                    {toolName}
                </Typography>
                <Typography noWrap sx={{ fontSize: textVar.md, color: 'text.secondary' }}>
                    {`: ${currentLabel}`}
                </Typography>
            </Button>
            <Menu
                anchorEl={anchorEl}
                open={Boolean(anchorEl)}
                onClose={() => setAnchorEl(null)}
                anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
                transformOrigin={{ vertical: 'top', horizontal: 'left' }}
            >
                {pages.map(page => (
                    <MenuItem
                        key={page.to}
                        selected={page.selected}
                        sx={menuItemSx}
                        onClick={() => {
                            setAnchorEl(null);
                            if (!page.selected) navigate(page.to);
                        }}
                    >
                        <ListItemIcon>
                            {page.selected ? <CheckIcon fontSize="small" /> : null}
                        </ListItemIcon>
                        <ListItemText primaryTypographyProps={{ fontSize: textVar.md }}>
                            {`${toolName}: ${page.label}`}
                        </ListItemText>
                    </MenuItem>
                ))}
            </Menu>
        </>
    );
};

/**
 * Phone-style overflow menu holding everything that does not fit in a narrow
 * app bar (language, settings, logs, links, exit).
 */
const ToolbarOverflowMenu: React.FC<{
    items: {
        key: string;
        label: string;
        icon: React.ReactNode;
        href?: string;
        onClick?: () => void;
    }[];
    showLanguages?: boolean;
}> = ({ items, showLanguages = true }) => {
    const { t } = useTranslation();
    const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);
    const close = () => setAnchorEl(null);

    return (
        <>
            <Tooltip title={t('appBar.moreOptions', { defaultValue: 'More options' })}>
                <IconButton
                    size="small"
                    onClick={(event) => setAnchorEl(event.currentTarget)}
                    aria-haspopup="menu"
                    aria-label={t('appBar.moreOptions', { defaultValue: 'More options' })}
                    sx={{
                        p: 0.5,
                        color: 'text.secondary',
                        '&:hover': { color: 'text.primary', backgroundColor: 'rgba(0, 0, 0, 0.04)' },
                    }}
                >
                    <MoreVertIcon fontSize="small" />
                </IconButton>
            </Tooltip>
            <Menu
                anchorEl={anchorEl}
                open={Boolean(anchorEl)}
                onClose={close}
                anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
                transformOrigin={{ vertical: 'top', horizontal: 'right' }}
                slotProps={{ paper: { sx: { minWidth: 200 } } }}
            >
                {showLanguages && <LanguageMenuItems onSelect={close} />}
                {showLanguages && items.length > 0 && <Divider />}
                {items.map(item => (
                    <MenuItem
                        key={item.key}
                        sx={menuItemSx}
                        {...(item.href
                            ? { component: 'a' as const, href: item.href, target: '_blank', rel: 'noopener noreferrer' }
                            : {})}
                        onClick={() => { close(); item.onClick?.(); }}
                    >
                        <ListItemIcon>{item.icon}</ListItemIcon>
                        <ListItemText primaryTypographyProps={{ fontSize: textVar.md }}>{item.label}</ListItemText>
                    </MenuItem>
                ))}
            </Menu>
        </>
    );
};

export interface AppFCProps {
}

// Extract menu components into separate components to prevent full app re-renders
const TableMenu: React.FC = () => {
    const [dialogOpen, setDialogOpen] = useState<boolean>(false);
    const { t } = useTranslation();
    
    return (
        <>
            <Button
                variant="text"
                onClick={() => setDialogOpen(true)}
                sx={{ textTransform: 'none' }}
            >
                {t('appBar.data')}
            </Button>
            
            {/* Unified Data Upload Dialog */}
            <UnifiedDataUploadDialog 
                open={dialogOpen}
                onClose={() => setDialogOpen(false)}
                initialTab="menu"
            />
        </>
    );
};


const WorkspacePickerDialog: React.FC<{open: boolean, onClose: () => void}> = ({open, onClose}) => {
    const [workspaces, setWorkspaces] = useState<{id: string, display_name: string, saved_at: string}[]>([]);
    const [loading, setLoading] = useState(false);
    const [listLoading, setListLoading] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
    const dispatch = useDispatch();
    const activeWorkspace = useSelector((state: AlidadeState) => state.activeWorkspace);
    const { t } = useTranslation();

    const fetchWsList = useCallback(async () => {
        setListLoading(true);
        try {
            const sessions = await listWorkspaces();
            setWorkspaces(sessions as any);
        } catch (e) { /* ignore */ }
        setListLoading(false);
    }, []);

    useEffect(() => {
        if (!open) return;
        fetchWsList();
    }, [open, fetchWsList]);

    useEffect(() => {
        if (!open) return;
        return onWorkspaceListChanged(fetchWsList);
    }, [open, fetchWsList]);

    const handleOpen = async (wsId: string) => {
        if (activeWorkspace?.id === wsId) { onClose(); return; }
        try { await saveWorkspaceState(getSerializableState(store.getState())); } catch { /* best effort */ }
        const wsEntry = workspaces.find(w => w.id === wsId);
        setLoading(true);
        dispatch(dfActions.setSessionLoading({ loading: true, label: t('workspace.openingWorkspace') }));
        onClose();
        try {
            const result = await loadWorkspace(wsId);
            if (result) {
                const displayName = result.displayName || wsEntry?.display_name || wsId;
                dispatch(dfActions.loadState({ ...result.state, activeWorkspace: { id: wsId, displayName, readOnly: result.readOnly } }));
                dispatch(dfActions.addMessages({ timestamp: Date.now(), component: "Workspace", type: "success", value: t('workspace.openedSession', { name: displayName }) }));
            } else {
                dispatch(dfActions.addMessages({ timestamp: Date.now(), component: "Workspace", type: "error", value: t('workspace.failedToOpenWorkspace') }));
            }
        } catch (e) {
            if (e instanceof WorkspaceLoadSupersededError) {
                setLoading(false);
                return;
            }
            dispatch(dfActions.addMessages({ timestamp: Date.now(), component: "Workspace", type: "error", value: t('workspace.failedToOpenWorkspace') }));
        }
        setLoading(false);
        dispatch(dfActions.setSessionLoading({ loading: false }));
    };

    const handleCreate = () => {
        dispatch(dfActions.resetState());
        onClose();
    };

    const handleDelete = async (workspaceId: string) => {
        try {
            await deleteWorkspace(workspaceId);
            setWorkspaces(prev => prev.filter(s => s.id !== workspaceId));
            dispatch(dfActions.addMessages({ timestamp: Date.now(), component: "Workspace", type: "success", value: t('workspace.deletedSession', { name: workspaceId }) }));
        } catch (e) { /* ignore */ }
        setConfirmDelete(null);
    };

    return (
        <Dialog open={open} onClose={onClose} maxWidth="xs" fullWidth>
            <DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                {t('workspace.sessions')}
                <Tooltip title={t('workspace.refreshList')}>
                    <IconButton size="small" onClick={fetchWsList} disabled={listLoading} sx={{ color: 'text.secondary' }}>
                        {listLoading ? <CircularProgress size={18} /> : <RefreshIcon fontSize="small" />}
                    </IconButton>
                </Tooltip>
            </DialogTitle>
            <DialogContent sx={{ px: 1 }}>
                {listLoading && workspaces.length === 0 ? (
                    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', py: 4, gap: 1.5 }}>
                        <CircularProgress size={28} />
                        <Typography variant="body2" color="text.secondary">{t('workspace.loadingSessions')}</Typography>
                    </Box>
                ) : (
                    <>
                        {/* New session — same row style as session items */}
                        <Box
                            sx={{
                                display: 'flex', alignItems: 'center',
                                px: 1.5, py: 1, mx: 0, my: 0.5, borderRadius: 1, cursor: 'pointer',
                                '&:hover': { backgroundColor: 'action.hover' },
                                transition: 'background-color 0.15s',
                            }}
                            onClick={handleCreate}
                        >
                            <Typography variant="body2" color="primary" sx={{ fontWeight: 500 }}>
                                {t('workspace.newSession')}
                            </Typography>
                        </Box>
                        {workspaces.length > 0 && <Divider sx={{ my: 0.5 }} />}
                        {workspaces.map(s => (
                        <Box
                            key={s.id}
                            sx={{
                                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                                px: 1.5, py: 1, mx: 0, my: 0.5, borderRadius: 1, cursor: 'pointer',
                                backgroundColor: activeWorkspace?.id === s.id ? 'action.selected' : 'transparent',
                                '&:hover': { backgroundColor: activeWorkspace?.id === s.id ? 'action.selected' : 'action.hover' },
                                transition: 'background-color 0.15s',
                            }}
                            onClick={() => handleOpen(s.id)}
                        >
                            <Box sx={{ flex: 1, minWidth: 0 }}>
                                <Typography variant="body2" fontWeight={activeWorkspace?.id === s.id ? 'bold' : 'normal'} noWrap>
                                    {s.display_name} {activeWorkspace?.id === s.id ? t('workspace.active') : ''}
                                </Typography>
                                {s.saved_at && (
                                    <Typography variant="caption" color="text.secondary">
                                        {new Date(s.saved_at).toLocaleString()}
                                    </Typography>
                                )}
                            </Box>
                            {activeWorkspace?.id !== s.id && (
                                confirmDelete === s.id ? (
                                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }} onClick={e => e.stopPropagation()}>
                                        <Button size="small" color="error" sx={{ minWidth: 0, fontSize: textVar.xs, textTransform: 'none' }}
                                            onClick={() => handleDelete(s.id)}>{t('workspace.delete')}</Button>
                                        <Button size="small" sx={{ minWidth: 0, fontSize: textVar.xs, textTransform: 'none' }}
                                            onClick={() => setConfirmDelete(null)}>{t('workspace.cancel')}</Button>
                                    </Box>
                                ) : (
                                    <Tooltip title={t('workspace.deleteSession')}>
                                        <IconButton size="small" onClick={(e) => { e.stopPropagation(); setConfirmDelete(s.id); }} sx={{ color: 'text.secondary' }}>
                                            <ClearIcon fontSize="small" />
                                        </IconButton>
                                    </Tooltip>
                                )
                            )}
                        </Box>
                    ))
                    }
                    </>
                )}
            </DialogContent>
            <DialogActions>
                <Button onClick={onClose}>{t('workspace.close')}</Button>
            </DialogActions>
        </Dialog>
    );
};

const WorkspaceMenu: React.FC = () => {
    const [pickerOpen, setPickerOpen] = useState(false);
    const activeWorkspace = useSelector((state: AlidadeState) => state.activeWorkspace);
    const serverConfig = useSelector((state: AlidadeState) => state.serverConfig);
    const { t } = useTranslation();
    const diskPersistenceDisabled = false; // all backends support workspace switching

    console.log('Rendering WorkspaceMenu, activeWorkspace:', activeWorkspace, 'serverConfig:', serverConfig); // Debug log for rendering and state
    console.log(serverConfig); // Debug log for serverConfig
    console.log(activeWorkspace); // Debug log for activeWorkspace

    if (!activeWorkspace) return null;

    return (
        <>
            <Tooltip title={t('workspace.sessionTooltip', { name: activeWorkspace?.id || '' })} placement="bottom">
                <Box 
                    onClick={() => !diskPersistenceDisabled && setPickerOpen(true)}
                    sx={{ 
                        display: 'flex', alignItems: 'center', gap: 0.5,
                        cursor: 'pointer',
                        px: 1,
                        py: 0.25,
                        borderRadius: 1,
                        '&:hover': { backgroundColor: 'rgba(0,0,0,0.04)' },
                        '&:hover .ws-chevron': { opacity: 1 },
                    }}
                >
                    <Typography noWrap sx={{ 
                        fontSize: textVar.lg, 
                        fontWeight: 500, 
                        color: 'text.primary',
                        maxWidth: 280,
                        letterSpacing: '0.01em',
                    }}>
                        {activeWorkspace?.displayName || activeWorkspace?.id}
                    </Typography>
                    <KeyboardArrowDownIcon className="ws-chevron" sx={{ fontSize: iconVar.md, color: 'text.secondary', opacity: 0.4, transition: 'opacity 0.15s' }} />
                </Box>
            </Tooltip>
            <WorkspacePickerDialog open={pickerOpen} onClose={() => setPickerOpen(false)} />
        </>
    );
};

// Exit the current session and return to the front-page (no workspace).
// Saves work first so the session is recoverable from the workspace picker —
// unless the session is empty, in which case it's discarded rather than left
// behind as an untitled shell in the picker.
const useExitSession = () => {
    const dispatch = useDispatch();
    const state = useSelector((s: AlidadeState) => s);
    const sessionEmpty = useSelector(dfSelectors.selectSessionEmpty);

    return useCallback(async () => {
        const workspaceId = state.activeWorkspace?.id;
        if (sessionEmpty) {
            if (workspaceId) {
                try { await deleteWorkspace(workspaceId); } catch { /* may never have been created */ }
            }
        } else {
            try { await saveWorkspaceState(getSerializableState(state)); } catch { /* best effort */ }
        }
        dispatch(dfActions.resetState());
    }, [state, sessionEmpty, dispatch]);
};

const ExitSessionButton: React.FC = () => {
    const { t } = useTranslation();
    const handleExit = useExitSession();

    return (
        <Tooltip title={t('workspace.exitSessionTooltip', { defaultValue: 'Exit session and return to the workspace picker' })} placement="bottom">
            <Button
                size="small"
                variant="text"
                onClick={handleExit}
                startIcon={<LogoutIcon sx={{ fontSize: iconVar.md }} />}
                sx={{
                    textTransform: 'none',
                    fontSize: textVar.md,
                    fontWeight: 400,
                    px: 1.5,
                    py: 0.5,
                    minWidth: 'auto',
                    lineHeight: 1.5,
                    color: 'text.secondary',
                    '&:hover': { color: 'text.primary', backgroundColor: 'rgba(0, 0, 0, 0.04)' },
                }}
            >
                {t('workspace.exit', { defaultValue: 'Exit' })}
            </Button>
        </Tooltip>
    );
};

/**
 * Settings dialog. Renders its own icon-button trigger by default; the
 * compact toolbar hides the trigger and drives `open` from its overflow menu.
 */
const ConfigDialog: React.FC<{
    open?: boolean;
    onOpenChange?: (open: boolean) => void;
    hideTrigger?: boolean;
}> = ({ open: openProp, onOpenChange, hideTrigger = false }) => {
    const [openState, setOpenState] = useState(false);
    const open = openProp ?? openState;
    const setOpen = useCallback((value: boolean) => {
        setOpenState(value);
        onOpenChange?.(value);
    }, [onOpenChange]);
    const dispatch = useDispatch();
    const { t } = useTranslation();
    const config = useSelector((state: AlidadeState) => state.config);
    const rowLimitDefault = DEFAULT_ROW_LIMIT;
    const rowLimitMax = DEFAULT_ROW_LIMIT;


    const [formulateTimeoutSeconds, setFormulateTimeoutSeconds] = useState(config.formulateTimeoutSeconds ?? 180);
    const [defaultChartWidth, setDefaultChartWidth] = useState(config.defaultChartWidth ?? 300);
    const [defaultChartHeight, setDefaultChartHeight] = useState(config.defaultChartHeight ?? 300);
    const [maxStretchFactor, setMaxStretchFactor] = useState(config.maxStretchFactor ?? 1.5);
    const [frontendRowLimit, setFrontendRowLimit] = useState(config.frontendRowLimit ?? rowLimitDefault);
    const [paletteKey, setPaletteKey] = useState(
        (config.paletteKey && palettes[config.paletteKey]) ? config.paletteKey : defaultPaletteKey
    );
    const [agentSettings, setAgentSettings] = useState<AgentSettings>(config.agentSettings ?? {});
    // reopening the dialog starts from what is saved
    useEffect(() => { if (open) setAgentSettings(config.agentSettings ?? {}); }, [open, config.agentSettings]);

    const hasChanges = formulateTimeoutSeconds !== config.formulateTimeoutSeconds || 
                      defaultChartWidth !== config.defaultChartWidth ||
                      defaultChartHeight !== config.defaultChartHeight ||
                      maxStretchFactor !== config.maxStretchFactor ||
                      frontendRowLimit !== config.frontendRowLimit ||
                      !sameSettings(agentSettings, config.agentSettings) ||
                      paletteKey !== ((config.paletteKey && palettes[config.paletteKey]) ? config.paletteKey : defaultPaletteKey);

    return (
        <>
            {!hideTrigger && (
            <Tooltip title={t('app.settings')}>
                <IconButton
                    size="small"
                    onClick={() => setOpen(true)}
                    aria-label={t('app.settings')}
                    sx={{
                        p: 0.5,
                        color: 'text.secondary',
                        '&:hover': { color: 'text.primary', backgroundColor: 'rgba(0, 0, 0, 0.04)' },
                    }}
                >
                    <SettingsOutlinedIcon fontSize="small" />
                </IconButton>
            </Tooltip>
            )}
            <Dialog onClose={() => setOpen(false)} open={open}>
                <DialogTitle>{t('app.settings')}</DialogTitle>
                <DialogContent>
                    <Box sx={{ 
                        display: 'flex', 
                        flexDirection: 'column', 
                        gap: 3,
                        maxWidth: 400
                    }}>
                        <Divider><Typography variant="caption">{t('config.frontend')}</Typography></Divider>
                        <FormControl fullWidth size="small">
                            <InputLabel id="palette-select-label" sx={{ fontSize: textVar.md }}>{t('config.colorTheme')}</InputLabel>
                            <Select
                                labelId="palette-select-label"
                                value={paletteKey}
                                label={t('config.colorTheme')}
                                onChange={(e) => setPaletteKey(e.target.value)}
                                sx={{ fontSize: textVar.md }}
                                renderValue={(key) => {
                                    const p = palettes[key];
                                    return (
                                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                                            <Box sx={{ width: 12, height: 12, borderRadius: '50%', backgroundColor: p.primary.main, flexShrink: 0 }} />
                                            <Box sx={{ width: 12, height: 12, borderRadius: '50%', backgroundColor: p.custom.main, flexShrink: 0 }} />
                                            <Typography sx={{ fontSize: textVar.md }}>{p.name}</Typography>
                                        </Box>
                                    );
                                }}
                            >
                                {paletteKeys.map(key => {
                                    const p = palettes[key];
                                    return (
                                        <MenuItem key={key} value={key} sx={{ py: 0.5 }}>
                                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mr: 1.5 }}>
                                                <Box sx={{ width: 14, height: 14, borderRadius: '50%', backgroundColor: p.primary.main, border: '1px solid rgba(0,0,0,0.1)' }} />
                                                <Box sx={{ width: 14, height: 14, borderRadius: '50%', backgroundColor: p.custom.main, border: '1px solid rgba(0,0,0,0.1)' }} />
                                            </Box>
                                            <ListItemText primary={p.name} slotProps={{ primary: { sx: { fontSize: textVar.md } } }} />
                                        </MenuItem>
                                    );
                                })}
                            </Select>
                        </FormControl>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                            <Box sx={{ flex: 1 }}>
                                <TextField
                                    label={t('config.defaultChartWidth')}
                                    type="number"
                                    variant="outlined"
                                    value={defaultChartWidth}
                                    onChange={(e) => {
                                        const value = parseInt(e.target.value);
                                        setDefaultChartWidth(value);
                                    }}
                                    fullWidth
                                    slotProps={{
                                        input: {
                                            inputProps: {
                                                min: 100,
                                                max: 1000
                                            }
                                        }
                                    }}
                                    error={defaultChartWidth < 100 || defaultChartWidth > 1000}
                                    helperText={defaultChartWidth < 100 || defaultChartWidth > 1000 ? 
                                        t('config.chartSizeRangeError') : ""}
                                />
                            </Box>
                            <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
                                <ClearIcon fontSize="small" />
                            </Typography>
                            <Box sx={{ flex: 1 }}>
                                <TextField
                                    label={t('config.defaultChartHeight')}
                                    type="number"
                                    variant="outlined"
                                    value={defaultChartHeight}
                                    onChange={(e) => {
                                        const value = parseInt(e.target.value);
                                        setDefaultChartHeight(value);
                                    }}
                                    fullWidth
                                    slotProps={{
                                        input: {
                                            inputProps: {
                                                min: 100,
                                                max: 1000
                                            }
                                        }
                                    }}
                                    error={defaultChartHeight < 100 || defaultChartHeight > 1000}
                                    helperText={defaultChartHeight < 100 || defaultChartHeight > 1000 ? 
                                        t('config.chartSizeRangeError') : ""}
                                />
                            </Box>
                        </Box>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                            <Box sx={{ flex: 1 }}>
                                <TextField
                                    label={t('config.localRowLimit')}
                                    type="number"
                                    variant="outlined"
                                    value={frontendRowLimit}
                                    onChange={(e) => {
                                        const value = parseInt(e.target.value);
                                        setFrontendRowLimit(value);
                                    }}
                                    fullWidth
                                    slotProps={{
                                        input: {
                                            inputProps: {
                                                min: 100,
                                                max: rowLimitMax
                                            }
                                        }
                                    }}
                                    error={frontendRowLimit < 100 || frontendRowLimit > rowLimitMax}
                                    helperText={frontendRowLimit < 100 || frontendRowLimit > rowLimitMax ? 
                                        t('config.localRowLimitRangeError') : ""}
                                />
                                <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
                                    {t('config.localRowLimitHint')}
                                </Typography>
                            </Box>
                        </Box>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                            <Box sx={{ flex: 1 }}>
                                <TextField
                                    label={t('config.maxStretchFactor')}
                                    type="number"
                                    variant="outlined"
                                    value={maxStretchFactor}
                                    onChange={(e) => {
                                        const value = parseFloat(e.target.value);
                                        setMaxStretchFactor(value);
                                    }}
                                    fullWidth
                                    slotProps={{
                                        input: {
                                            inputProps: {
                                                min: 1,
                                                max: 5,
                                                step: 0.1
                                            }
                                        }
                                    }}
                                    error={isNaN(maxStretchFactor) || maxStretchFactor < 1 || maxStretchFactor > 5}
                                    helperText={isNaN(maxStretchFactor) || maxStretchFactor < 1 || maxStretchFactor > 5 ? 
                                        t('config.maxStretchFactorRangeError') : ""}
                                />
                                <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
                                    {t('config.maxStretchFactorHint')}
                                </Typography>
                            </Box>
                        </Box>
                        <Divider><Typography variant="caption">{t('config.backend')}</Typography></Divider>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                            <Box sx={{ flex: 1 }}>
                                <TextField
                                    label={t('config.formulateTimeout')}
                                    type="number"
                                    variant="outlined"
                                    value={formulateTimeoutSeconds}
                                    onChange={(e) => {
                                        const value = parseInt(e.target.value);
                                        setFormulateTimeoutSeconds(value);
                                    }}
                                    inputProps={{
                                        min: 0,
                                        max: 3600,
                                    }}
                                    error={formulateTimeoutSeconds <= 0 || formulateTimeoutSeconds > 3600}
                                    helperText={formulateTimeoutSeconds <= 0 || formulateTimeoutSeconds > 3600 ? 
                                        t('config.formulateTimeoutRangeError') : ""}
                                    fullWidth
                                />
                                <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
                                    {t('config.formulateTimeoutHint')}
                                </Typography>
                            </Box>
                        </Box>
                        <Divider><Typography variant="caption">{t('config.agent.title')}</Typography></Divider>
                        <AgentSettingsSection value={agentSettings} onChange={setAgentSettings} />
                    </Box>
                </DialogContent>
                <DialogActions sx={{'.MuiButton-root': {textTransform: 'none'}}}>
                    <Button sx={{marginRight: 'auto'}} onClick={() => {
                        setFormulateTimeoutSeconds(180);
                        setDefaultChartWidth(300);
                        setDefaultChartHeight(300);
                        setMaxStretchFactor(2.0);
                        setFrontendRowLimit(rowLimitDefault);
                        setPaletteKey(defaultPaletteKey);
                        setAgentSettings({});
                    }}>{t('session.resetToDefault')}</Button>
                    <Button onClick={() => setOpen(false)}>{t('app.cancel')}</Button>
                    <Button 
                        variant={hasChanges ? "contained" : "text"}
                        disabled={!hasChanges || isNaN(formulateTimeoutSeconds) || formulateTimeoutSeconds <= 0 || formulateTimeoutSeconds > 3600
                            || isNaN(defaultChartWidth) || defaultChartWidth <= 0 || defaultChartWidth > 1000
                            || isNaN(defaultChartHeight) || defaultChartHeight <= 0 || defaultChartHeight > 1000
                            || isNaN(maxStretchFactor) || maxStretchFactor < 1 || maxStretchFactor > 5
                            || isNaN(frontendRowLimit) || frontendRowLimit < 100 || frontendRowLimit > rowLimitMax}
                        onClick={() => {
                            dispatch(dfActions.setConfig({formulateTimeoutSeconds, defaultChartWidth, defaultChartHeight, maxStretchFactor, frontendRowLimit, paletteKey,
                                agentSettings: compactSettings(agentSettings)}));
                            setOpen(false);
                        }}
                    >
                        {t('app.apply')}
                    </Button>
                </DialogActions>
            </Dialog>
        </>
    );  
}

const ErrorBoundaryFallback: React.FC = () => {
    const { t } = useTranslation();
    const routeError = useRouteError() as any;
    const [logsOpen, setLogsOpen] = useState(false);
    const [detailsOpen, setDetailsOpen] = useState(false);
    // Read the desktop flag off the URL rather than the store — the store may be
    // exactly what failed, and this screen has to render regardless.
    const isDesktopApp = new URLSearchParams(window.location.search).get('desktop') === '1';
    const detail = routeError?.message || (typeof routeError === 'string' ? routeError : '');
    const stack = typeof routeError?.stack === 'string' ? routeError.stack : '';
    const mutedActionSx = {
        minWidth: 0, px: 0.5,
        color: 'text.disabled', fontSize: '0.7rem', fontWeight: 400,
        textTransform: 'none',
        '&:hover': { color: 'text.secondary', backgroundColor: 'transparent' },
    } as const;
    return (
        <Box sx={{ width: "100%", height: "100%", display: "flex" }}>
            <Box sx={{
                margin: "150px auto",
                display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1,
                maxWidth: 640, px: 2,
            }}>
                <Typography color="gray">
                    {t('workspace.errorOccurred')} <Link href="/app">{t('workspace.refreshSession')}</Link>{'. '}{t('workspace.errorPersistHint')}
                </Typography>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    {(detail || stack) && (
                        <Button
                            variant="text"
                            size="small"
                            startIcon={<KeyboardArrowDownIcon sx={{
                                fontSize: 15,
                                transform: detailsOpen ? 'rotate(180deg)' : 'none',
                                transition: 'transform 0.15s',
                            }} />}
                            onClick={() => setDetailsOpen(open => !open)}
                            sx={mutedActionSx}
                        >
                            View error details
                        </Button>
                    )}
                    {isDesktopApp && (
                        <Button
                            variant="text"
                            size="small"
                            startIcon={<TerminalOutlinedIcon sx={{ fontSize: 15 }} />}
                            onClick={() => setLogsOpen(true)}
                            sx={mutedActionSx}
                        >
                            View backend log
                        </Button>
                    )}
                </Box>
                {detailsOpen && (detail || stack) && (
                    <Typography
                        component="pre"
                        sx={{
                            fontFamily: 'var(--df-font-mono)', fontSize: '0.65rem', color: 'text.disabled',
                            whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                            m: 0, maxHeight: 260, overflowY: 'auto', textAlign: 'left',
                            width: '100%',
                        }}
                    >
                        {[detail, stack].filter(Boolean).join('\n\n')}
                    </Typography>
                )}
                {isDesktopApp && <LogViewerDialog open={logsOpen} onOpenChange={setLogsOpen} hideTrigger />}
            </Box>
        </Box>
    );
};

const AUTH_ERROR_MESSAGES: Record<string, string> = {
    access_denied: 'auth.ssoErrorAccessDenied',
    invalid_state: 'auth.ssoErrorInvalidState',
    invalid_client: 'auth.ssoErrorInvalidClient',
    token_exchange_failed: 'auth.ssoErrorTokenExchange',
    missing_token_endpoint: 'auth.ssoErrorMissingEndpoint',
};

const AppShell: FC = () => {
    const dispatch = useDispatch<AppDispatch>();
    const { t } = useTranslation();
    const location = useLocation();
    const [searchParams, setSearchParams] = useSearchParams();
    const viewMode = useSelector((state: AlidadeState) => state.viewMode);
    const tables = useSelector(dfSelectors.getAllTables);
    const activeWorkspace = useSelector((state: AlidadeState) => state.activeWorkspace);
    const serverConfig = useSelector((state: AlidadeState) => state.serverConfig);

    useEffect(() => {        const authError = searchParams.get('auth_error');
        if (!authError) return;
        const i18nKey = AUTH_ERROR_MESSAGES[authError] || 'auth.ssoErrorGeneric';
        dispatch(dfActions.addMessages({
            type: 'error',
            component: 'auth',
            timestamp: Date.now(),
            value: t(i18nKey, { defaultValue: 'SSO login failed. Please contact your administrator.' }),
        }));
        searchParams.delete('auth_error');
        setSearchParams(searchParams, { replace: true });
    }, []);

    // Auto-persist session state to the active workspace (debounced)
    useAutoSave();
    // Auto-name workspace after first table + model are available
    useWorkspaceAutoName();
    const generatedReports = useSelector((state: AlidadeState) => state.generatedReports);

    const isAboutPage = location.pathname === '/about';
    const isAppPage = !isAboutPage;

    // The desktop canvas (threads, encoding shelf, viz cards) genuinely needs
    // room, so the app shell floors content at MIN_SUPPORTED. Landing and phone
    // workspace views reflow instead; the media override below removes the
    // desktop floor when Thread and Canvas become alternate full-width views.
    const isLandingView = isAppPage && !activeWorkspace;
    const shellMinWidth = isLandingView ? 0 : `${MIN_SUPPORTED.width}px`;

    // Narrow toolbars fold their controls into menus instead of letting the
    // nav buttons, session name and trailing actions overlap.
    const toolbarRef = useRef<HTMLDivElement | null>(null);
    const isCompactToolbar = useIsNarrow(toolbarRef, COMPACT_TOOLBAR_WIDTH);
    const [settingsOpen, setSettingsOpen] = useState(false);
    const [tracesOpen, setTracesOpen] = useState(false);
    const [logsOpen, setLogsOpen] = useState(false);
    const exitSession = useExitSession();
    const inSession = isAppPage && !!activeWorkspace;

    return (
        <Box sx={{
            position: 'absolute',
            backgroundColor: 'rgba(255, 255, 255, 0.3)',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            overflow: 'auto',
            '& > *': {
                minWidth: shellMinWidth,
                minHeight: `${MIN_SUPPORTED.height}px`,
                '@media (max-width: 700px)': {
                    minWidth: 0,
                },
            },
        }}>
            <Box sx={{
                display: 'flex',
                flexDirection: 'column',
                height: '100%',
                width: '100%',
                overflow: 'hidden'
            }}>
                <AppBar position="static">
                    <Toolbar ref={toolbarRef} variant="dense" sx={{ height: 40, minHeight: 36, position: 'relative', pl: '0px !important' }}>
                        <Box sx={{ width: 40, minWidth: 40, display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
                            <Box component="img" sx={{ height: 20 }} alt="" src={dfLogo} />
                        </Box>
                        {isCompactToolbar ? (
                            <PageNavMenu isAboutPage={isAboutPage} />
                        ) : (
                        <>
                        <Button sx={{
                            display: "flex", flexDirection: "row", textTransform: "none",
                            alignItems: 'stretch',
                            backgroundColor: 'transparent',
                            minWidth: 0,
                            px: 0.5,
                            "&:hover": {
                                backgroundColor: "transparent"
                            }
                        }} color="inherit">
                            <Typography noWrap component="h1" sx={{ fontWeight: 300, display: { xs: 'none', sm: 'block' }, letterSpacing: '0.03em' }}>
                                {toolName}
                            </Typography>
                        </Button>
                        <Box
                            sx={{
                                ml: 2,
                                height: '28px',
                                my: 'auto',
                                display: 'flex',
                            }}
                        >
                            <TopNavButton to="/about" label={t('appBar.about')} selected={isAboutPage} />
                            <TopNavButton to="/app" label={t('appBar.app')} selected={isAppPage} />
                        </Box>
                        </>
                        )}
                        {/* Workspace name — session indicator/switcher. Centered
                            absolutely when there is room, otherwise it flows
                            between the nav menu and the trailing actions. */}
                        {activeWorkspace && isAppPage && (
                            isCompactToolbar ? (
                                <Box sx={{ flex: 1, minWidth: 0, display: 'flex', justifyContent: 'center', alignItems: 'center', mx: 1 }}>
                                    <WorkspaceMenu />
                                </Box>
                            ) : (
                                <Box sx={{ position: 'absolute', left: '50%', transform: 'translateX(-50%)', display: 'flex', alignItems: 'center' }}>
                                    <WorkspaceMenu />
                                </Box>
                            )
                        )}
                        {isAppPage && isCompactToolbar && (
                            <Box sx={{ display: 'flex', ml: 'auto', alignItems: 'center', gap: 0.5 }}>
                                <ModelSelectionButton />
                                <ConfigDialog open={settingsOpen} onOpenChange={setSettingsOpen} hideTrigger />
                                {serverConfig.IS_LOCAL_MODE && (
                                    <LogViewerDialog open={logsOpen} onOpenChange={setLogsOpen} hideTrigger />
                                )}
                                <RunTraceDialog open={tracesOpen} onOpenChange={setTracesOpen} hideTrigger />
                                <ToolbarOverflowMenu
                                    items={[
                                        {
                                            key: 'settings',
                                            label: t('app.settings'),
                                            icon: <SettingsOutlinedIcon fontSize="small" />,
                                            onClick: () => setSettingsOpen(true),
                                        },
                                        {
                                            key: 'traces',
                                            label: t('trace.open'),
                                            icon: <TimelineOutlinedIcon fontSize="small" />,
                                            onClick: () => setTracesOpen(true),
                                        },
                                        ...(serverConfig.IS_LOCAL_MODE ? [{
                                            key: 'logs',
                                            label: t('logs.viewLogs', { defaultValue: 'View backend log' }),
                                            icon: <TerminalOutlinedIcon fontSize="small" />,
                                            onClick: () => setLogsOpen(true),
                                        }] : []),
                                        ...(inSession ? [{
                                            key: 'exit',
                                            label: t('workspace.exit', { defaultValue: 'Exit' }),
                                            icon: <LogoutIcon fontSize="small" />,
                                            onClick: () => { exitSession(); },
                                        }] : []),
                                    ]}
                                />
                            </Box>
                        )}
                        {isAppPage && !isCompactToolbar && (
                            <Box sx={{ display: 'flex', ml: 'auto', alignItems: 'center', gap: 0.75 }}>
                                <ModelSelectionButton />
                                <Divider orientation="vertical" variant="middle" flexItem sx={{ my: 1 }} />
                                <LanguageSwitcher />
                                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.25 }}>
                                    <ConfigDialog />
                                    <RunTraceDialog />
                                    {serverConfig.IS_LOCAL_MODE && <LogViewerDialog />}
                                </Box>
                                {inSession && (
                                    <>
                                        <Divider orientation="vertical" variant="middle" flexItem sx={{ my: 1 }} />
                                        <ExitSessionButton />
                                    </>
                                )}
                            </Box>
                        )}
                        {isAboutPage && isCompactToolbar && (
                            <Box sx={{ ml: 'auto', display: 'flex', alignItems: 'center' }}>
                                {serverConfig.IS_LOCAL_MODE && (
                                    <LogViewerDialog open={logsOpen} onOpenChange={setLogsOpen} hideTrigger />
                                )}
                                {serverConfig.IS_LOCAL_MODE && (
                                    <ToolbarOverflowMenu
                                        items={[{
                                            key: 'logs',
                                            label: t('logs.viewLogs', { defaultValue: 'View backend log' }),
                                            icon: <TerminalOutlinedIcon fontSize="small" />,
                                            onClick: () => setLogsOpen(true),
                                        }]}
                                    />
                                )}
                            </Box>
                        )}
                        {isAboutPage && !isCompactToolbar && (
                            <Box sx={{ ml: 'auto', display: 'flex', gap: 0.5 }}>
                                <LanguageSwitcher />
                                {serverConfig.IS_LOCAL_MODE && <LogViewerDialog />}
                            </Box>
                        )}
                        <AuthButton />
                    </Toolbar>
                </AppBar>
                <Box sx={{ flex: 1, minHeight: 0, overflow: 'hidden', '& > div': { height: '100%' } }}>
                    <Outlet />
                </Box>
                <MessageSnackbar />
                <ChartRenderService />
            </Box>
        </Box>
    );
}

export const AppFC: FC<AppFCProps> = function AppFC(appProps) {

    const dispatch = useDispatch<AppDispatch>();
    const { t, i18n } = useTranslation();
    const rawPaletteKey = useSelector((state: AlidadeState) => state.config.paletteKey);
    const activePaletteKey = (rawPaletteKey && palettes[rawPaletteKey]) ? rawPaletteKey : defaultPaletteKey;

    const [configLoaded, setConfigLoaded] = useState(false);
    const [startupLogsOpen, setStartupLogsOpen] = useState(false);
    const isDesktopApp = useMemo(
        () => new URLSearchParams(window.location.search).get('desktop') === '1',
        [],
    );

    useEffect(() => {
        syncVegaLocale();
        const onLangChanged = () => syncVegaLocale();
        i18n.on('languageChanged', onLangChanged);
        return () => { i18n.off('languageChanged', onLangChanged); };
    }, [i18n]);

    useEffect(() => {
        apiRequest(getUrls().APP_CONFIG)
            .then(({ data }) => {
                dispatch(dfActions.setServerConfig(data));
                setConfigLoaded(true);
            });
    }, []);

    // Validate persisted workspace still exists on the backend
    const activeWorkspace = useSelector((state: AlidadeState) => state.activeWorkspace);
    const tables = useSelector(dfSelectors.getAllTables);
    
    // Debug: log persisted state on startup
    useEffect(() => {
        if (configLoaded) {
            console.log('[DEBUG] activeWorkspace:', activeWorkspace);
            console.log('[DEBUG] tables:', tables.length, tables.map(t => ({ id: t.id, virtual: t.virtual, rowLen: t.rows?.length })));
            
            // Recover orphaned state: content exists but activeWorkspace was lost
            if (!activeWorkspace && !dfSelectors.selectSessionEmpty(store.getState())) {
                const recoveredId = `recovered_${Date.now()}`;
                dispatch(dfActions.setActiveWorkspace({ id: recoveredId, displayName: t('workspace.recoveredSession') }));
            }
        }
    }, [configLoaded]);

    // Unified auth initialisation — driven by /api/auth/info and server IDENTITY
    const [authChecked, setAuthChecked] = useState(false);
    const [migrationBrowserId, setMigrationBrowserId] = useState<string | null>(null);
    const serverConfig = useSelector((state: AlidadeState) => state.serverConfig);

    useEffect(() => {
        if (!configLoaded) return;

        (async () => {
            const prevType = localStorage.getItem('df_identity_type');
            const prevBrowserId = localStorage.getItem('df_browser_id');

            let resolvedIdentity: { type: 'user' | 'browser' | 'local'; id: string; displayName?: string } | null = null;

            // Check if the server assigned a fixed identity (e.g. localhost mode)
            const serverIdentity = serverConfig?.IDENTITY;
            if (serverIdentity?.type === 'local' && serverIdentity?.id) {
                resolvedIdentity = { type: 'local', id: serverIdentity.id };
            }

            if (!resolvedIdentity) {
                try {
                    const { getAuthInfo, getOidcUser } = await import('./oidcConfig');
                    const info: AuthInfo | null = await getAuthInfo();

                    if (info?.action === 'backend') {
                        // Backend OIDC — identity from server session
                        try {
                            const { data: status } = await apiRequest(info.status_url || '/api/auth/oidc/status');
                            if (status.authenticated && status.user) {
                                resolvedIdentity = {
                                    type: 'user',
                                    id: String(status.user.sub || status.user.id || 'session_user'),
                                    displayName: typeof status.user.name === 'string' ? status.user.name : undefined,
                                };
                            }
                        } catch {
                            // fall through to browser identity
                        }
                    } else if (info?.action === 'frontend') {
                        // OIDC PKCE — check for an existing session
                        const user = await getOidcUser();
                        if (user && !user.expired) {
                            resolvedIdentity = {
                                type: 'user',
                                id: String(user.profile.sub),
                                displayName: typeof user.profile.name === 'string' ? user.profile.name : undefined,
                            };
                        }
                    } else if (info?.action === 'transparent') {
                        // Azure App Service EasyAuth — headers injected by Azure
                        try {
                            const resp = await fetch('/.auth/me');
                            const result = await resp.json();
                            if (Array.isArray(result) && result.length > 0) {
                                const authData = result[0];
                                const name = authData['user_claims']?.find((item: any) => item.typ === 'name')?.val || '';
                                const userId = authData['user_id'];
                                if (userId) {
                                    resolvedIdentity = { type: 'user', id: userId, displayName: name };
                                }
                            }
                        } catch {
                            // fall through to browser identity
                        }
                    }
                    // 'redirect' and 'none' → browser identity (resolvedIdentity stays null)
                } catch {
                    // fall through to browser identity
                }
            }

            if (!resolvedIdentity) {
                resolvedIdentity = { type: 'browser', id: getBrowserId() };
            }

            dispatch(dfActions.setIdentity(resolvedIdentity));

            try {
                const { data: refreshedConfig } = await apiRequest(getUrls().APP_CONFIG);
                dispatch(dfActions.setServerConfig(refreshedConfig));
            } catch {
                // App config was already loaded; connector status refresh is best-effort.
            }

            // Persist current identity type for next page load
            localStorage.setItem('df_identity_type', resolvedIdentity.type);
            if (resolvedIdentity.type === 'browser') {
                localStorage.setItem('df_browser_id', resolvedIdentity.id);
            }

            // Detect anonymous → authenticated transition
            if (
                prevType === 'browser' &&
                resolvedIdentity.type === 'user' &&
                prevBrowserId
            ) {
                setMigrationBrowserId(prevBrowserId);
            }

            setAuthChecked(true);
        })();
    }, [configLoaded]);

    useEffect(() => {
        document.title = toolName;
        // Load all server-configured models instantly (no connectivity check).
        // Users can verify connectivity via the "Test" button in the model dialog,
        // or errors will surface naturally when a model is first used.
        dispatch(fetchGlobalModelList());
    }, []);

    let theme = createTheme({
        typography: {
            fontFamily: [
                "Arial",
                "Roboto",
                "Helvetica Neue",
                "sans-serif"
            ].join(",")
        },
        // Default Material UI palette
        // Active palette from user config — selectable via Settings dialog
        // Available: material, fluent, vivid, jewel, electric, tealCoral, copilot
        palette: (() => {
            const p = palettes[activePaletteKey];
            const bg = (entry: { main: string; bgcolor?: string }) => entry.bgcolor ?? alpha(entry.main, bgAlpha);
            const tc = (entry: { main: string; textColor?: string }) => entry.textColor ?? entry.main;
            return {
                primary:   { main: p.primary.main,   bgcolor: bg(p.primary),   textColor: tc(p.primary)   },
                secondary: { main: p.secondary.main, bgcolor: bg(p.secondary), textColor: tc(p.secondary) },
                derived:   { main: p.derived.main,   bgcolor: bg(p.derived),   textColor: tc(p.derived)   },
                custom:    { main: p.custom.main,    bgcolor: bg(p.custom),    textColor: tc(p.custom)    },
                warning:   { main: p.warning.main },
            };
        })(),
        components: {
            MuiButton: {
                defaultProps: {
                    disableElevation: true,
                },
                styleOverrides: {
                    root: {
                        textTransform: 'none',
                        borderRadius: 4,
                        fontWeight: 500,
                        lineHeight: 1.4,
                        minWidth: 0,
                        whiteSpace: 'nowrap',
                        '& .MuiButton-startIcon': {
                            marginLeft: 0,
                            marginRight: buttonVar.iconGap,
                        },
                        '& .MuiButton-endIcon': {
                            marginLeft: buttonVar.iconGap,
                            marginRight: 0,
                        },
                    },
                    sizeSmall: {
                        minHeight: buttonVar.heightSmall,
                        padding: `0 ${buttonVar.paddingSmall}`,
                        fontSize: textVar.sm,
                        '& .MuiButton-icon > :nth-of-type(1)': {
                            fontSize: iconVar.sm,
                        },
                    },
                    sizeMedium: {
                        minHeight: buttonVar.heightMedium,
                        padding: `0 ${buttonVar.paddingMedium}`,
                        fontSize: textVar.md,
                        '& .MuiButton-icon > :nth-of-type(1)': {
                            fontSize: iconVar.md,
                        },
                    },
                    text: ({ ownerState, theme: t }) => {
                        const c = ownerState.color;
                        if (c && c !== 'inherit' && c !== 'error' && c !== 'info' && c !== 'success' && c in t.palette) {
                            const p = (t.palette as any)[c];
                            if (p?.textColor) return { color: p.textColor };
                        }
                        return {};
                    },
                    outlined: ({ ownerState, theme: t }) => {
                        const c = ownerState.color;
                        if (c && c !== 'inherit' && c !== 'error' && c !== 'info' && c !== 'success' && c in t.palette) {
                            const p = (t.palette as any)[c];
                            if (p?.textColor) return { color: p.textColor, borderColor: alpha(p.textColor, 0.5) };
                        }
                        return {};
                    },
                },
                variants: [
                    {
                        props: { variant: 'soft' },
                        style: ({ theme: t }) => ({
                            color: (t.palette.primary as any).textColor ?? t.palette.primary.main,
                            backgroundColor: (t.palette.primary as any).bgcolor ?? alpha(t.palette.primary.main, 0.1),
                            '&:hover': {
                                backgroundColor: alpha(t.palette.primary.main, 0.16),
                            },
                        }),
                    },
                    {
                        props: { variant: 'toolbar' },
                        style: ({ theme: t }) => ({
                            color: t.palette.text.secondary,
                            backgroundColor: 'transparent',
                            '&:hover': {
                                color: t.palette.text.primary,
                                backgroundColor: t.palette.action.hover,
                            },
                        }),
                    },
                ],
            },
            MuiIconButton: {
                styleOverrides: {
                    root: ({ ownerState, theme: t }) => {
                        const c = ownerState.color;
                        if (c && c !== 'inherit' && c !== 'default' && c !== 'error' && c !== 'info' && c !== 'success' && c in t.palette) {
                            const p = (t.palette as any)[c];
                            if (p?.textColor) return { color: p.textColor };
                        }
                        return {};
                    },
                },
            },
            MuiLink: {
                styleOverrides: {
                    root: ({ ownerState, theme: t }) => {
                        const c = ownerState.color as string | undefined;
                        if (c && c !== 'inherit' && c in t.palette) {
                            const p = (t.palette as any)[c];
                            if (p?.textColor) return { color: p.textColor };
                        }
                        return {};
                    },
                },
            },
        },
        transitions: {
            duration: {
                shortest: 100,
                shorter: 100,
                short: 100,
                standard: 100,
                complex: 150,
                enteringScreen: 100,
                leavingScreen: 100,
            },
        },
    });

    const router = useMemo(() => createBrowserRouter([
        {
            path: "/auth/callback",
            element: <OidcCallback />,
        },
        {
            path: "/",
            element: <AppShell />,
            errorElement: <ErrorBoundaryFallback />,
            children: [
                {
                    index: true,
                    element: <AlidadeFC />,
                },
                {
                    path: "app",
                    element: <AlidadeFC />,
                },
                {
                    path: "about",
                    element: <About />,
                },
                {
                    path: "*",
                    element: <AlidadeFC />,
                },
            ],
        }
    ]), []);

    return (
        <ThemeProvider theme={theme}>
            <LayoutProvider>
                {configLoaded && authChecked ? (
                    <RouterProvider router={router} />
                ) : (
                    <>
                        <AnvilLoader
                            label="loading Alidade..."
                            action={isDesktopApp ? (
                                <Link
                                    component="button"
                                    type="button"
                                    underline="always"
                                    onClick={() => setStartupLogsOpen(true)}
                                    sx={{
                                        color: 'text.disabled',
                                        fontSize: '0.7rem',
                                        fontWeight: 400,
                                        fontFamily: 'inherit',
                                        '&:hover': { color: 'text.secondary' },
                                    }}
                                >
                                    View backend log
                                </Link>
                            ) : undefined}
                        />
                        {isDesktopApp && (
                            <LogViewerDialog
                                open={startupLogsOpen}
                                onOpenChange={setStartupLogsOpen}
                                hideTrigger
                            />
                        )}
                    </>
                )}
                {migrationBrowserId && (
                    <IdentityMigrationDialog
                        oldBrowserId={migrationBrowserId}
                        onDone={() => setMigrationBrowserId(null)}
                    />
                )}
            </LayoutProvider>
        </ThemeProvider>
    );
}

function stringAvatar(name: string) {
    let displayName = ""
    try {
        let nameSplit = name.split(' ')
        displayName = `${nameSplit[0][0]}${nameSplit.length > 1 ? nameSplit[nameSplit.length - 1][0] : ''}`
    } catch {
        displayName = name ? name[0] : "?";
    }
    return {
        sx: {
            bgcolor: "cornflowerblue",
            width: 36,
            height: 36,
            margin: "auto",
            fontSize: "1rem"
        },
        children: displayName,
    };
}
