'use strict';

// Keep the native resolver on the recorded workspace path. The collector's
// filesystem has usage snapshots and state, so it cannot establish a repository
// identity for a relative path or for one of its own private mount points.
const projectRoot = require('/opt/token-monitor/src/shared/projectRoot');
const resolveProjectRoot = projectRoot.resolveProjectRoot;
const collectorRoots = ['/snapshot', '/work', '/state', '/relay', '/opt/token-monitor', '/opt/tokscale-pricing'];
function isWorkspacePath(value) {
  const normalized = value.replace(/\\/g, '/').replace(/\/{2,}/g, '/');
  if (!/^(?:[a-z]:)?\/.+/i.test(normalized) || /[\x00-\x1f\x7f]/.test(normalized)) return false;
  if (normalized.split('/').some(part => part === '.' || part === '..')) return false;
  return !collectorRoots.some(root => normalized === root || normalized.startsWith(root + '/'));
}
projectRoot.resolveProjectRoot = (dir, key) => {
  if (!isWorkspacePath(dir)) return '';
  const root = resolveProjectRoot(dir, key);
  return isWorkspacePath(root) ? root : '';
};

// Loaded before the official headless agent. Select the newest known session
// observation and leave period projection and persistence to Token Monitor.
// Upstream hashes an empty resolved root and can still attach a fallback
// label. Drop that sentinel before the archive or wire payload sees it.
const { projectIdentity } = require('/opt/token-monitor/src/shared/sessionMetadata');
const unattributedProjectId = projectIdentity('.').projectId;
const storeModule = require('/opt/token-monitor/src/shared/usage/sessionUsageArchiveStore');
const { applySessionDayArchive, applySessionUsageArchive } = require('/opt/token-monitor/src/shared/usage/sessionUsageArchive');
const { PERIODS, applyPeriodDelta, emptyPeriod, normalizeClientName } = require('/opt/token-monitor/src/shared/usage');
const dailyArchive = require('/opt/token-monitor/src/shared/dailyHistoryArchive');
const { normalizeHistory, parseGraphResult } = require('/opt/token-monitor/src/shared/history');

const retainHistory = dailyArchive.retainDailyHistory;
let historyScan;
dailyArchive.retainDailyHistory = (graphs, options) => {
  // The upstream graph contains day/provider totals, before session versions
  // can be compared. Defer committing it until the usage scan has selected them.
  historyScan = { graphs, options };
  return retainHistory(graphs, { ...options, writeEnabled: false });
};

function retainProjectIdentity(summary, archive) {
  const periods = [...PERIODS.map(name => summary[name]), ...Object.values(summary.nativeRangeDays || {})];
  for (const period of periods) {
    for (const [key, session] of Object.entries(period?.sessions || {})) {
      const retained = archive.sessions[key]?.periods?.allTime;
      if (retained?.projectLabel && retained.projectId !== unattributedProjectId) {
        session.projectLabel = retained.projectLabel;
        if (retained.projectId) session.projectId = retained.projectId;
        else delete session.projectId;
      } else if (session.projectId === unattributedProjectId || !session.projectLabel) {
        delete session.projectId;
        delete session.projectLabel;
      }
    }
  }
}

function removeOlderObservations(summary, archive, now) {
  const stale = new Map();
  for (const [key, live] of Object.entries(summary.allTime?.sessions || {})) {
    const retained = archive.sessions[key]?.periods?.allTime;
    if (!retained) continue;
    const retainedTime = Date.parse(retained.lastUsedAt);
    const liveTime = Date.parse(live.lastUsedAt);
    const older = Number.isFinite(retainedTime) && Number.isFinite(liveTime) && liveTime < retainedTime;
    // A resumed backup can write a newer turn before it has caught up with
    // the retained usage. Its timestamp alone cannot justify reducing history.
    if (older || live.totalTokens < retained.totalTokens) stale.set(key, [live, retained]);
  }
  if (stale.size === 0) return stale;

  const day = summary.periodWindows.today.key;
  const month = day.slice(0, 7);
  const observed = { sessions: {} };
  for (const key of stale.keys()) {
    const periods = {};
    for (const name of PERIODS) {
      const row = summary[name]?.sessions?.[key];
      if (row) periods[name] = row;
    }
    const { client, sessionId } = periods.allTime;
    observed.sessions[key] = { client, sessionId, day, month, periods };
  }
  const empty = Object.fromEntries(PERIODS.map(name => [name, emptyPeriod()]));
  const redundant = applySessionUsageArchive(empty, observed, { now, canonical: true, mutate: true });
  for (const name of PERIODS) {
    if (!summary[name]) continue;
    const current = applyPeriodDelta(summary[name], emptyPeriod(), redundant[name]);
    for (const key of stale.keys()) delete current.sessions[key];
    summary[name] = current;
  }

  // Native ranges are derived before this selection. Leave those optional
  // projections to the Hub's history fallback for a mixed restore scan.
  delete summary.nativeRanges;
  delete summary.nativeRangeWindows;
  for (const day of Object.values(summary.nativeRangeDays || {})) {
    day.sessions = Object.fromEntries(Object.entries(day.sessions).filter(([key]) => !stale.has(key)));
  }
  delete summary.dailySessionPeriods;
  return stale;
}

