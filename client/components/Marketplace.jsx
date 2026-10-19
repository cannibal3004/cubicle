'use client';

import React, { useEffect, useState } from 'react';
import {
  FiAlertCircle,
  FiCheck,
  FiExternalLink,
  FiRefreshCw,
  FiSearch,
  FiSettings,
  FiZap,
} from 'react-icons/fi';
import {
  authorizeConnector,
  disconnectConnector,
  fetchConnectionStatus,
  fetchConnectorCatalog,
  fetchSettings,
  saveSettings,
} from '../lib/api';

// A stable fallback keeps the marketplace useful when the API or connector key
// is not configured yet.
const CURATED_APPS = [
  { slug: 'github', label: 'GitHub', blurb: 'Issues, pull requests, and code', domain: 'github.com' },
  { slug: 'slack', label: 'Slack', blurb: 'Post updates and read channels', domain: 'slack.com' },
  { slug: 'gmail', label: 'Gmail', blurb: 'Read and send email', domain: 'gmail.com' },
  { slug: 'googlecalendar', label: 'Google Calendar', blurb: 'Read and create calendar events', domain: 'calendar.google.com' },
  { slug: 'googlesheets', label: 'Google Sheets', blurb: 'Read and update spreadsheets', domain: 'sheets.google.com' },
  { slug: 'googledocs', label: 'Google Docs', blurb: 'Read and write documents', domain: 'docs.google.com' },
  { slug: 'googledrive', label: 'Google Drive', blurb: 'Browse and manage files', domain: 'drive.google.com' },
  { slug: 'notion', label: 'Notion', blurb: 'Pages and databases', domain: 'notion.so' },
  { slug: 'linear', label: 'Linear', blurb: 'Issues and project tracking', domain: 'linear.app' },
  { slug: 'discord', label: 'Discord', blurb: 'Messages and channels', domain: 'discord.com' },
  { slug: 'x', label: 'X (Twitter)', blurb: 'Post and read on X', domain: 'x.com' },
  { slug: 'hubspot', label: 'HubSpot', blurb: 'CRM search and updates', domain: 'hubspot.com' },
  { slug: 'salesforce', label: 'Salesforce', blurb: 'CRM records and reports', domain: 'salesforce.com' },
  { slug: 'jira', label: 'Jira', blurb: 'Issues and sprints', domain: 'atlassian.com' },
  { slug: 'asana', label: 'Asana', blurb: 'Tasks and projects', domain: 'asana.com' },
  { slug: 'trello', label: 'Trello', blurb: 'Boards and cards', domain: 'trello.com' },
  { slug: 'dropbox', label: 'Dropbox', blurb: 'Files and folders', domain: 'dropbox.com' },
  { slug: 'airtable', label: 'Airtable', blurb: 'Bases and records', domain: 'airtable.com' },
  { slug: 'figma', label: 'Figma', blurb: 'Files and comments', domain: 'figma.com' },
  { slug: 'stripe', label: 'Stripe', blurb: 'Payments and customers', domain: 'stripe.com' },
  { slug: 'zapier', label: 'Zapier', blurb: 'Connect apps through automation', domain: 'zapier.com' },
  { slug: 'reddit', label: 'Reddit', blurb: 'Browse and post on Reddit', domain: 'reddit.com' },
  { slug: 'sentry', label: 'Sentry', blurb: 'Errors, alerts, and performance', domain: 'sentry.io' },
  { slug: 'posthog', label: 'PostHog', blurb: 'Analytics and feature flags', domain: 'posthog.com' },
];

const LS_KEY = 'cubicle_connected_plugins';

function loadLocalEnabled() {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || '[]');
  } catch {
    return [];
  }
}

function saveLocalEnabled(slugs) {
  localStorage.setItem(LS_KEY, JSON.stringify(slugs));
}

