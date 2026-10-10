// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { Box, Typography, Button, Link, useTheme, alpha } from "@mui/material";
import React, { FC } from "react";
import GridViewIcon from '@mui/icons-material/GridView';
import CheckCircleOutlineRoundedIcon from '@mui/icons-material/CheckCircleOutlineRounded';

import { toolName } from "../app/App";
import { textVar } from '../app/layout';
import alidadeLogo from '../assets/alidade-logo.svg';
import { useTranslation } from 'react-i18next';

const HIGHLIGHTS = ['highlight1', 'highlight2', 'highlight3', 'highlight4', 'highlight5'];

export const About: FC<{}> = function About({ }) {
    const theme = useTheme();
    const { t } = useTranslation();

    return (
        <Box
            component="main"
            role="main"
            sx={{
                display: "flex",
                flexDirection: "column",
                overflowY: "auto",
                width: '100%',
                height: '100%',
                background: `
                    linear-gradient(90deg, ${alpha(theme.palette.text.secondary, 0.01)} 1px, transparent 1px),
                    linear-gradient(0deg, ${alpha(theme.palette.text.secondary, 0.01)} 1px, transparent 1px)
                `,
                backgroundSize: '16px 16px',
            }}
        >
            <Box sx={{ margin: 'auto', py: 4, display: "flex", flexDirection: "column", textAlign: "center", maxWidth: 760, width: '100%', px: 3 }}>
                <Box component="header" sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', mt: 2 }}>
                    <Box component="img" src={alidadeLogo} alt="" aria-hidden="true" sx={{ width: 88, height: 88, mb: 1 }} />
                    <Typography component="h1" fontSize={72} sx={{ letterSpacing: '0.04em', lineHeight: 1.1 }}>{toolName}</Typography>
                    <Typography component="p" sx={{ fontSize: 22, color: 'text.secondary', mt: 1 }}>
                        {t('landing.tagline')}
                    </Typography>
                    <Typography component="p" sx={{ fontSize: textVar.sm, color: 'text.secondary', mt: 1.5, maxWidth: 560 }}>
                        {t('about.nameMeaning')}
                    </Typography>
                </Box>

                <Box component="nav" aria-label="Primary actions" sx={{ display: 'flex', justifyContent: 'center', mt: 4, mb: 5 }}>
                    <Button size="large" variant="contained" color="primary"
                        sx={{ textTransform: 'none' }}
                        startIcon={<GridViewIcon aria-hidden="true" />}
                        href="/app"
                    >{t('about.openApp')}</Button>
                </Box>

                <Box component="section" aria-labelledby="about-highlights" sx={{ textAlign: 'left', mx: 'auto', maxWidth: 600 }}>
                    <Typography id="about-highlights" component="h2" sx={{ fontSize: textVar.lg, fontWeight: 600, mb: 1.5 }}>
                        {t('about.highlightsTitle')}
                    </Typography>
                    <Box component="ul" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: 1.25 }}>
                        {HIGHLIGHTS.map(key => (
                            <Box component="li" key={key} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
                                <CheckCircleOutlineRoundedIcon sx={{ fontSize: 18, color: 'primary.main', mt: '2px', flexShrink: 0 }} aria-hidden="true" />
                                <Typography sx={{ fontSize: textVar.sm, color: 'text.primary', lineHeight: 1.6 }}>{t(`about.${key}`)}</Typography>
                            </Box>
                        ))}
                    </Box>
                </Box>

                <Box sx={{ mt: 6, mx: 2 }}>
                    <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                        <strong>{t('about.dataHandling')}</strong> {t('about.dataHandlingText')}
                    </Typography>
                </Box>
            </Box>

            <Box
                component="footer"
                role="contentinfo"
                sx={{ color: 'text.secondary', textAlign: 'center', py: 1, px: 2,
                    backgroundColor: 'rgba(255, 255, 255, 0.89)' }}
            >
                <Typography sx={{ fontSize: textVar.xs }}>
                    {t('about.attribution')}{' '}
                    <Link href="https://github.com/microsoft/data-formulator" target="_blank" rel="noopener noreferrer"
                        underline="hover" color="inherit">github.com/microsoft/data-formulator</Link>
                </Typography>
            </Box>
        </Box>)
}
