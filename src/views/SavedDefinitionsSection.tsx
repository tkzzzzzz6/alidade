// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * SavedDefinitionsSection — the knowledge panel's list of saved definitions.
 *
 * When the user answers a clarifying question ("by 'best' I mean highest
 * margin"), the agent saves what the word means; later questions that use the
 * same word on the same tables are answered with it instead of asking again.
 * Here the user sees what was saved, how often it was used, and can delete it.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Box, CircularProgress, IconButton, Tooltip, Typography } from '@mui/material';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import RefreshIcon from '@mui/icons-material/Refresh';

import { deleteDefinition, listDefinitions, type SavedDefinition } from '../api/knowledgeApi';
import { textVar } from '../app/layout';

export const SavedDefinitionsSection: React.FC = () => {
    const { t } = useTranslation();
    const [items, setItems] = useState<SavedDefinition[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            setItems(await listDefinitions());
        } catch (e: any) {
            setError(e?.message || String(e));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    const remove = async (id: string) => {
        try {
            await deleteDefinition(id);
            setItems(prev => prev.filter(d => d.id !== id));
        } catch (e: any) {
            setError(e?.message || String(e));
        }
    };

    return (
        <Box data-testid="saved-definitions" sx={{ pb: 1, borderBottom: '1px solid rgba(0, 0, 0, 0.07)' }}>
            <Box sx={{ display: 'flex', alignItems: 'center', px: 1.5, pt: 1.25, pb: 0.75, backgroundColor: 'rgba(255, 255, 255, 0.46)' }}>
                <Typography sx={{ flex: 1, fontSize: textVar.xs, fontWeight: 700, color: 'rgba(0, 0, 0, 0.72)', letterSpacing: 0.6, textTransform: 'uppercase' }}>
                    {t('knowledge.definitions')}
                </Typography>
                <Tooltip title={t('knowledge.definitionsRefresh')}>
                    <span>
                        <IconButton size="small" onClick={load} disabled={loading} aria-label={t('knowledge.definitionsRefresh')}>
                            <RefreshIcon sx={{ fontSize: 16 }} />
                        </IconButton>
                    </span>
                </Tooltip>
            </Box>
            <Box sx={{ mx: 1.5, mb: 0.75, py: 0.25 }}>
                <Typography sx={{ fontSize: textVar.xs, color: 'text.secondary', lineHeight: 1.55 }}>
                    {t('knowledge.definitionsHint')}
                </Typography>
            </Box>
            {loading && items.length === 0 && (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 1 }}><CircularProgress size={16} /></Box>
            )}
            {error && <Typography sx={{ mx: 1.5, fontSize: textVar.xs, color: 'error.main' }}>{error}</Typography>}
            {!loading && !error && items.length === 0 && (
                <Typography sx={{ fontSize: textVar.xs, color: 'text.disabled', px: 1.5, py: 0.5, fontStyle: 'italic' }}>
                    {t('knowledge.definitionsEmpty')}
                </Typography>
            )}
            {items.map(d => (
                <Box key={d.id} data-testid="saved-definition"
                     sx={{ display: 'flex', alignItems: 'flex-start', gap: 0.5, mx: 1.5, my: 0.5, px: 1, py: 0.5,
                           borderRadius: 1, '&:hover': { bgcolor: 'action.hover' },
                           '&:hover .def-delete': { opacity: 1 } }}>
                    <Box sx={{ flex: 1, minWidth: 0 }}>
                        <Typography sx={{ fontSize: textVar.sm, wordBreak: 'break-word' }}>
                            <b>{d.term}</b>{' = '}{d.meaning}
                        </Typography>
                        <Typography sx={{ fontSize: textVar.xxs, color: 'text.secondary' }}>
                            {[
                                d.tables?.length ? t('knowledge.definitionTables', { tables: d.tables.join(', ') }) : t('knowledge.definitionAllTables'),
                                d.aliases?.length ? t('knowledge.definitionAliases', { aliases: d.aliases.join(', ') }) : null,
                                t('knowledge.definitionUses', { count: d.uses ?? 0 }),
                            ].filter(Boolean).join(' · ')}
                        </Typography>
                    </Box>
                    <Tooltip title={t('knowledge.definitionDelete')}>
                        <IconButton className="def-delete" size="small" onClick={() => remove(d.id)}
                                    aria-label={t('knowledge.definitionDelete')} sx={{ opacity: 0.5, p: 0.25 }}>
                            <DeleteOutlineIcon sx={{ fontSize: 16 }} />
                        </IconButton>
                    </Tooltip>
                </Box>
            ))}
        </Box>
    );
};

export default SavedDefinitionsSection;
