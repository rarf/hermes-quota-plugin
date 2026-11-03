"""Executable regressions for focused-profile desktop quota routing and refreshes."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "desktop" / "plugin.js"


def _run_routing_probe() -> dict[str, Any]:
    runner = r"""
const fs = require("fs");
const sourcePath = process.argv[1];
const source = fs.readFileSync(sourcePath, "utf8");
const start = source.indexOf('const QUOTA_QUERY_KEY =');
const end = source.indexOf('\nfunction useQuota()', start);
if (start < 0 || end < 0) {
  throw new Error("quota data block not found in desktop/plugin.js");
}

function extractObjectAfter(marker) {
  const markerAt = source.indexOf(marker);
  const objectAt = source.indexOf("{", markerAt + marker.length);
  if (markerAt < 0 || objectAt < 0) throw new Error(`missing ${marker}`);
  let depth = 0;
  let quote = "";
  let escaped = false;
  for (let i = objectAt; i < source.length; i += 1) {
    const ch = source[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === quote) quote = "";
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      quote = ch;
      continue;
    }
    if (ch === "{") depth += 1;
    else if (ch === "}" && --depth === 0) return source.slice(objectAt, i + 1);
  }
  throw new Error(`unterminated object after ${marker}`);
}

const storage = new Map();
const calls = [];
let focusedOwner = { connectionId: "remote-a", profile: "shared" };
let hangRoutes = false;
let releaseRoutes = null;
let profileRoutesCalls = 0;
let hangLive = false;
let releaseLive = null;
let liveError = null;
let unhandledRejections = 0;
process.on("unhandledRejection", () => {
  unhandledRejections += 1;
});
const liveCalls = [];
let activeProfile = "shared";
let routes = [
  { connectionId: "local", profile: "default", targetProfile: "default", mode: "local" },
  { connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote" },
  { connectionId: "remote-b", profile: "shared", targetProfile: "backend-shared", mode: "remote" },
  { connectionId: "remote-b", profile: "other", targetProfile: "other", mode: "remote" },
];
let nextResult = { code: 0, output: "{}" };
const host = {
  state: {
    focusedSessionOwner: { get: () => focusedOwner },
    profile: { get: () => activeProfile },
  },
  profileRoutes: () => {
    profileRoutesCalls += 1;
    return hangRoutes
      ? new Promise((resolve) => { releaseRoutes = () => resolve(routes); })
      : Promise.resolve(routes);
  },
  requestProfile: async (route, method, params, timeoutMs, options) => {
    calls.push({ route, method, params, timeoutMs, options });
    return nextResult;
  },
  request: async (method, params) => {
    liveCalls.push({ method, params });
    if (hangLive) {
      return new Promise((resolve, reject) => {
        releaseLive = (error) => {
          if (error) reject(error);
          else resolve({ code: 0, output: "{}" });
        };
      });
    }
    if (liveError) throw liveError;
    return { code: 0, output: "{}" };
  },
};
const CTX = {
  storage: {
    get: (key) => storage.get(key),
    set: (key, value) => storage.set(key, value),
  },
};
const factory = new Function(
  "host",
  "CTX",
  "setTimeout",
  "clearTimeout",
  "ANTIGRAVITY_PROVIDER_ID",
  "baseProviderId",
  `${source.slice(start, end)}\nreturn { CLI_TIMEOUT_MS, REFRESH_TIMEOUT_MS, normalizeQuotaScope, quotaScopeKey, antigravitySwitchState, parseJsonOutput, quotaQueryKey, readSnapshot, writeSnapshot, quotaCli, refreshQuotaCache, withAntigravityVisibility, refreshFocusedQuota: typeof refreshFocusedQuota === "function" ? refreshFocusedQuota : undefined };`,
);
const api = factory(
  host,
  CTX,
  setTimeout,
  clearTimeout,
  "antigravity",
  (pid) => String(pid || "").split(":")[0],
);
const invalidated = [];
const mutationConfig = new Function(
  "refreshFocusedQuota",
  "host",
  "normalizeQuotaScope",
  "quotaCli",
  "CLI_TIMEOUT_MS",
  "REFRESH_TIMEOUT_MS",
  "quotaQueryKey",
  "qc",
  `return (${extractObjectAfter("const refresh = useMutation(")});`,
)(
  api.refreshFocusedQuota,
  host,
  api.normalizeQuotaScope,
  api.quotaCli,
  api.CLI_TIMEOUT_MS,
  api.REFRESH_TIMEOUT_MS,
  api.quotaQueryKey,
  { invalidateQueries: (request) => invalidated.push(request.queryKey) },
);
const useQuotaStart = source.indexOf("function useQuota()");
const useQuotaEnd = source.indexOf("\n// ---- single worst chip", useQuotaStart);
if (useQuotaStart < 0 || useQuotaEnd < 0) throw new Error("useQuota block not found");
const makeUseQuota = new Function(
  "host",
  "refreshIntervalAtom",
  "antigravitySwitchAtom",
  "useQueryClient",
  "useValue",
  "useQuery",
  "normalizeQuotaScope",
  "quotaQueryKey",
  "quotaCli",
  "parseJsonOutput",
  "writeSnapshot",
  "readSnapshot",
  "refreshQuotaCache",
  "withAntigravityVisibility",
  "quotaScopeKey",
  "antigravitySwitchState",
  "useMemo",
  `${source.slice(useQuotaStart, useQuotaEnd)}\nreturn useQuota;`,
);

(async () => {
  const ownerA = api.normalizeQuotaScope({ connectionId: "remote-a", profile: "shared" });
  const ownerB = api.normalizeQuotaScope({ connectionId: "remote-b", profile: "shared" });

  await api.quotaCli(ownerB, ["quota", "status", "--json", "--cached"]);
  const routedCall = calls.shift();

  const manualRefresh = mutationConfig.mutationFn();
  focusedOwner = { connectionId: "remote-b", profile: "shared" };
  const manualScope = await manualRefresh;
  mutationConfig.onSuccess(manualScope);
  const manualCall = calls.shift();

  api.writeSnapshot(ownerA, { owner: "a" });
  api.writeSnapshot(ownerB, { owner: "b" });
  const snapshots = [api.readSnapshot(ownerA), api.readSnapshot(ownerB)];

  focusedOwner = { connectionId: "remote-a", profile: "shared" };
  nextResult = {
    code: 0,
    output: JSON.stringify({ age_s: null, providers: { codex: { windows: [] } } }),
  };
  const hookInvalidated = [];
  const refreshIntervalAtom = {};
  const antigravitySwitchAtom = {};
  const useQuota = makeUseQuota(
    host,
    refreshIntervalAtom,
    antigravitySwitchAtom,
    () => ({ invalidateQueries: (request) => hookInvalidated.push(request.queryKey) }),
    (atom) => atom === refreshIntervalAtom
      ? 60
      : atom === antigravitySwitchAtom
        ? {}
        : focusedOwner,
    (options) => options,
    api.normalizeQuotaScope,
    api.quotaQueryKey,
    api.quotaCli,
    api.parseJsonOutput,
    api.writeSnapshot,
    api.readSnapshot,
    api.refreshQuotaCache,
    api.withAntigravityVisibility,
    api.quotaScopeKey,
    api.antigravitySwitchState,
    (factory) => factory(),
  );
  const hookOptions = useQuota();
  focusedOwner = { connectionId: "remote-b", profile: "shared" };
  const hookData = await hookOptions.queryFn();
  await new Promise((resolve) => setImmediate(resolve));
  const hookCalls = calls.splice(0);

  // Regression: while the switch is off, a render that did not change the raw
  // payload must return the SAME filtered object. Rebuilding it on every
  // repaint gave every effect keyed on `data` (cache-age timer, notifier) a
  // new dependency and restarted them.
  const memoAtoms = { refresh: {}, switch: {} };
  const memoScopeKey = api.quotaScopeKey(api.normalizeQuotaScope(focusedOwner));
  const memoPayload = {
    age_s: 5,
    providers: {
      antigravity: { windows: [] },
      "antigravity:work": { windows: [] },
      anthropic: { windows: [] },
    },
    provider_switches: { antigravity: { enabled: false, setting: "antigravityEnabled" } },
  };
  let memoData = memoPayload;
  let memoOverrides = { [memoScopeKey]: false };
  let memoState = null;
  const memoize = (factory, deps) => {
    if (
      memoState &&
      memoState.deps.length === deps.length &&
      deps.every((dep, index) => Object.is(dep, memoState.deps[index]))
    ) {
      return memoState.value;
    }
    const value = factory();
    memoState = { deps, value };
    return value;
  };
  const useMemoQuota = makeUseQuota(
    host,
    memoAtoms.refresh,
    memoAtoms.switch,
    () => ({ invalidateQueries: () => {} }),
    (atom) => atom === memoAtoms.refresh
      ? 60
      : atom === memoAtoms.switch
        ? memoOverrides
        : focusedOwner,
    (options) => ({ ...options, data: memoData }),
    api.normalizeQuotaScope,
    api.quotaQueryKey,
    api.quotaCli,
    api.parseJsonOutput,
    api.writeSnapshot,
    api.readSnapshot,
    api.refreshQuotaCache,
    api.withAntigravityVisibility,
    api.quotaScopeKey,
    api.antigravitySwitchState,
    memoize,
  );
  const memoFirst = useMemoQuota();
  const memoSecond = useMemoQuota();
  const filteredOnce = memoFirst.data;
  const filteredStable = memoFirst.data === memoSecond.data;
  const filteredShape =
    filteredOnce !== memoPayload &&
    filteredOnce.providers !== memoPayload.providers &&
    filteredOnce.providers.anthropic === memoPayload.providers.anthropic &&
    !("antigravity" in filteredOnce.providers) &&
    !("antigravity:work" in filteredOnce.providers) &&
    filteredOnce.provider_switches === memoPayload.provider_switches;
  const nextPayload = { ...memoPayload, age_s: 6 };
  memoData = nextPayload;
  const memoThird = useMemoQuota();
  const refiltersNewPayload =
    memoThird.data !== filteredOnce &&
    memoThird.data !== nextPayload &&
    memoThird.data.age_s === 6;
  memoOverrides = { [memoScopeKey]: true };
  const memoOn = useMemoQuota();
  const onReturnsRaw = memoOn.data === nextPayload;
  memoOverrides = { [memoScopeKey]: false };
  const memoOffAgain = useMemoQuota();
  const memoOffAgainSecond = useMemoQuota();
  const offAgainStable =
    memoOffAgain.data === memoOffAgainSecond.data &&
    memoOffAgain.data !== nextPayload;

  nextResult = { code: 127, output: "quota: command not found" };
  let failure = "";
  try {
    await api.quotaCli(ownerA, ["quota", "refresh"]);
  } catch (error) {
    failure = String(error && error.message || error);
  }
  const failedCalls = calls.splice(0);

  nextResult = { code: 0, output: "{}" };
  const refreshResults = await Promise.all([
    api.refreshQuotaCache(ownerA),
    api.refreshQuotaCache(ownerA),
    api.refreshQuotaCache(ownerB),
  ]);
  const refreshCalls = calls.splice(0);

  routes = [
    { connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote" },
    { connectionId: "remote-a", profile: "shared", targetProfile: "other", mode: "remote" },
  ];
  let ambiguousFailure = "";
  try {
    await api.quotaCli(ownerA, ["quota", "status"]);
  } catch (error) {
    ambiguousFailure = String(error && error.message || error);
  }
  const callsAfterAmbiguousRoute = calls.length;

  routes = [];
  let missingFailure = "";
  try {
    await api.quotaCli(ownerA, ["quota", "status"]);
  } catch (error) {
    missingFailure = String(error && error.message || error);
  }
  const callsAfterMissingRoute = calls.length;

  let nullOwnerFailure = "";
  try {
    await api.quotaCli(api.normalizeQuotaScope(null), ["quota", "status"]);
  } catch (error) {
    nullOwnerFailure = String(error && error.message || error);
  }

  // Issue #79: a legacy single connection reports an owner without a registry
  // connectionId. It must use the live gateway; a null owner must stay disabled.
  const nullScope = api.normalizeQuotaScope(null);
  const legacyOwner = { connectionId: "", profile: "default" };
  const legacyScope = api.normalizeQuotaScope(legacyOwner);
  const legacyRoutesBefore = profileRoutesCalls;
  const legacyRequestProfileBefore = calls.length;
  const legacyLiveBefore = liveCalls.length;
  const legacyResult = await api.quotaCli(
    legacyScope,
    ["quota", "status", "--json", "--cached"],
  );
  const legacyCall = liveCalls[legacyLiveBefore];
  const legacyProfileRoutesDelta = profileRoutesCalls - legacyRoutesBefore;
  const legacyRequestProfileDelta = calls.length - legacyRequestProfileBefore;

  const legacyNoProfileScope = api.normalizeQuotaScope({ connectionId: "  ", profile: "  " });
  const legacyNoProfileLiveBefore = liveCalls.length;
  await api.quotaCli(legacyNoProfileScope, ["quota", "status"]);
  const legacyNoProfileCall = liveCalls[legacyNoProfileLiveBefore];

  activeProfile = "";
  const legacyDefaultScope = api.normalizeQuotaScope({ connectionId: "" });
  activeProfile = "shared";

  let liveTimeoutFailure = "";
  const liveTimeoutBefore = liveCalls.length;
  hangLive = true;
  try {
    await api.quotaCli(legacyScope, ["quota", "status"], 5);
  } catch (error) {
    liveTimeoutFailure = String(error && error.message || error);
  }
  hangLive = false;
  if (releaseLive) {
    releaseLive(new Error("late socket drop"));
    releaseLive = null;
  }
  await new Promise((resolve) => setImmediate(resolve));
  const liveTimeoutCall = liveCalls[liveTimeoutBefore];

  let liveRejectionFailure = "";
  liveError = new Error("live socket closed");
  try {
    await api.quotaCli(legacyScope, ["quota", "status"]);
  } catch (error) {
    liveRejectionFailure = String(error && error.message || error);
  }
  liveError = null;

  hangRoutes = true;
  let timeoutFailure = "";
  try {
    await api.quotaCli(ownerA, ["quota", "status"], 5);
  } catch (error) {
    timeoutFailure = String(error && error.message || error);
  }
  hangRoutes = false;
  releaseRoutes();
  releaseRoutes = null;
  await new Promise((resolve) => setImmediate(resolve));
  const callsAfterTimeout = calls.length;

  const fastTimeoutApi = factory(
    host,
    CTX,
    (callback, delay) => setTimeout(callback, Math.min(delay, 5)),
    clearTimeout,
    "antigravity",
    (pid) => String(pid || "").split(":")[0],
  );
  routes = [
    { connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote" },
  ];
  hangRoutes = true;
  const timedOutRefresh = await fastTimeoutApi.refreshQuotaCache(ownerA);
  hangRoutes = false;
  releaseRoutes();
  releaseRoutes = null;
  await new Promise((resolve) => setImmediate(resolve));
  const lateRefreshCalls = calls.splice(0);
  nextResult = { code: 0, output: "{}" };
  const refreshAfterTimeout = await fastTimeoutApi.refreshQuotaCache(ownerA);
  const refreshAfterTimeoutCalls = calls.splice(0);

  // A switch flip must not be dropped because the poll path is already
  // sweeping with the old setting. `trailing` waits for that sweep and then
  // runs exactly one more; the extra caller coalesces into the same follow-up.
  routes = [
    { connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote" },
  ];
  hangRoutes = true;
  const trailingFirst = api.refreshQuotaCache(ownerA);
  await new Promise((resolve) => setImmediate(resolve));
  const trailingSecond = api.refreshQuotaCache(ownerA, { trailing: true });
  const trailingThird = api.refreshQuotaCache(ownerA, { trailing: true });
  const plainWhileTrailing = await api.refreshQuotaCache(ownerA);
  hangRoutes = false;
  releaseRoutes();
  releaseRoutes = null;
  const trailingResults = await Promise.all([trailingFirst, trailingSecond, trailingThird]);
  const trailingCalls = calls.splice(0);

  process.stdout.write(JSON.stringify({
    ownerA,
    ownerB,
    queryKeys: [api.quotaQueryKey(ownerA), api.quotaQueryKey(ownerB)],
    routedCall,
    manualScope,
    manualCall,
    invalidated,
    snapshots,
    storageKeys: [...storage.keys()],
    hookQueryKey: hookOptions.queryKey,
    hookEnabled: hookOptions.enabled,
    hookPlaceholder: hookOptions.placeholderData,
    hookData,
    hookCalls,
    hookInvalidated,
    failure,
    failedCalls,
    refreshResults,
    refreshCalls,
    ambiguousFailure,
    missingFailure,
    nullOwnerFailure,
    nullScope,
    legacyScope,
    legacyResult,
    legacyCall,
    legacyProfileRoutesDelta,
    legacyRequestProfileDelta,
    legacyNoProfileScope,
    legacyNoProfileCall,
    legacyDefaultScope,
    liveTimeoutFailure,
    liveTimeoutCall,
    liveRejectionFailure,
    unhandledRejections,
    liveCalls,
    callsAfterAmbiguousRoute,
    callsAfterMissingRoute,
    timeoutFailure,
    callsAfterTimeout,
    timedOutRefresh,
    lateRefreshCalls,
    refreshAfterTimeout,
    refreshAfterTimeoutCalls,
    trailingResults,
    plainWhileTrailing,
    trailingCalls,
    quotaMemo: {
      filteredStable,
      filteredShape,
      refiltersNewPayload,
      onReturnsRaw,
      offAgainStable,
    },
    REFRESH_TIMEOUT_MS: api.REFRESH_TIMEOUT_MS,
  }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", runner, str(PLUGIN)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "widget routing probe failed: "
            f"exit={completed.returncode}, stderr={completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


class WidgetProfileRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = _run_routing_probe()

    def test_cli_routes_to_exact_connection_and_profile(self) -> None:
        call = self.result["routedCall"]
        self.assertEqual("remote-b", call["route"]["connectionId"])
        self.assertEqual("shared", call["route"]["profile"])
        self.assertEqual("cli.exec", call["method"])

    def test_cli_exec_selects_the_route_backend_profile(self) -> None:
        call = self.result["routedCall"]
        self.assertEqual(
            [
                "--profile",
                "backend-shared",
                "quota",
                "status",
                "--json",
                "--cached",
            ],
            call["params"]["argv"],
        )

    def test_failures_do_not_fall_back_to_default_or_ambiguous_routes(self) -> None:
        self.assertIn("command not found", self.result["failure"])
        self.assertEqual(1, len(self.result["failedCalls"]))
        self.assertEqual(
            "remote-a", self.result["failedCalls"][0]["route"]["connectionId"]
        )
        self.assertTrue(self.result["ambiguousFailure"])
        self.assertTrue(self.result["missingFailure"])
        self.assertTrue(self.result["nullOwnerFailure"])
        self.assertEqual(0, self.result["callsAfterAmbiguousRoute"])
        self.assertEqual(0, self.result["callsAfterMissingRoute"])

    def test_legacy_single_connection_uses_the_live_gateway(self) -> None:
        self.assertIsNone(self.result["nullScope"])
        self.assertEqual(
            {"connectionId": "", "profile": "default", "live": True},
            self.result["legacyScope"],
        )
        call = self.result["legacyCall"]
        self.assertEqual("cli.exec", call["method"])
        self.assertEqual(
            [
                "--profile",
                "default",
                "quota",
                "status",
                "--json",
                "--cached",
            ],
            call["params"]["argv"],
        )
        self.assertEqual({"code": 0, "output": "{}"}, self.result["legacyResult"])
        self.assertEqual(0, self.result["legacyProfileRoutesDelta"])
        self.assertEqual(0, self.result["legacyRequestProfileDelta"])

    def test_legacy_single_connection_resolves_the_live_profile(self) -> None:
        self.assertEqual(
            {"connectionId": "", "profile": "shared", "live": True},
            self.result["legacyNoProfileScope"],
        )
        self.assertEqual(
            ["--profile", "shared", "quota", "status"],
            self.result["legacyNoProfileCall"]["params"]["argv"],
        )
        self.assertEqual(
            {"connectionId": "", "profile": "default", "live": True},
            self.result["legacyDefaultScope"],
        )
        self.assertEqual(4, len(self.result["liveCalls"]))

    def test_live_gateway_timeout_and_rejection_are_bounded(self) -> None:
        self.assertIn("quota cli timeout", self.result["liveTimeoutFailure"])
        self.assertEqual("cli.exec", self.result["liveTimeoutCall"]["method"])
        self.assertEqual("live socket closed", self.result["liveRejectionFailure"])
        self.assertEqual(0, self.result["unhandledRejections"])

    def test_manual_refresh_captures_click_time_owner_at_foreground_priority(self) -> None:
        self.assertEqual(
            {"connectionId": "remote-a", "profile": "shared"},
            self.result["manualScope"],
        )
        call = self.result["manualCall"]
        self.assertEqual("remote-a", call["route"]["connectionId"])
        self.assertEqual({"spawnPriority": "foreground"}, call["options"])
        self.assertEqual(
            [["quota", "widget", "remote-a", "shared"]],
            self.result["invalidated"],
        )

    def test_query_keys_and_snapshots_are_scope_isolated(self) -> None:
        self.assertNotEqual(*self.result["queryKeys"])
        self.assertEqual([{"owner": "a"}, {"owner": "b"}], self.result["snapshots"])
        keys = self.result["storageKeys"]
        self.assertEqual(2, len(keys))
        self.assertNotEqual(*keys)
        self.assertNotIn("lastPayload", keys)

    def test_query_hook_captures_scope_for_read_refresh_and_invalidation(self) -> None:
        expected_key = ["quota", "widget", "remote-a", "shared"]
        self.assertEqual(expected_key, self.result["hookQueryKey"])
        self.assertTrue(self.result["hookEnabled"])
        self.assertEqual({"owner": "a"}, self.result["hookPlaceholder"])
        self.assertEqual({"codex": {"windows": []}}, self.result["hookData"]["providers"])
        self.assertEqual(2, len(self.result["hookCalls"]))
        self.assertTrue(
            all(
                call["route"]["connectionId"] == "remote-a"
                and call["route"]["profile"] == "shared"
                for call in self.result["hookCalls"]
            )
        )
        self.assertEqual([expected_key], self.result["hookInvalidated"])

    def test_filtered_payload_identity_is_stable_across_renders(self) -> None:
        memo = self.result["quotaMemo"]
        self.assertTrue(
            memo["filteredStable"],
            "the off-switch payload changed identity between unchanged renders",
        )
        self.assertTrue(
            memo["filteredShape"],
            "the filtered payload lost Antigravity rows or copied its siblings",
        )
        self.assertTrue(
            memo["refiltersNewPayload"],
            "a new raw payload must produce a new filtered payload",
        )
        self.assertTrue(memo["onReturnsRaw"], "an on switch must render the raw payload")
        self.assertTrue(
            memo["offAgainStable"],
            "turning the switch off again must not rebuild the payload per render",
        )

    def test_route_discovery_is_bounded_by_cli_timeout(self) -> None:
        self.assertIn("quota cli timeout", self.result["timeoutFailure"])
        self.assertEqual(0, self.result["callsAfterTimeout"])
        self.assertFalse(self.result["timedOutRefresh"])
        self.assertEqual([], self.result["lateRefreshCalls"])
        self.assertTrue(self.result["refreshAfterTimeout"])
        self.assertEqual(1, len(self.result["refreshAfterTimeoutCalls"]))

    def test_refresh_deduplication_is_per_scope(self) -> None:
        self.assertEqual([True, False, True], self.result["refreshResults"])
        calls = self.result["refreshCalls"]
        self.assertEqual(2, len(calls))
        self.assertEqual(
            {"remote-a", "remote-b"},
            {call["route"]["connectionId"] for call in calls},
        )

    def test_refresh_deadline_covers_the_backend_sweep_budget(self) -> None:
        # quota_cache.REFRESH_BUDGET_S is 20s; a 15s UI deadline used to abort
        # a healthy sweep and leave the card a switch behind.
        self.assertGreaterEqual(self.result["REFRESH_TIMEOUT_MS"], 20_000)
        self.assertGreaterEqual(self.result["manualCall"]["timeoutMs"], 20_000)
        self.assertLessEqual(
            self.result["manualCall"]["timeoutMs"], self.result["REFRESH_TIMEOUT_MS"]
        )
        for call in self.result["refreshCalls"]:
            self.assertGreaterEqual(call["timeoutMs"], 20_000)
        for call in self.result["trailingCalls"]:
            self.assertGreaterEqual(call["timeoutMs"], 20_000)

    def test_switch_refresh_trails_an_in_flight_sweep_instead_of_being_dropped(self) -> None:
        self.assertEqual([True, True, True], self.result["trailingResults"])
        self.assertFalse(self.result["plainWhileTrailing"])
        calls = self.result["trailingCalls"]
        self.assertEqual(2, len(calls), "one in-flight sweep plus exactly one follow-up")
        self.assertEqual(
            [["quota", "refresh"], ["quota", "refresh"]],
            [call["params"]["argv"][2:] for call in calls],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
