'use strict';

// Loaded before the official headless agent. Select the newest known session
// observation and leave period projection and persistence to Token Monitor.
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
    const stale = removeOlderObservations(summary, store.read(now), now);
    const result = capture(summary, now);
    if (!result.error) finishHistory(summary, stale, result.archive, now);
    return result;
  };
  return store;
};