function normalizeCard(app) {
  return {
    slug: app.slug || app.key || app.name,
    label: app.label || app.name || app.slug,
    blurb: app.blurb || app.description || 'Connector integration',
    domain: app.domain || '',
    logo: app.logo || null,
    // From the live catalog. Composio lends its own OAuth app for "managed"
    // toolkits; the rest need an auth config with your credentials first.
    managedAuth: typeof app.managed_auth === 'boolean' ? app.managed_auth : null,
    authSchemes: Array.isArray(app.auth_schemes) ? app.auth_schemes : [],
    hasAuthConfig: Boolean(app.has_auth_config),
    needsSetup: Boolean(app.needs_setup),
  };
}

const COMPOSIO_DASHBOARD_URL = 'https://platform.composio.dev/';

const SCHEME_LABELS = {
  OAUTH2: 'an OAuth app (client ID and secret)',
  OAUTH1: 'an OAuth 1.0 app',
  API_KEY: 'an API key',
  BEARER_TOKEN: 'a bearer token',
  BASIC: 'a username and password',
  BASIC_WITH_JWT: 'a username and password',
  SERVICE_ACCOUNT: 'a service account',
  GOOGLE_SERVICE_ACCOUNT: 'a Google service account',
  NO_AUTH: 'no credentials',
};

function describeSchemes(app) {
  const labels = (app.authSchemes || []).map((scheme) => SCHEME_LABELS[scheme] || scheme.toLowerCase());
  if (!labels.length) return 'your own credentials';
  return labels.join(' or ');
}

function setupMessage(app) {
  return `${app.label} has no Composio-managed credentials. In the Composio dashboard, open Auth Configs and create one for ${app.label} with ${describeSchemes(app)}, then refresh this page and connect.`;
}

function AppIcon({ app }) {
  const [failed, setFailed] = useState(false);
  const source = app.logo || (app.domain
    ? `https://www.google.com/s2/favicons?domain=${app.domain}&sz=64`
    : null);

  if (source && !failed) {
    return (
      <img
        src={source}
        alt=""
        className="w-8 h-8 rounded-lg object-contain flex-shrink-0"
        onError={() => setFailed(true)}
      />
    );
  }

  return (
    <div className="w-8 h-8 rounded-lg bg-[#27272a] flex items-center justify-center text-xs font-bold text-zinc-300 border border-[#333338] flex-shrink-0">
      {(app.label || '?').charAt(0).toUpperCase()}
    </div>
  );
}