function restoreHistory(graphs, options, summary, stale, archive, now) {
  // Day graph rows have no session identity. Use exact upstream session-day
  // projection where the window scan provides it. For older affected days,
  // keep previous observations for the affected providers and admit the rest.
  const formatter = new Intl.DateTimeFormat('sv-SE', { timeZone: options.timeZone });
  const dates = value => {
    const millis = Date.parse(value);
    return Number.isFinite(millis) ? formatter.format(new Date(millis)) : null;
  };
  const affected = [...stale.values()].map(([live, retained]) => {
    const days = [dates(live.startedAt), dates(live.lastUsedAt),
      dates(retained.startedAt), dates(retained.lastUsedAt)].filter(Boolean).sort();
    return { client: live.client, first: days[0], last: days.at(-1) };
  });
  const affectedOn = day => new Set(affected.filter(
    ({ first, last }) => !first || (first <= day && day <= last)
  ).map(({ client }) => client));
  const scanDays = [options.todayKey, ...Object.keys(summary.nativeRangeDays || {})];
  const exactDays = scanDays.filter(day => affectedOn(day).size > 0).sort();
  const nativeRangeWindows = Object.fromEntries(exactDays.map(day => [day, { start: day, end: day }]));
  const nativeRanges = Object.fromEntries(exactDays.map(day => [day, emptyPeriod()]));
  const exact = applySessionDayArchive({ nativeRangeWindows, nativeRanges }, archive, { now, canonical: true });
  const { liveDays } = exactDays.reduce((days, day) => dailyArchive.captureLiveDailyHistory(
    days, exact.nativeRanges[day], { todayKey: day, pricingRevision: options.pricingRevision }
  ), {});
  let partial = false;
  const filtered = graphs.map(graph => ({
    ...graph,
    contributions: (graph.contributions || []).flatMap(row => {
      if (Object.hasOwn(nativeRanges, row.date)) return [];
      const clients = affectedOn(row.date);
      if (!clients.size) return [row];
      const rows = row.clients || [];
      const observed = rows.filter(item => !clients.has(normalizeClientName(item.client)));
      partial ||= observed.length < rows.length;
      return observed.length ? [{ ...row, clients: observed }] : [];
    }),
  }));
  if (liveDays) filtered.push(dailyArchive.graphFromDailyHistoryArchive([], { liveDays }, {}));
  return { graphs: filtered, partial };
}

function finishHistory(summary, stale, archive, now) {
  if (!historyScan) return;
  const { graphs, options } = historyScan;
  historyScan = undefined;
  const selected = stale.size ? restoreHistory(graphs, options, summary, stale, archive, now) : { graphs };
  const graph = retainHistory(selected.graphs, options);
  const history = normalizeHistory(parseGraphResult(graph), { capDays: options.capDays, todayKey: options.todayKey });
  if (selected.partial || summary.history?.summary?.activeDaysComplete === false) {
    history.summary.activeDaysComplete = false;
  }
  if (history.daily.length || history.monthly.length) summary.history = history;
  else delete summary.history;
}

const createStore = storeModule.createSessionUsageArchiveStore;
storeModule.createSessionUsageArchiveStore = function createRestoreAwareStore(options) {
  const store = createStore(options);
  const capture = store.capture;
  store.capture = (summary, now) => {
    const archive = store.read(now);
    retainProjectIdentity(summary, archive);
    const stale = removeOlderObservations(summary, archive, now);
    const result = capture(summary, now);
    if (!result.error) finishHistory(summary, stale, result.archive, now);
    return result;
  };
  return store;
};