export default function Marketplace({ onOpenSettings }) {
  const [apps, setApps] = useState(CURATED_APPS);
  const [connected, setConnected] = useState([]);
  const [search, setSearch] = useState('');
  const [configured, setConfigured] = useState(false);
  const [source, setSource] = useState('curated');
  const [loading, setLoading] = useState(true);
  const [busySlug, setBusySlug] = useState(null);
  const [notice, setNotice] = useState('');
  const [noticeLink, setNoticeLink] = useState('');
  const [error, setError] = useState('');
  const [refreshToken, setRefreshToken] = useState(0);
  // The app whose sign-in is open in another tab; the page polls until it connects.
  const [awaiting, setAwaiting] = useState(null);
  // Connected apps whose tools are withheld from every bot.
  const [disabledToolkits, setDisabledToolkits] = useState([]);

  useEffect(() => {
    let mounted = true;

    async function loadMarketplace() {
      setLoading(true);
      setError('');

      // A manual refresh bypasses the server's catalog cache so an auth
      // config just created in the Composio dashboard shows up at once.
      const catalog = await fetchConnectorCatalog(refreshToken > 0);
      const nextApps = (catalog.cards || []).length
        ? catalog.cards.map(normalizeCard)
        : CURATED_APPS;

      if (!mounted) return;
      setApps(nextApps);
      setConfigured(Boolean(catalog.configured));
      setSource(catalog.source || 'curated');

      if (catalog.configured) {
        const settings = await fetchSettings();
        if (!mounted) return;
        setDisabledToolkits(Array.isArray(settings?.disabled_toolkits) ? settings.disabled_toolkits : []);
      }

      if (catalog.configured && nextApps.length) {
        // A connection always sits on an auth config, so only those apps
        // can be connected; asking about all ~1,600 would not fit in a URL.
        const candidates = nextApps.filter((app) => app.hasAuthConfig || app.managedAuth === null);
        const status = await fetchConnectionStatus(candidates.map((app) => app.slug));
        if (!mounted) return;
        setConnected(
          Object.entries(status.services || {})
            .filter(([, value]) => value && value.connected)
            .map(([slug]) => slug),
        );
        if (status.error) setError(status.error);
      } else {
        setConnected(loadLocalEnabled());
      }

      if (mounted) setLoading(false);
    }

    loadMarketplace().catch((err) => {
      if (!mounted) return;
      setError(err.message || 'Could not load connector catalog');
      setLoading(false);
    });

    return () => {
      mounted = false;
    };
  }, [refreshToken]);

  const toggle = async (app) => {
    if (busySlug) return;
    setNotice('');
    setNoticeLink('');
    setError('');

    const isOn = connected.includes(app.slug);
    if (!configured) {
      const next = isOn
        ? connected.filter((slug) => slug !== app.slug)
        : [...connected, app.slug];
      setConnected(next);
      saveLocalEnabled(next);
      setNotice('Local preference saved. Add a Composio key to authorize a real account.');
      return;
    }

    if (!isOn && app.needsSetup) {
      setNotice(setupMessage(app));
      setNoticeLink(COMPOSIO_DASHBOARD_URL);
      return;
    }

    setBusySlug(app.slug);
    let authWindow = null;
    try {
      if (!isOn && typeof window !== 'undefined') {
        authWindow = window.open('', '_blank');
      }

      if (isOn) {
        await disconnectConnector(app.slug);
        setConnected((current) => current.filter((slug) => slug !== app.slug));
        setNotice(`${app.label} disconnected.`);
      } else {
        const result = await authorizeConnector(app.slug);
        if (result.connected && !result.url) {
          // Needs no sign-in: it is connected already.
          if (authWindow && !authWindow.closed) authWindow.close();
          setConnected((current) => (current.includes(app.slug) ? current : [...current, app.slug]));
          setNotice(`${app.label} connected. It needs no sign-in; its tools are available now.`);
          return;
        }
        if (!result.url) throw new Error('The connector did not return an authorization link');
        if (authWindow) {
          authWindow.location.href = result.url;
        } else if (typeof window !== 'undefined') {
          window.open(result.url, '_blank', 'noopener,noreferrer');
        }
        setNotice(`Authorization opened for ${app.label} in a new tab. This page updates when you have signed in.`);
        setAwaiting({ slug: app.slug, label: app.label, since: Date.now() });
      }
    } catch (err) {
      if (authWindow && !authWindow.closed) authWindow.close();
      setError(err.message || `Could not update ${app.label}`);
    } finally {
      setBusySlug(null);
    }
  };

  // After a sign-in opens, ask the server every few seconds whether the app is
  // connected yet, for up to ten minutes; the first yes refreshes the page.
  useEffect(() => {
    if (!awaiting) return undefined;
    let disposed = false;
    const check = async () => {
      if (Date.now() - awaiting.since > 10 * 60 * 1000) {
        setAwaiting(null);
        return;
      }
      try {
        const status = await fetchConnectionStatus([awaiting.slug]);
        if (disposed) return;
        if (status?.services?.[awaiting.slug]?.connected) {
          setAwaiting(null);
          setConnected((current) => (current.includes(awaiting.slug) ? current : [...current, awaiting.slug]));
          setNotice(`${awaiting.label} connected. Its tools are offered to your bots from their next turn.`);
          setNoticeLink(null);
          setRefreshToken((value) => value + 1);
        }
      } catch (err) {
        // A failed check is retried on the next tick.
      }
    };
    const interval = window.setInterval(check, 3000);
    return () => {
      disposed = true;
      window.clearInterval(interval);
    };
  }, [awaiting]);

  const setToolsOffered = async (app, offered) => {
    const next = offered ? disabledToolkits.filter((slug) => slug !== app.slug) : [...new Set([...disabledToolkits, app.slug])];
    setDisabledToolkits(next);
    try {
      await saveSettings({ disabled_toolkits: next });
    } catch (err) {
      setDisabledToolkits(disabledToolkits);
      setError(err.message || 'Could not update tool settings');
    }
  };

  // Without a search, show the apps you can use (connected or set up) and
  // the most popular ones; the whole catalog is reachable through search.
  const POPULAR_LIMIT = 60;
  const SEARCH_LIMIT = 200;
  const query = search.trim().toLowerCase();
  let visible;
  let hiddenCount = 0;
  if (query) {
    const matches = apps.filter((app) => `${app.label} ${app.slug} ${app.blurb}`.toLowerCase().includes(query));
    visible = matches.slice(0, SEARCH_LIMIT);
    hiddenCount = matches.length - visible.length;
  } else {
    const mine = apps.filter((app) => connected.includes(app.slug) || app.hasAuthConfig);
    const rest = apps.filter((app) => !mine.includes(app)).slice(0, POPULAR_LIMIT);
    visible = [...mine, ...rest];
    hiddenCount = apps.length - visible.length;
  }

  return (
    <div className="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b] select-none font-sans text-zinc-100">
      <div className="px-6 py-4 border-b border-[#18181c] flex items-center justify-between gap-4 flex-shrink-0">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-bold text-zinc-100 tracking-wide">Connected Apps</h2>
            <span className="text-[10px] bg-zinc-800 text-zinc-400 border border-zinc-700 px-1.5 py-0.5 rounded-full font-semibold">
              {connected.length} connected
            </span>
          </div>
          <p className="text-[11px] text-zinc-500 mt-0.5">
            {source === 'api' ? 'Live connector catalog' : 'Curated connector catalog'}
            {configured ? ' · account authorization available' : ' · local preview mode'}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setRefreshToken((value) => value + 1)}
            className="p-2 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1e1e22] transition"
            title="Refresh connector status"
          >
            <FiRefreshCw className={loading ? 'animate-spin' : ''} />
          </button>
          <div className="relative w-56">
            <FiSearch className="absolute left-3 top-2.5 text-zinc-500 text-xs" />
            <input
              suppressHydrationWarning={true}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search apps…"
              className="w-full bg-[#18181b] border border-[#27272a] rounded-xl pl-8 pr-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition"
            />
          </div>
        </div>
      </div>

      <div className="mx-6 mt-4 flex-shrink-0">
        <div className="rounded-xl border border-blue-500/20 bg-blue-500/[0.07] px-4 py-2.5 text-[11px] text-blue-300 flex items-start gap-2">
          <FiZap className="flex-shrink-0 text-blue-400 mt-0.5" />
          <span>
            {configured
              ? 'Connect an app to open its provider authorization flow. No account is connected until you complete that flow.'
              : 'This is a local catalog preview. Add a Composio API key in App Settings to discover live connectors and authorize accounts.'}
          </span>
          {!configured && onOpenSettings && (
            <button
              type="button"
              onClick={onOpenSettings}
              className="ml-auto flex items-center gap-1 text-blue-200 hover:text-white font-semibold flex-shrink-0"
            >
              <FiSettings /> Settings
            </button>
          )}
        </div>
      </div>

      {(notice || error) && (
        <div className={`mx-6 mt-3 flex-shrink-0 rounded-xl border px-4 py-2.5 text-[11px] flex items-start gap-2 ${
          error
            ? 'border-rose-500/20 bg-rose-500/[0.07] text-rose-300'
            : 'border-emerald-500/20 bg-emerald-500/[0.07] text-emerald-300'
        }`}>
          {error ? <FiAlertCircle className="mt-0.5 flex-shrink-0" /> : <FiCheck className="mt-0.5 flex-shrink-0" />}
          <span>
            {error || notice}
            {!error && noticeLink && (
              <>
                {' '}
                <a
                  href={noticeLink}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline underline-offset-2 hover:text-white"
                >
                  Open the Composio dashboard
                </a>
              </>
            )}
          </span>
        </div>
      )}

      <div
        className="flex-1 overflow-y-auto mx-6 mt-3 mb-6 rounded-2xl border border-[#1e1e22]"
        style={{ scrollbarWidth: 'thin', scrollbarColor: '#27272a transparent' }}
      >
        {loading && apps.length === 0 ? (
          <div className="py-16 text-center text-sm text-zinc-600">Loading connectors…</div>
        ) : visible.length === 0 ? (
          <div className="py-16 text-center text-sm text-zinc-600">No apps match.</div>
        ) : (
          [...visible.map((app, index) => {
            const isOn = connected.includes(app.slug);
            const isBusy = busySlug === app.slug;
            const needsSetup = configured && !isOn && app.needsSetup;
            return (
              <div
                key={app.slug}
                className={`flex items-center gap-3.5 px-4 py-3.5 transition-colors hover:bg-[#111115] ${
                  index > 0 ? 'border-t border-[#1a1a1e]' : ''
                } ${isOn ? 'bg-[#0d1210]' : 'bg-[#09090b]'}`}
              >
                <AppIcon app={app} />

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 text-xs font-semibold text-zinc-100">
                    {app.label}
                    {isOn && <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 block flex-shrink-0" />}
                  </div>
                  <div className="text-[11px] text-zinc-500 truncate mt-0.5">{app.blurb}</div>
                  {isOn && configured && (
                    <label className="flex items-center gap-1.5 mt-1 text-[10px] text-zinc-400 cursor-pointer select-none w-max" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={!disabledToolkits.includes(app.slug)}
                        onChange={(e) => setToolsOffered(app, e.target.checked)}
                        className="accent-emerald-500"
                      />
                      {disabledToolkits.includes(app.slug) ? 'Tools off for all bots (still connected)' : 'Tools offered to bots'}
                    </label>
                  )}
                  {needsSetup && (
                    <div className="text-[10px] text-amber-400/80 truncate mt-0.5">
                      Bring your own credentials: {describeSchemes(app)}
                    </div>
                  )}
                </div>

                <button
                  suppressHydrationWarning={true}
                  type="button"
                  disabled={Boolean(busySlug)}
                  onClick={() => toggle(app)}
                  title={needsSetup ? 'Create an auth config for this app in the Composio dashboard first' : undefined}
                  className={`w-28 flex-shrink-0 py-1.5 rounded-xl text-[11px] font-semibold transition flex items-center justify-center gap-1.5 disabled:opacity-50 ${
                    isOn
                      ? 'bg-emerald-600/20 text-emerald-400 border border-emerald-600/40 hover:bg-rose-500/15 hover:text-rose-400 hover:border-rose-500/30'
                      : needsSetup
                        ? 'bg-amber-500/10 text-amber-300 border border-amber-500/30 hover:bg-amber-500/20'
                        : 'bg-[#1e1e22] text-zinc-400 border border-[#2a2a30] hover:text-white hover:bg-[#27272a]'
                  }`}
                >
                  {isBusy ? (
                    <FiRefreshCw className="animate-spin" />
                  ) : isOn ? (
                    <>
                      <FiCheck className="text-xs" />
                      {configured ? 'Connected' : 'Enabled'}
                    </>
                  ) : needsSetup ? (
                    <>
                      <FiSettings className="text-xs" />
                      Needs setup
                    </>
                  ) : (
                    <>
                      {configured && <FiExternalLink className="text-xs" />}
                      {configured ? 'Connect' : 'Enable'}
                    </>
                  )}
                </button>
              </div>
            );
          }),
          hiddenCount > 0 && (
            <div key="__more" className="px-4 py-3 border-t border-[#1a1a1e] text-[11px] text-zinc-500 text-center">
              {query
                ? `Showing the first ${visible.length} matches of ${visible.length + hiddenCount}. Narrow the search to find the rest.`
                : `Showing ${visible.length} of ${apps.length} apps. Search to find any other app, including MCP servers.`}
            </div>
          )]
        )}
      </div>
    </div>
  );
}
