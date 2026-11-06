"""Desktop Nous card: Billing RPC primary, cached CLI row as fallback.

The mapping/routing probe executes the real billing block from
``desktop/plugin.js`` with a fake gateway; the render tests execute the real
component through ``widget_monitor_renderer.cjs``. Billing payloads are
SYNTHETIC and include decoy card/org fields so the disclosure checks prove
none of them reach the row, the DOM or plugin storage.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from test_widget_monitor import NOW, iso, run, snapshot
from widget_harness import nodes, text, tooltip_text

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "desktop" / "plugin.js"


def _cli_payload(plan="Tier 9", used=5):
    return {
        "age_s": 30,
        "providers": {
            "nous": {
                "label": "nous",
                "plan": plan,
                "windows": [{"label": "Stale cache window", "used_percent": used}],
                "details": ["stale cache detail"],
                "access_state": "active",
                "api_calls_available": None,
            },
            "anthropic": {"windows": []},
        },
    }


def _credit_billing(**over):
    base = {
        "ok": True,
        "logged_in": True,
        "free_tier_account": False,
        "free_tier_model": None,
        # Decoys: the card must ignore these, and nothing may persist them.
        "org_name": "SECRET-ORG",
        "org_slug": "secret-org",
        "role": "OWNER",
        "balance_display": "$3.40",
        "card": {"brand": "Visa", "last4": "9999", "masked": "Visa ····9999"},
        "payment_method": {"kind": "card", "brand": "Visa", "last4": "9999"},
        "auto_reload": {"enabled": True, "threshold_display": "$5.00",
                        "reload_to_display": "$25.00", "card": {"kind": "canonical"}},
        "usage": {
            "ok": True,
            "available": True,
            "status": "healthy",
            "plan_name": "Pro",
            "renews_display": "Dec 3, 2026",
            "plan_bar": {
                "kind": "plan", "remaining_display": "$14.00",
                "total_display": "$20.00", "spent_display": "$6.00",
                "pct_used": 30, "fill_fraction": 0.7,
            },
            "topup_bar": {
                "kind": "topup", "remaining_display": "$12.00",
                "total_display": "$12.00", "spent_display": "$0.00",
                "pct_used": None, "fill_fraction": 1.0,
            },
            "has_topup": True,
            "total_spendable_display": "$26.00",
        },
        "plan_usage": None,
        "monthly_cap": {
            "limit_display": "$200.00", "spent_display": "$6.00",
            "is_default_ceiling": False,
        },
    }
    base.update(over)
    return base


def _window_billing(**over):
    base = {
        "ok": True,
        "logged_in": True,
        "free_tier_account": False,
        "usage": {"ok": True, "available": True},
        "plan_usage": {
            "kind": "usage_window",
            "plan": "Pro Max",
            "gauges": [
                {"label": "day", "left_fraction": 0.8, "style": "bar",
                 "resets_at_ms": int(NOW + 3600) * 1000, "scope": "all",
                 "title": "Daily included usage"},
                {"label": "week", "left_fraction": 0.25, "style": "bar",
                 "resets_at_ms": None, "scope": "all",
                 "title": "Weekly included usage"},
            ],
            "top_tier_model_ids": [],
            "subscription_model_ids": [],
            "featured_model_ids": [],
            "paused_reason": None,
            "credits_pay": None,
        },
        "org_name": "SECRET-ORG",
        "card": {"brand": "Visa", "last4": "9999"},
    }
    base.update(over)
    return base


def _plus_window_billing(**over):
    """The real live Plus `billing.state` shape (sanitized).

    A usage-window plan: core's dollar-credit model is unavailable, so Billing
    cannot compute a usage total, but `balance_display` still reports the
    account wallet. The live labels are the server ids; the card must render
    the exact card names instead. The org slug matches
    `_cli_plus_window_payload`'s FNV-1a account key, so the compatible account
    money facts may merge.
    """
    base = {
        "ok": True,
        "logged_in": True,
        "free_tier_account": False,
        "free_tier_model": None,
        "usage": {"ok": True, "available": False},
        "plan_usage": {
            "kind": "usage_window",
            "plan": "Hermes Plus",
            "gauges": [
                {"label": "day", "left_fraction": 1.0, "style": "green",
                 "resets_at_ms": int((NOW + 6 * 3600) * 1000), "scope": "all",
                 "title": "Today"},
                {"label": "week", "left_fraction": 1.0, "style": "green",
                 "resets_at_ms": int((NOW + 3 * 86400) * 1000), "scope": "all",
                 "title": "This week"},
                {"label": "month", "left_fraction": 1.0, "style": "green",
                 "resets_at_ms": int((NOW + 30 * 86400) * 1000), "scope": "all",
                 "title": "This month"},
                {"label": "top_tier", "left_fraction": 1.0, "style": "green",
                 "resets_at_ms": int((NOW + 30 * 86400) * 1000), "scope": "top_tier",
                 "title": "Large models (monthly)"},
            ],
            "top_tier_model_ids": [],
            "subscription_model_ids": [],
            "featured_model_ids": [
                "deepseek/deepseek-v4-flash-0731",
                "z-ai/glm-5.3",
                "stepfun/step-3.7-flash:free",
            ],
            "paused_reason": None,
            "credits_pay": False,
        },
        "monthly_cap": {
            "limit_usd": "1000", "limit_display": "$1000",
            "spent_this_month_usd": "0", "spent_display": "$0",
            "is_default_ceiling": True,
        },
        "keep_going_on_credits": False,
        # Decoys: never rendered or persisted.
        "org_name": "SECRET-ORG",
        "org_slug": "hermes-plus-org",
        "role": "OWNER",
        "balance_display": "$9.99",
        "card": {"brand": "Visa", "last4": "9999", "masked": "Visa ····9999"},
        "payment_method": {"kind": "card", "brand": "Visa", "last4": "9999"},
        "auto_reload": {"enabled": False, "threshold_display": "$5.00",
                        "reload_to_display": "$25.00", "card": {"kind": "none"}},
    }
    base.update(over)
    return base


def _cli_plus_window_payload():
    """The live `quota status --json --cached` row after the CLI Plus fix.

    Mirrors the account helper's returned fields: four usage windows, the
    Account balance, the subscription period end and the period spend. A
    component line is deliberately absent: the account's total is the only
    money row the card renders.
    """
    return {
        "age_s": 20,
        "providers": {
            "nous": {
                "label": "nous",
                "plan": "Hermes Plus",
                "windows": [
                    {"label": "Daily", "used_percent": 0,
                     "reset_at": iso(6 * 3600)},
                    {"label": "Weekly", "used_percent": 0,
                     "reset_at": iso(3 * 86400)},
                    {"label": "Monthly", "used_percent": 0,
                     "reset_at": iso(30 * 86400)},
                    {"label": "Large models (Monthly)", "used_percent": 0,
                     "reset_at": iso(30 * 86400)},
                ],
                "details": [
                    "Cycle ends: 2026-12-06T13:00:47.000Z",
                    "Spend this period: $0.00 (no cap reported)",
                ],
                "detail_facts": {
                    "cycle_ends": "Cycle ends: 2026-12-06T13:00:47.000Z",
                    "spend": "Spend this period: $0.00 (no cap reported)",
                },
                "account_balances": [
                    {"currency": "USD", "total_balance": "9.99",
                     "label": "Account balance"},
                ],
                "access_state": "active",
                "api_calls_available": True,
                "account_org_key": "5595a7bb8ee9c3d5",  # FNV-1a("hermes-plus-org")
            },
            "anthropic": {"windows": []},
        },
    }


def _free_billing(**over):
    base = {
        "ok": True,
        "logged_in": False,
        "free_tier_account": True,
        "free_tier_model": "nous/welcome",
        "usage": {"ok": True, "available": False},
        "card": {"brand": "Visa", "last4": "9999"},
        "org_name": "SECRET-ORG",
        "org_slug": "synthetic-org",
    }
    base.update(over)
    return base


def _free_plan_billing(**over):
    """The real signed-in Free Billing shape.

    Core's aggregate `usage.status` is "depleted" whenever paid access is
    false, so a Free-plan account reports "depleted" while its plan_name is
    literally "Free". plan_usage is null because a free plan has no window.
    """
    base = {
        "ok": True,
        "logged_in": True,
        "free_tier_account": False,
        "free_tier_model": None,
        "usage": {
            "ok": True,
            "available": True,
            "status": "depleted",
            "plan_name": "Free",
            "renews_display": "Dec 3, 2026",
        },
        "plan_usage": None,
        "org_slug": "synthetic-org",
    }
    base.update(over)
    return base


def _free_topup_billing(**over):
    """A Free plan whose purchased credits make the aggregate usage healthy.

    `usage.available: true` and a top-up bar describe the separate purchased
    wallet, not a paid subscription. The plan is still Free, and its padded
    renewal date must not be rendered.
    """
    usage = _core_usage(
        status="healthy",
        plan_name="Free",
        renews_display="Jan 1, 2027",
        topup_remaining_display="$10.00",
        total_spendable_display="$10.00",
        has_topup=True,
        topup_bar=_core_topup_bar("$10.00"),
    )
    return _billing_with_usage(usage, **over)


def _paused_window_billing(**over):
    base = _window_billing()
    base["usage"] = {"ok": True, "available": False}
    base["plan_usage"] = {
        **base["plan_usage"],
        "paused_reason": "payment_past_due",
    }
    base.update(over)
    return base


def _core_usage(**over):
    """The exact field set ``_serialize_usage_model`` emits for a credits plan."""
    base = {
        "ok": True,
        "available": True,
        "status": "healthy",
        "plan_name": None,
        "renews_at": None,
        "renews_display": None,
        "subscription_remaining_display": None,
        "topup_remaining_display": None,
        "total_spendable_display": None,
        "has_topup": False,
        "plan_bar": None,
        "topup_bar": None,
    }
    base.update(over)
    return base


def _core_plan_bar(remaining, total, spent, pct_used, fill_fraction):
    return {
        "kind": "plan",
        "remaining_display": remaining,
        "total_display": total,
        "spent_display": spent,
        "pct_used": pct_used,
        "fill_fraction": fill_fraction,
    }


def _core_topup_bar(remaining):
    return {
        "kind": "topup",
        "remaining_display": remaining,
        "total_display": remaining,
        "spent_display": "$0.00",
        "pct_used": None,
        "fill_fraction": 1.0,
    }


def _billing_with_usage(usage, plan_usage=None, **over):
    base = _credit_billing()
    base["usage"] = usage
    base["plan_usage"] = plan_usage
    base["monthly_cap"] = None
    base.update(over)
    return base


def _depleted_subscription_billing(**over):
    """Core shape: a paid plan whose monthly allowance is fully spent."""
    usage = _core_usage(
        status="depleted",
        plan_name="Pro",
        renews_at="2026-12-03T00:00:00Z",
        renews_display="Dec 3, 2026",
        subscription_remaining_display="$0.00",
        topup_remaining_display="$0.00",
        total_spendable_display="$0.00",
        plan_bar=_core_plan_bar("$0.00", "$20.00", "$20.00", 100, 0.0),
    )
    return _billing_with_usage(usage, **over)


def _depleted_subscription_no_bar_billing(**over):
    """Depletion corroborated by the zero balance, without a plan bar."""
    usage = _core_usage(
        status="depleted",
        plan_name="Pro",
        renews_at="2026-12-03T00:00:00Z",
        renews_display="Dec 3, 2026",
        subscription_remaining_display="$0.00",
        topup_remaining_display="$0.00",
        total_spendable_display="$0.00",
    )
    return _billing_with_usage(usage, **over)


def _member_cap_billing(**over):
    """Core shape: member spend cap exceeded, yet credits are still positive.

    ``usage_model_from_account`` derives ``status`` from the aggregate
    ``paid_service_access`` flag, so this reports "depleted" while the plan bar
    and both balances are positive. A top-up cannot restore it.
    """
    usage = _core_usage(
        status="depleted",
        plan_name="Pro",
        renews_at="2026-12-03T00:00:00Z",
        renews_display="Dec 3, 2026",
        subscription_remaining_display="$14.00",
        topup_remaining_display="$12.00",
        total_spendable_display="$26.00",
        has_topup=True,
        plan_bar=_core_plan_bar("$14.00", "$20.00", "$6.00", 30, 0.7),
        topup_bar=_core_topup_bar("$12.00"),
    )
    return _billing_with_usage(usage, **over)


def _signed_in_free_billing(**over):
    """Core shape: a signed-in Free plan has no plan_name and no balance."""
    usage = _core_usage(status="depleted")
    return _billing_with_usage(usage, **over)


def _paid_no_balance_billing(**over):
    """Core shape: a paid plan claim with no credit evidence at all."""
    usage = _core_usage(status="depleted", plan_name="Pro")
    return _billing_with_usage(usage, **over)


def _paused_credits_billing(credits_pay, **over):
    """Core window-plan shape with a paused plan and its credits_pay answer."""
    base = _window_billing()
    base["usage"] = {"ok": True, "available": False}
    base["plan_usage"] = {
        **base["plan_usage"],
        "paused_reason": "payment_past_due",
        "credits_pay": credits_pay,
    }
    base["keep_going_on_credits"] = credits_pay is not False
    base.update(over)
    return base


def _exhausted_window_billing(credits_pay=False, **over):
    """A window plan whose every general included allowance is fully spent."""
    base = _window_billing()
    base["plan_usage"] = {
        **base["plan_usage"],
        "gauges": [
            {**gauge, "left_fraction": 0.0}
            for gauge in base["plan_usage"]["gauges"]
        ],
        "credits_pay": credits_pay,
    }
    base.update(over)
    return base


def _paused_pay_empty_wallet_billing(**over):
    """A paused plan whose `credits_pay` promise has no funded wallet."""
    base = _paused_credits_billing(True, balance_display="$0.00")
    base.update(over)
    return base


def _exhausted_pay_empty_wallet_billing(**over):
    """A spent allowance whose `credits_pay` promise has no funded wallet."""
    base = _exhausted_window_billing(credits_pay=True, balance_display="$0.00")
    base.update(over)
    return base


def _top_tier_exhausted_window_billing(**over):
    """Only a top-tier allowance is spent: that does not gate paid calls."""
    base = _window_billing()
    base["plan_usage"] = {
        **base["plan_usage"],
        "gauges": [
            {"label": "top_tier", "left_fraction": 0.0, "style": "bar",
             "resets_at_ms": None, "scope": "top_tier",
             "title": "Large models (monthly)"},
        ],
        "credits_pay": False,
    }
    base.update(over)
    return base


def _paused_wallet_zero_billing(**over):
    """A paused Plus plan whose Billing wallet is explicitly empty."""
    base = _plus_window_billing(balance_display="$0.00", keep_going_on_credits=True)
    base["usage"] = {"ok": True, "available": False}
    base["plan_usage"] = {
        **base["plan_usage"],
        "paused_reason": "payment_past_due",
        "credits_pay": True,
    }
    base.update(over)
    return base


def _cli_paused_plus_payload():
    """A stale cached paused row whose total still counts withheld credits."""
    payload = _cli_plus_window_payload()
    row = payload["providers"]["nous"]
    row["access_state"] = "paused"
    row["api_calls_available"] = True
    row["account_balances"] = [
        {"currency": "USD", "total_balance": "18.00", "label": "Account balance"},
    ]
    return payload


def _cli_free_payload():
    return {
        "age_s": 30,
        "providers": {
            "nous": {
                "label": "nous",
                "plan": "Free",
                "windows": [],
                "details": ["Free tier - free models only"],
                "access_state": "free",
                "api_calls_available": None,
            },
            "anthropic": {"windows": []},
        },
    }


def _cli_master_payload(access_state="active", plan="Tier 9", *, age_s=30):
    """Cached old-master row with the facts the fresh account API lost.

    The lines mirror ``_fetch_nous_portal`` exactly, so this fixture proves the
    Desktop merges by fact identity rather than by ad-hoc text slicing.
    """
    payload = _cli_payload(plan=plan)
    row = payload["providers"]["nous"]
    row["access_state"] = access_state
    row["details"] = [
        "Subscription credits: $14.00",
        "Top-up credits: $12.00",
        "Total usable: $26.00",
        "Rollover: $3.00",
        "Spend this period: $6.00",
        "Rate limits: 50 RPM · 500k TPM · 2.1k RPH",
    ]
    row["detail_facts"] = {
        "subscription_window": "$14.00 of $20.00 subscription credits left",
        "subscription_credits": "Subscription credits: $14.00",
        "topup_credits": "Top-up credits: $12.00",
        "total_usable": "Total usable: $26.00",
        "rollover": "Rollover: $3.00",
        "cycle_ends": "Cycle ends: Dec 3, 2026",
        "spend": "Spend this period: $6.00",
        "rate_limits": "Rate limits: 50 RPM · 500k TPM · 2.1k RPH",
    }
    payload["age_s"] = age_s
    return payload


def _cli_master_no_age_payload():
    payload = _cli_master_payload()
    payload.pop("age_s", None)
    return payload


def _cli_master_balance_payload():
    """Cached row with the prominent balance but without the old detail line."""
    payload = _cli_master_payload()
    row = payload["providers"]["nous"]
    row["detail_facts"].pop("total_usable", None)
    row["account_balances"] = [
        {"currency": "USD", "total_balance": "26.00", "label": "Account balance"},
    ]
    return payload


def _cli_unknown_master_payload():
    """Cached master row whose entitlement state is unknown.

    Its rate limits are plan-specific claims; without a proven same-plan state
    they must not be merged into an active Billing row.
    """
    return _cli_master_payload(access_state="unknown")


def _cli_free_master_payload():
    payload = _cli_free_payload()
    row = payload["providers"]["nous"]
    row["details"] = [
        "Free tier - free models only",
        "Rate limits: 50 RPM · 500k TPM · 2.1k RPH",
        "Tool pool: browser-use, firecrawl",
    ]
    row["detail_facts"] = {
        "free_tier": "Free tier - free models only",
        "rate_limits": "Rate limits: 50 RPM · 500k TPM · 2.1k RPH",
        "tool_pool": "Tool pool: browser-use, firecrawl",
    }
    # FNV-1a digest of "synthetic-org", the fixture Billing slug. The pool
    # merge now requires this same-organisation proof, like rate limits.
    row["account_org_key"] = "10afe58765c15923"
    return payload


def _cli_unavailable_master_payload():
    payload = _cli_master_payload()
    row = payload["providers"]["nous"]
    row["unavailable_reason"] = "fetch-error"
    row["windows"] = []
    return payload


def _cli_bad_fact_payload():
    payload = _cli_master_payload()
    payload["providers"]["nous"]["detail_facts"] = {
        "rate_limits": {"text": "forged"},
        "total_usable": 26,
    }
    return payload


def _billing_queries(out):
    return [q for q in out["queries"] if q["key"][:2] == ["quota", "billing"]]


def _plans(tree):
    return [n["props"].get("title") for n in nodes(tree)
            if n.get("props", {}).get("data-card-plan")]


def _windows(tree):
    return [n["props"]["data-window"] for n in nodes(tree)
            if "data-window" in n.get("props", {})]


def _provider_subtree(tree, pid):
    for n in nodes(tree):
        if n.get("props", {}).get("data-provider") == pid:
            return n
    return None


def _card_providers(tree):
    return [n["props"]["data-card-provider"] for n in nodes(tree)
            if "data-card-provider" in n.get("props", {})]


def _provider_tones(tree, pid):
    card = _provider_subtree(tree, pid)
    return [] if card is None else [n["props"].get("data-tone") for n in nodes(card)]


def _card_status_node(tree, pid="nous"):
    for n in nodes(tree):
        if n.get("props", {}).get("data-card-status") == pid:
            return n
    raise AssertionError(f"no card status node for {pid}")


def _chip_button(tree, name="Nous Portal"):
    for n in nodes(tree):
        label = str(n.get("props", {}).get("aria-label", ""))
        if n.get("type") == "button" and label.startswith(name):
            return n
    raise AssertionError(f"no chip for {name}")


def _chip_dot_color(chip):
    for n in nodes(chip):
        if "data-sb-dot" in n.get("props", {}):
            return n["props"].get("style", {}).get("background")
    return None


def _prop_nodes(tree, name):
    return [n for n in nodes(tree) if name in n.get("props", {})]


def _run_probe() -> dict:
    runner = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const start = source.indexOf('const BILLING_QUERY_KEY =');
const end = source.indexOf('\nfunction useQuota()', start);
if (start < 0 || end < 0 || end <= start) {
  throw new Error("billing block not found in desktop/plugin.js");
}
const block = source.slice(start, end);
let unhandled = 0;
process.on("unhandledRejection", () => { unhandled += 1; });

const CREDIT = __CREDIT__;
const WINDOW = __WINDOW__;
const PLUS_WINDOW = __PLUS_WINDOW__;
const PLUS_CLI = __PLUS_CLI__;
const FREE = __FREE__;
const FREEPLAN = __FREEPLAN__;
const FREETOPUP = __FREETOPUP__;
const CLI = __CLI__;
const CLI_FREE = __CLI_FREE__;
const MEMBERCAP = __MEMBERCAP__;
const SIGNEDFREE = __SIGNEDFREE__;
const PAIDNOPROOF = __PAIDNOPROOF__;
const DEPLETEDSUB = __DEPLETEDSUB__;
const DEPLETEDNOBAR = __DEPLETEDNOBAR__;
const PAUSEDPAY = __PAUSEDPAY__;
const PAUSEDNOPAY = __PAUSEDNOPAY__;
const PAUSEDUNKNOWN = __PAUSEDUNKNOWN__;
const EXHAUSTED = __EXHAUSTED__;
const TOPTIEREXHAUSTED = __TOPTIEREXHAUSTED__;
const PAUSED_PAY_EMPTY = __PAUSED_PAY_EMPTY__;
const EXHAUSTED_PAY_EMPTY = __EXHAUSTED_PAY_EMPTY__;
const PAUSED_CLI = __PAUSED_CLI__;
const PAUSED_WALLET = __PAUSED_WALLET__;
const MASTER = __MASTER__;
const MASTER_FREE = __MASTER_FREE__;
const MASTER_UNAVAILABLE = __MASTER_UNAVAILABLE__;
const MASTER_NO_AGE = __MASTER_NO_AGE__;
const MASTER_BALANCE = __MASTER_BALANCE__;
const MASTER_UNKNOWN = __MASTER_UNKNOWN__;
const MASTER_BAD_FACT = __MASTER_BAD_FACT__;

function apiFor(host, useQuery) {
  const factory = new Function(
    "host",
    "useQuery",
    "setTimeout",
    "clearTimeout",
    `function quotaQueryKey(scope) { return ["quota", scope ? scope.connectionId : null, scope ? scope.profile : null]; }\n${block}\nreturn {BILLING_TIMEOUT_MS, billingQueryKey, billingStateToNousRow, mergeNousRow, fetchBillingState, fetchBillingSnapshot, resetBillingCadence, useNousBilling, invalidateQuotaSurfaces};`,
  );
  return factory(host, useQuery || (() => ({data: undefined})), setTimeout, clearTimeout);
}

function routedHost({routes = [{connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote"}], response = CREDIT, error = null, hang = false} = {}) {
  const calls = [];
  let release = null;
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (route, method, params, timeoutMs, options) => {
      calls.push({kind: "route", route, method, params, timeoutMs, options});
      if (error) throw error;
      if (hang) return new Promise((resolve, reject) => { release = (e) => e ? reject(e) : resolve(response); });
      return response;
    },
  };
  return {host, calls, release: () => release};
}

function liveHost({response = CREDIT, error = null, hang = false} = {}) {
  const calls = [];
  let release = null;
  return {
    calls,
    release: () => release,
    host: {
      request: async (method, params, timeoutMs) => {
        calls.push({kind: "live", method, params, timeoutMs});
        if (error) throw error;
        if (hang) return new Promise((resolve, reject) => { release = (e) => e ? reject(e) : resolve(response); });
        return response;
      },
    },
  };
}

function codedError(message, code) {
  return Object.assign(new Error(message), {code});
}

(async () => {
  const out = {};

  // Pure field mapping and merge.
  out.rowCredit = apiFor({}).billingStateToNousRow(CREDIT);
  out.rowCreditForeignTotal = apiFor({}).billingStateToNousRow({
    ...CREDIT,
    usage: {...CREDIT.usage, total_spendable_display: "€26.00"},
  });
  out.rowCreditGroupedTotal = apiFor({}).billingStateToNousRow({
    ...CREDIT,
    usage: {...CREDIT.usage, total_spendable_display: "$1,234.50"},
  });
  out.rowWindow = apiFor({}).billingStateToNousRow(WINDOW);
  out.rowWindowUnavailable = apiFor({}).billingStateToNousRow({
    ...WINDOW,
    usage: {ok: true, available: false},
  });
  out.rowWindowPaused = apiFor({}).billingStateToNousRow({
    ...WINDOW,
    usage: {ok: true, available: false},
    plan_usage: {...WINDOW.plan_usage, paused_reason: "payment_past_due"},
  });
  out.rowPlusWindow = apiFor({}).billingStateToNousRow(PLUS_WINDOW);
  out.rowCreditWalletIgnored = apiFor({}).billingStateToNousRow({
    ...CREDIT,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: "Pro",
      plan_bar: CREDIT.usage.plan_bar,
    },
  });
  out.rowFree = apiFor({}).billingStateToNousRow(FREE);
  out.rowFreePlan = apiFor({}).billingStateToNousRow(FREEPLAN);
  out.rowFreeTopup = apiFor({}).billingStateToNousRow(FREETOPUP);
  out.rowFreeTopupNoPlanName = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: null,
      has_active_subscription: false,
      renews_display: "Jan 1, 2027",
      subscription_remaining_display: "$0.00",
      topup_remaining_display: "$10.00",
      total_spendable_display: "$10.00",
      has_topup: true,
      topup_bar: {
        kind: "topup", remaining_display: "$10.00",
        total_display: "$10.00", spent_display: "$0.00",
        pct_used: null, fill_fraction: 1.0,
      },
    },
    plan_usage: null,
  });
  out.rowFreeTopupNoPlanNameNoFlag = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: null,
      renews_display: "Jan 1, 2027",
      subscription_remaining_display: "$0.00",
      topup_remaining_display: "$10.00",
      total_spendable_display: "$10.00",
      has_topup: true,
      topup_bar: {
        kind: "topup", remaining_display: "$10.00",
        total_display: "$10.00", spent_display: "$0.00",
        pct_used: null, fill_fraction: 1.0,
      },
    },
    plan_usage: null,
  });
  out.rowFreeTopupNoPlanNameTopLevel = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    has_active_subscription: false,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: null,
      renews_display: "Jan 1, 2027",
      subscription_remaining_display: "$0.00",
      topup_remaining_display: "$10.00",
      total_spendable_display: "$10.00",
      has_topup: true,
      topup_bar: {
        kind: "topup", remaining_display: "$10.00",
        total_display: "$10.00", spent_display: "$0.00",
        pct_used: null, fill_fraction: 1.0,
      },
    },
    plan_usage: null,
  });
  out.rowFreeTopupZero = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: "Free",
      renews_display: "Jan 1, 2027",
      topup_remaining_display: "$0.00",
      total_spendable_display: "$0.00",
      has_topup: true,
      topup_bar: {
        kind: "topup", remaining_display: "$0.00",
        total_display: "$0.00", spent_display: "$0.00",
        pct_used: null, fill_fraction: 0,
      },
    },
    plan_usage: null,
  });
  out.rowFreeTopupNoTotal = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: "Free",
      topup_remaining_display: "$10.00",
      topup_bar: {
        kind: "topup", remaining_display: "$10.00",
        total_display: "$10.00", spent_display: "$0.00",
        pct_used: null, fill_fraction: 1.0,
      },
    },
    plan_usage: null,
  });
  out.rowFreeTopupOnlyTotal = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: "Free",
      subscription_remaining_display: "$0.00",
      topup_remaining_display: "$10.00",
      has_topup: true,
      topup_bar: {
        kind: "topup", remaining_display: "$10.00",
        total_display: "$10.00", spent_display: "$0.00",
        pct_used: null, fill_fraction: 1.0,
      },
    },
    plan_usage: null,
  });
  out.rowFreeTopupUnknownSub = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: "Free",
      topup_remaining_display: "$10.00",
      has_topup: true,
    },
    plan_usage: null,
  });
  out.rowFreeFundedNoTopup = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: "Free",
      total_spendable_display: "$5.00",
    },
    plan_usage: null,
  });
  out.rowStatusFree = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true,
    usage: {ok: true, available: true, status: "free"},
  });
  out.rowLoggedOut = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: false, free_tier_account: false,
    usage: {available: false}, card: {brand: "Visa", last4: "9999"},
  });
  out.rowUnsupported = apiFor({}).billingStateToNousRow({ok: false, error: "nope"});
  out.rowFreeNameOnly = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {available: true, plan_name: "Free"},
    plan_usage: {kind: "window", plan: "Pro", gauges: []},
  });
  out.rowDepleted = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true,
    usage: {
      ok: true, available: true, status: "depleted", plan_name: "Pro",
      plan_bar: {
        kind: "plan", remaining_display: "$0.00", total_display: "$20.00",
        spent_display: "$20.00", pct_used: 100, fill_fraction: 0,
      },
    },
  });
  out.rowDepletedBare = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true,
    usage: {ok: true, available: true, status: "depleted"},
  });
  out.rowDepletedSubscription = apiFor({}).billingStateToNousRow(DEPLETEDSUB);
  out.rowDepletedSubscriptionNoBar = apiFor({}).billingStateToNousRow(DEPLETEDNOBAR);
  out.rowMemberCap = apiFor({}).billingStateToNousRow(MEMBERCAP);
  out.rowSignedInFree = apiFor({}).billingStateToNousRow(SIGNEDFREE);
  out.rowPaidNoBalance = apiFor({}).billingStateToNousRow(PAIDNOPROOF);
  out.rowPausedPay = apiFor({}).billingStateToNousRow(PAUSEDPAY);
  out.rowPausedNoPay = apiFor({}).billingStateToNousRow(PAUSEDNOPAY);
  out.rowPausedUnknown = apiFor({}).billingStateToNousRow(PAUSEDUNKNOWN);
  out.rowActiveExhaustedNoPay = apiFor({}).billingStateToNousRow(EXHAUSTED);
  out.rowTopTierExhaustedNoPay = apiFor({}).billingStateToNousRow(TOPTIEREXHAUSTED);
  out.rowPausedPayEmptyWallet = apiFor({}).billingStateToNousRow(PAUSED_PAY_EMPTY);
  out.rowActiveExhaustedPayEmptyWallet = apiFor({}).billingStateToNousRow(EXHAUSTED_PAY_EMPTY);
  out.rowLow = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true,
    usage: {
      ok: true, available: true, status: "low", plan_name: "Plus",
      total_spendable_display: "$3.40",
    },
  });
  out.rowLowBare = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: true,
    usage: {
      ok: true, available: true, status: "low",
      topup_bar: {
        kind: "topup", remaining_display: "$4.00",
        total_display: "$4.00", spent_display: "$0.00",
        pct_used: null, fill_fraction: 1.0,
      },
    },
  });
  out.rowLoggedOutError = apiFor({}).billingStateToNousRow({
    ok: true, logged_in: false, free_tier_account: false,
    error: "could not load billing state",
    usage: {available: false},
  });

  const merged = apiFor({}).mergeNousRow(CLI, CREDIT);
  out.mergedCredit = merged;
  out.mergedWindow = apiFor({}).mergeNousRow(CLI, WINDOW);
  out.mergedPlusWindow = apiFor({}).mergeNousRow(PLUS_CLI, PLUS_WINDOW, 20, 1800);
  out.mergedPlusWindowNoMaster = apiFor({}).mergeNousRow(
    {age_s: 20, providers: {anthropic: {windows: []}}},
    PLUS_WINDOW, 20, 1800,
  );
  const plusZeroSubscription = JSON.parse(JSON.stringify(PLUS_CLI));
  plusZeroSubscription.providers.nous.detail_facts.subscription_credits =
    "Subscription credits: $0.00";
  out.mergedPlusWindowZeroSubscription = apiFor({}).mergeNousRow(
    plusZeroSubscription, PLUS_WINDOW, 20, 1800,
  );
  const plusPositiveSubscription = JSON.parse(JSON.stringify(PLUS_CLI));
  plusPositiveSubscription.providers.nous.detail_facts.subscription_credits =
    "Subscription credits: $5.00";
  out.mergedPlusWindowPositiveSubscription = apiFor({}).mergeNousRow(
    plusPositiveSubscription, PLUS_WINDOW, 20, 1800,
  );
  const plusCacheTotal = JSON.parse(JSON.stringify(PLUS_CLI));
  plusCacheTotal.providers.nous.account_balances = [
    { currency: "USD", total_balance: "14.99", label: "Account balance" },
  ];
  out.mergedPlusWindowCacheTotal = apiFor({}).mergeNousRow(
    plusCacheTotal, PLUS_WINDOW, 20, 1800,
  );
  out.mergedPausedCacheTotal = apiFor({}).mergeNousRow(
    PAUSED_CLI, PAUSED_WALLET, 20, 1800,
  );
  out.mergedPlusWindowStale = apiFor({}).mergeNousRow(PLUS_CLI, PLUS_WINDOW, 20, 10);
  const plusWrongPlan = {
    ...PLUS_WINDOW,
    plan_usage: {...PLUS_WINDOW.plan_usage, plan: "Hermes Super"},
  };
  out.mergedPlusWindowWrongPlan = apiFor({}).mergeNousRow(PLUS_CLI, plusWrongPlan, 20, 1800);
  const plusWrongOrg = {...PLUS_WINDOW, org_slug: "org_other"};
  out.mergedPlusWindowWrongOrg = apiFor({}).mergeNousRow(PLUS_CLI, plusWrongOrg, 20, 1800);
  const plusWrongState = JSON.parse(JSON.stringify(PLUS_CLI));
  plusWrongState.providers.nous.access_state = "unknown";
  out.mergedPlusWindowWrongState = apiFor({}).mergeNousRow(
    plusWrongState, PLUS_WINDOW, 20, 1800,
  );
  const plusWithCreditsModel = {
    ...PLUS_WINDOW,
    usage: {
      ok: true, available: true, status: "healthy", plan_name: "Hermes Plus",
      total_spendable_display: "$4.00",
    },
  };
  out.mergedPlusWindowBillingMoney = apiFor({}).mergeNousRow(
    PLUS_CLI, plusWithCreditsModel, 20, 1800,
  );
  out.mergedFree = apiFor({}).mergeNousRow(CLI, FREE);
  out.mergedFreePlan = apiFor({}).mergeNousRow(CLI, FREEPLAN);
  const mergedStatusFree = apiFor({}).mergeNousRow(CLI, {
    ok: true, logged_in: true,
    usage: {available: true, status: "free"},
  });
  out.mergedStatusFreeUnchanged = mergedStatusFree === CLI;
  const mergedUnavailable = apiFor({}).mergeNousRow(CLI, {
    ok: true, logged_in: true,
    usage: {available: false, status: "unavailable"},
  });
  out.mergedUnavailableUnchanged = mergedUnavailable === CLI;
  out.mergedNoProvider = apiFor({}).mergeNousRow(
    {age_s: 1, providers: {anthropic: {windows: []}}}, CREDIT,
  );
  out.mergedBareDepleted = apiFor({}).mergeNousRow(CLI, {
    ok: true, logged_in: true,
    usage: {ok: true, available: true, status: "depleted"},
  });
  const mergedMemberCap = apiFor({}).mergeNousRow(CLI, MEMBERCAP);
  out.mergedMemberCap = mergedMemberCap;
  out.mergedMemberCapUnchanged = mergedMemberCap === CLI;
  const mergedSignedInFree = apiFor({}).mergeNousRow(CLI_FREE, SIGNEDFREE);
  out.mergedSignedInFree = mergedSignedInFree;
  out.mergedSignedInFreeUnchanged = mergedSignedInFree === CLI_FREE;
  const mergedPaidNoBalance = apiFor({}).mergeNousRow(CLI, PAIDNOPROOF);
  out.mergedPaidNoBalance = mergedPaidNoBalance;
  out.mergedPaidNoBalanceUnchanged = mergedPaidNoBalance === CLI;
  out.mergedDepletedSubscription = apiFor({}).mergeNousRow(CLI, DEPLETEDSUB);
  // Billing owns plan/status/windows and every financial fact. The cached
  // row contributes only the non-financial Free tool pool, and only when both
  // sources are free and the same-profile cache is fresh.
  const ORG_KEYS = {
    org_synthetic: "1523bbad0010557d", // FNV-1a 64 of "org_synthetic"
    org_other: "6112182168775f70", // FNV-1a 64 of "org_other"
    secret_org: "8c3d40ad051e04ea", // FNV-1a 64 of "secret-org"
  };
  const withOrg = (master, org) => {
    const clone = JSON.parse(JSON.stringify(master));
    clone.providers.nous.account_org_key = ORG_KEYS[org];
    return clone;
  };
  const withBillingOrg = (billing, org) => ({...billing, org_slug: org});
  out.mergedMasterFreeWithOrg = apiFor({}).mergeNousRow(
    withOrg(MASTER_FREE, "org_synthetic"),
    withBillingOrg(FREE, "org_synthetic"), 5,
  );
  const malformedRateMaster = withOrg(MASTER_FREE, "org_synthetic");
  malformedRateMaster.providers.nous.detail_facts.rate_limits =
    "Rate limits: 50 RPM · 500k TPM (forged)";
  out.mergedMalformedRateWithOrg = apiFor({}).mergeNousRow(
    malformedRateMaster,
    withBillingOrg(FREE, "org_synthetic"), 5,
  );
  out.mergedMasterFreeOrgMismatch = apiFor({}).mergeNousRow(
    withOrg(MASTER_FREE, "org_other"),
    withBillingOrg(FREE, "org_synthetic"), 5,
  );
  out.mergedMasterFreeWithOrgStale = apiFor({}).mergeNousRow(
    withOrg(MASTER_FREE, "org_synthetic"),
    withBillingOrg(FREE, "org_synthetic"), 5, 10,
  );
  const paidMaster = JSON.parse(JSON.stringify(MASTER));
  paidMaster.providers.nous.plan = "Plus";
  paidMaster.providers.nous.access_state = "active";
  paidMaster.providers.nous.detail_facts.rate_limits = "Rate limits: 400 RPM · 4M TPM · 16.8k RPH";
  paidMaster.providers.nous.account_org_key = ORG_KEYS.org_synthetic;
  const paidBilling = {
    ok: true, logged_in: true, free_tier_account: false,
    usage: {ok: true, available: true, status: "healthy", plan_name: "Plus"},
    plan_usage: null,
    org_slug: "org_synthetic",
  };
  out.mergedPaidActualRatesWithOrg = apiFor({}).mergeNousRow(paidMaster, paidBilling, 5);
  // Same exact plan label and organisation, but the cached row's entitlement
  // state is unknown. It must not decorate an active Billing row: state is
  // per-tier evidence, not a generic free/paid side.
  const unknownSamePlan = JSON.parse(JSON.stringify(MASTER));
  unknownSamePlan.providers.nous.plan = "Plus";
  unknownSamePlan.providers.nous.access_state = "unknown";
  unknownSamePlan.providers.nous.detail_facts.rate_limits = "Rate limits: 400 RPM · 4M TPM · 16.8k RPH";
  unknownSamePlan.providers.nous.account_org_key = ORG_KEYS.org_synthetic;
  out.mergedUnknownSamePlanIntoActive = apiFor({}).mergeNousRow(
    unknownSamePlan, paidBilling, 5,
  );
  // A low balance maps to `unknown` access, but the exact paid plan label
  // still selects its documented published ceiling.
  out.mergedLowPaidPublished = apiFor({}).mergeNousRow(
    {age_s: 1, providers: {}}, out.rowLow, 5,
  );
  const published = (plan) => ({
    ok: true, logged_in: true, free_tier_account: false,
    usage: {ok: true, available: true, status: "healthy", plan_name: plan},
    plan_usage: null,
  });
  out.publishedCeilings = {};
  for (const plan of ["Free", "Basic", "Plus", "Hermes Plus", "Super", "Scale", "Ultra", "Default paid users", "Pro"]) {
    const merged = apiFor({}).mergeNousRow(
      {age_s: 1, providers: {}}, published(plan), 5,
    );
    out.publishedCeilings[plan] = merged.providers.nous.detail_facts.rate_ceiling || null;
  }
  out.mergedMasterActive = apiFor({}).mergeNousRow(MASTER, CREDIT, 5);
  out.mergedMasterUnknownIntoActive = apiFor({}).mergeNousRow(MASTER_UNKNOWN, CREDIT, 5);
  out.mergedMasterFree = apiFor({}).mergeNousRow(MASTER_FREE, FREE, 5);
  out.mergedMasterFreePlan = apiFor({}).mergeNousRow(MASTER_FREE, FREEPLAN, 5);
  const noPoolMaster = JSON.parse(JSON.stringify(MASTER_FREE));
  noPoolMaster.providers.nous.details = ["Free tier - free models only"];
  delete noPoolMaster.providers.nous.detail_facts.tool_pool;
  out.mergedToolPoolDisabled = apiFor({}).mergeNousRow(noPoolMaster, FREE, 5);
  out.mergedToolPoolStale = apiFor({}).mergeNousRow(MASTER_FREE, FREE, 5, 10);
  out.mergedToolPoolPaid = apiFor({}).mergeNousRow(MASTER_FREE, CREDIT, 5);
  out.mergedToolPoolUnavailable = apiFor({}).mergeNousRow(MASTER_UNAVAILABLE, FREE, 5);
  out.mergedToolPoolUnknown = apiFor({}).mergeNousRow(MASTER_UNKNOWN, FREE, 5);
  const noOrgPoolMaster = JSON.parse(JSON.stringify(MASTER_FREE));
  delete noOrgPoolMaster.providers.nous.account_org_key;
  out.mergedToolPoolNoOrg = apiFor({}).mergeNousRow(noOrgPoolMaster, FREE, 5);
  out.mergedToolPoolOrgMismatch = apiFor({}).mergeNousRow(
    withOrg(MASTER_FREE, "org_other"), FREE, 5,
  );
  const controlPoolMaster = JSON.parse(JSON.stringify(MASTER_FREE));
  controlPoolMaster.providers.nous.detail_facts.tool_pool = "Tool pool: browser-use\x1b[31m";
  out.mergedToolPoolControl = apiFor({}).mergeNousRow(controlPoolMaster, FREE, 5);
  const duplicateBilling = {
    label: "nous", plan: "Free", access_state: "free",
    details: ["Tool pool: browser-use, firecrawl"],
    windows: [], account_balances: [],
  };
  out.mergedToolPoolDuplicate = apiFor({}).mergeNousRow(MASTER_FREE, duplicateBilling, 5);
  out.mergedMasterIntoFree = apiFor({}).mergeNousRow(MASTER, FREE, 5);
  out.mergedMasterIntoFreeTopup = apiFor({}).mergeNousRow(MASTER, FREETOPUP, 5);
  out.mergedMasterFreeIntoActive = apiFor({}).mergeNousRow(MASTER_FREE, CREDIT, 5);
  out.mergedMasterIntoDepleted = apiFor({}).mergeNousRow(MASTER, DEPLETEDSUB, 5);
  out.mergedMasterIntoLowBare = apiFor({}).mergeNousRow(MASTER, {
    ok: true, logged_in: true,
    usage: {
      ok: true, available: true, status: "low",
      topup_bar: {
        kind: "topup", remaining_display: "$4.00",
        total_display: "$4.00", spent_display: "$0.00",
        pct_used: null, fill_fraction: 1.0,
      },
    },
  }, 5);
  out.mergedMasterUnavailable = apiFor({}).mergeNousRow(MASTER_UNAVAILABLE, CREDIT, 5);
  out.mergedMasterNoAge = apiFor({}).mergeNousRow(MASTER_NO_AGE, CREDIT, 5);
  out.mergedMasterTooOld = apiFor({}).mergeNousRow(MASTER, CREDIT, 5, 10);
  // A Billing read that omits the total field must not erase the cached
  // account's prominent amount when that source is fresh and same-plan.
  const creditWithoutTotal = {
    ...CREDIT,
    usage: {...CREDIT.usage, total_spendable_display: null},
  };
  out.mergedMasterBalanceFallback = apiFor({}).mergeNousRow(
    MASTER_BALANCE, creditWithoutTotal, 5,
  );
  const staleMasterBalance = {
    ...MASTER_BALANCE,
    providers: {
      ...MASTER_BALANCE.providers,
      nous: {
        ...MASTER_BALANCE.providers.nous,
        account_balances: [
          {currency: "USD", total_balance: "99.00", label: "Total usable"},
        ],
      },
    },
  };
  out.mergedMasterBalanceOverridden = apiFor({}).mergeNousRow(
    staleMasterBalance, CREDIT, 5,
  );
  out.mergedMasterBalanceIntoFree = apiFor({}).mergeNousRow(
    MASTER_BALANCE, FREE, 5,
  );
  out.mergedMasterBadFact = apiFor({}).mergeNousRow(MASTER_BAD_FACT, CREDIT, 5);
  const mergedBothFail = apiFor({}).mergeNousRow(MASTER_UNAVAILABLE, null, 5);
  out.mergedBothFailSame = mergedBothFail === MASTER_UNAVAILABLE;
  out.mergedBothFail = mergedBothFail;
  out.mergedMasterNoCap = apiFor({}).mergeNousRow(
    MASTER, {...CREDIT, monthly_cap: null}, 5,
  );
  // A mixed payload reports the oldest contributing age, never the cached
  // age masquerading for a fresh billing read (or the reverse).
  out.mergedAgeOlderBilling = apiFor({}).mergeNousRow(CLI, CREDIT, 120);
  out.mergedAgeFreshBilling = apiFor({}).mergeNousRow(CLI, CREDIT, 5);
  out.mergedAgeNoBillingAge = apiFor({}).mergeNousRow(CLI, CREDIT);
  out.mergedAgeMissingCache = apiFor({}).mergeNousRow(
    {providers: {nous: CLI.providers.nous}}, CREDIT, 42,
  );
  const mergedLoggedOut = apiFor({}).mergeNousRow(CLI, {
    ok: true, logged_in: false, free_tier_account: false,
    usage: {available: false},
  });
  out.mergedLoggedOutUnchanged = mergedLoggedOut === CLI;

  // Exact registry route.
  const exact = routedHost();
  out.routeExact = {
    result: await apiFor(exact.host).fetchBillingState({connectionId: "remote-a", profile: "shared"}),
    calls: exact.calls,
  };

  // A route the plugin cannot name safely: backend profile differs.
  const mismatch = routedHost({routes: [
    {connectionId: "remote-b", profile: "shared", targetProfile: "backend-shared", mode: "remote"},
  ]});
  out.routeMismatch = {
    result: await apiFor(mismatch.host).fetchBillingState({connectionId: "remote-b", profile: "shared"}),
    calls: mismatch.calls,
  };

  // Ambiguous / missing routes: no billing call at all.
  const ambiguous = routedHost({routes: [
    {connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote"},
    {connectionId: "remote-a", profile: "shared", targetProfile: "other", mode: "remote"},
  ]});
  out.routeAmbiguous = {
    result: await apiFor(ambiguous.host).fetchBillingState({connectionId: "remote-a", profile: "shared"}),
    calls: ambiguous.calls,
  };
  const missing = routedHost({routes: []});
  out.routeMissing = {
    result: await apiFor(missing.host).fetchBillingState({connectionId: "remote-a", profile: "shared"}),
    calls: missing.calls,
  };
  const noOwner = routedHost();
  out.noOwner = {
    result: await apiFor(noOwner.host).fetchBillingState(null),
    calls: noOwner.calls,
  };

  // Legacy single connection names the profile explicitly.
  const legacy = liveHost();
  out.legacy = {
    result: await apiFor(legacy.host).fetchBillingState(
      {connectionId: "", profile: "default", live: true}, 4000,
    ),
    calls: legacy.calls,
  };
  const noLiveDoor = apiFor({});
  out.noLiveDoor = await noLiveDoor.fetchBillingState(
    {connectionId: "", profile: "default", live: true}, 50,
  );

  // Older gateway / unknown profile / malformed answer.
  const oldGateway = routedHost({error: codedError("Method not found", -32601)});
  out.oldGateway = await apiFor(oldGateway.host).fetchBillingState(
    {connectionId: "remote-a", profile: "shared"},
  );
  const unknownProfile = routedHost({error: codedError("Unknown profile", 4064)});
  out.unknownProfile = await apiFor(unknownProfile.host).fetchBillingState(
    {connectionId: "remote-a", profile: "shared"},
  );
  const malformed = routedHost({response: ["not", "an", "object"]});
  out.malformed = await apiFor(malformed.host).fetchBillingState(
    {connectionId: "remote-a", profile: "shared"},
  );

  // A hung gateway is bounded by BILLING_TIMEOUT_MS, and a late rejection
  // must not escape as an unhandled rejection.
  const hung = routedHost({hang: true});
  const before = Date.now();
  out.timeoutResult = await apiFor(hung.host).fetchBillingState(
    {connectionId: "remote-a", profile: "shared"}, 25,
  );
  out.timeoutMs = Date.now() - before;
  if (hung.release()) hung.release()(new Error("late socket drop"));
  await new Promise((resolve) => setImmediate(resolve));

  // A timed-out request stays deduped until its underlying socket settles:
  // a new cadence must not stack a second portal read on the abandoned one.
  const overlap = routedHost({hang: true});
  const overlapApi = apiFor(overlap.host);
  out.overlapFirst = await overlapApi.fetchBillingState(
    {connectionId: "remote-a", profile: "shared"}, 25,
  );
  const firstRelease = overlap.release();
  const secondRead = overlapApi.fetchBillingState(
    {connectionId: "remote-a", profile: "shared"}, 25,
  );
  out.overlapSecond = await secondRead;
  out.overlapCallsBeforeIdle = overlap.calls.length;
  firstRelease();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const thirdRead = overlapApi.fetchBillingState(
    {connectionId: "remote-a", profile: "shared"}, 25,
  );
  await new Promise((resolve) => setImmediate(resolve));
  out.overlapCallsAfterIdle = overlap.calls.length;
  if (overlap.release()) overlap.release()(new Error("late overlap drop"));
  out.overlapThird = await thirdRead;

  // A route discovery that never answers must not pin the scope forever.
  let routeHangCalls = 0;
  const routeHangHost = {
    profileRoutes: () => {
      routeHangCalls += 1;
      return routeHangCalls === 1
        ? new Promise(() => {})
        : Promise.resolve([{connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote"}]);
    },
    requestProfile: async () => CREDIT,
  };
  const routeHangApi = apiFor(routeHangHost);
  const routeHangScope = {connectionId: "remote-a", profile: "shared"};
  out.routeHangFirst = await routeHangApi.fetchBillingState(routeHangScope, 25);
  await new Promise((resolve) => setTimeout(resolve, 100));
  out.routeHangSecond = await routeHangApi.fetchBillingState(routeHangScope, 25);
  out.routeHangCalls = routeHangCalls;

  // A gateway call that never settles must also release the dedupe entry at
  // the capped idle deadline so a later cadence can retry.
  let neverSettleCalls = 0;
  const neverSettleHost = {
    profileRoutes: async () => [{connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote"}],
    requestProfile: async () => {
      neverSettleCalls += 1;
      return new Promise(() => {});
    },
  };
  const neverSettleApi = apiFor(neverSettleHost);
  out.neverFirst = await neverSettleApi.fetchBillingState(routeHangScope, 25);
  await new Promise((resolve) => setTimeout(resolve, 100));
  out.neverSecond = await neverSettleApi.fetchBillingState(routeHangScope, 25);
  out.neverCalls = neverSettleCalls;

  // Staggered observers share one cadence window: a second timer replaying
  // the same scope must not open a second portal read, and must keep the
  // original observation time. An explicit refresh clears the window.
  const cadence = routedHost();
  const cadenceApi = apiFor(cadence.host);
  const cadenceScope = {connectionId: "remote-a", profile: "shared"};
  const firstSnapshot = await cadenceApi.fetchBillingSnapshot(cadenceScope, 50, 60000);
  const secondSnapshot = await cadenceApi.fetchBillingSnapshot(cadenceScope, 50, 60000);
  out.cadence = {
    calls: cadence.calls.length,
    sameRow: firstSnapshot.row === secondSnapshot.row,
    sameObservedAt: firstSnapshot.observedAt === secondSnapshot.observedAt,
    serialized: JSON.stringify(secondSnapshot),
  };
  cadenceApi.resetBillingCadence(cadenceScope);
  await cadenceApi.fetchBillingSnapshot(cadenceScope, 50, 60000);
  out.cadence.afterReset = cadence.calls.length;

  // An explicit refresh repaints both sources, not just the CLI cache.
  const invalidated = [];
  apiFor({}).invalidateQuotaSurfaces(
    {invalidateQueries: (arg) => invalidated.push(arg.queryKey)},
    {connectionId: "remote-a", profile: "shared"},
  );
  out.invalidated = invalidated;

  // Concurrent reads for one scope share one gateway call; a later cadence
  // starts a new one.
  let resolveOnce = null;
  let dedupeCalls = 0;
  const dedupeHost = {
    profileRoutes: async () => [{connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote"}],
    requestProfile: async () => {
      dedupeCalls += 1;
      return new Promise((resolve) => { resolveOnce = () => resolve(CREDIT); });
    },
  };
  const dedupeApi = apiFor(dedupeHost);
  const p1 = dedupeApi.fetchBillingState({connectionId: "remote-a", profile: "shared"});
  const p2 = dedupeApi.fetchBillingState({connectionId: "remote-a", profile: "shared"});
  await new Promise((resolve) => setImmediate(resolve));
  out.dedupe = {calls: dedupeCalls, samePromise: p1 === p2};
  resolveOnce();
  const [d1, d2] = await Promise.all([p1, p2]);
  out.dedupe.sameValue = d1 === d2;
  const p3 = dedupeApi.fetchBillingState({connectionId: "remote-a", profile: "shared"});
  await new Promise((resolve) => setImmediate(resolve));
  resolveOnce();
  await p3;
  out.dedupe.afterSettled = dedupeCalls;

  // A profile switch is a different key and a different read.
  let switchCalls = 0;
  const switchRoutes = [];
  const switchHost = {
    profileRoutes: async () => [
      {connectionId: "remote-a", profile: "shared", targetProfile: "shared", mode: "remote"},
      {connectionId: "remote-a", profile: "home", targetProfile: "home", mode: "remote"},
    ],
    requestProfile: async (route) => { switchCalls += 1; switchRoutes.push(route.profile); return CREDIT; },
  };
  const switchApi = apiFor(switchHost);
  await Promise.all([
    switchApi.fetchBillingState({connectionId: "remote-a", profile: "shared"}),
    switchApi.fetchBillingState({connectionId: "remote-a", profile: "home"}),
  ]);
  out.profileSwitchCalls = switchCalls;
  out.profileSwitchRoutes = switchRoutes;

  // Query keys and hook options.
  const scopeA = {connectionId: "remote-a", profile: "shared"};
  const scopeB = {connectionId: "remote-b", profile: "shared"};
  out.keys = {
    a: apiFor({}).billingQueryKey(scopeA),
    b: apiFor({}).billingQueryKey(scopeB),
    none: apiFor({}).billingQueryKey(null),
  };
  const recorded = [];
  const hookApi = apiFor({}, (options) => {
    recorded.push({
      key: options.queryKey,
      enabled: options.enabled,
      retry: options.retry,
      refetchInterval: options.refetchInterval,
      refetchOnWindowFocus: options.refetchOnWindowFocus,
      staleTime: options.staleTime,
      structuralSharing: options.structuralSharing,
      hasPlaceholder: Object.prototype.hasOwnProperty.call(options, "placeholderData"),
      queryFn: typeof options.queryFn,
    });
    return {data: {marker: "billing"}};
  });
  out.hook = {
    data: hookApi.useNousBilling(scopeA, 600000).data,
    noScopeDataUndefined: hookApi.useNousBilling(null, 600000).data === undefined,
    options: recorded,
  };
  let hookQueryFn = null;
  apiFor(routedHost().host, (options) => {
    hookQueryFn = options.queryFn;
    return {data: undefined};
  }).useNousBilling(scopeA, 600000);
  apiFor({}).resetBillingCadence(scopeA);
  out.hookQueryRow = await hookQueryFn();
  out.mergedSanitizedRow = apiFor({}).mergeNousRow(CLI, out.hookQueryRow);
  out.mergedMasterSanitized = apiFor({}).mergeNousRow(MASTER, out.hookQueryRow, 5);
  // The query hook spreads the sanitized row to add `billing_observed_at`.
  // That copy must still carry the non-serialized organisation association,
  // or a legitimate same-org/same-plan live rate line would silently vanish.
  const hookRateMaster = JSON.parse(JSON.stringify(CLI));
  hookRateMaster.providers.nous.plan = "Pro";
  hookRateMaster.providers.nous.access_state = "active";
  hookRateMaster.providers.nous.account_org_key = ORG_KEYS.secret_org;
  hookRateMaster.providers.nous.detail_facts = {
    rate_limits: "Rate limits: 50 RPM · 500k TPM",
  };
  out.mergedHookLiveRate = apiFor({}).mergeNousRow(
    hookRateMaster, out.hookQueryRow, 5,
  );

  out.unhandled = unhandled;
  process.stdout.write(JSON.stringify(out));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""
    runner = (runner
              .replace("__CREDIT__", json.dumps(_credit_billing()))
              .replace("__WINDOW__", json.dumps(_window_billing()))
              .replace("__PLUS_WINDOW__", json.dumps(_plus_window_billing()))
              .replace("__PLUS_CLI__", json.dumps(_cli_plus_window_payload()))
              .replace("__FREE__", json.dumps(_free_billing()))
              .replace("__FREEPLAN__", json.dumps(_free_plan_billing()))
              .replace("__FREETOPUP__", json.dumps(_free_topup_billing()))
              .replace("__CLI__", json.dumps(_cli_payload()))
              .replace("__CLI_FREE__", json.dumps(_cli_free_payload()))
              .replace("__MEMBERCAP__", json.dumps(_member_cap_billing()))
              .replace("__SIGNEDFREE__", json.dumps(_signed_in_free_billing()))
              .replace("__PAIDNOPROOF__", json.dumps(_paid_no_balance_billing()))
              .replace("__DEPLETEDSUB__", json.dumps(_depleted_subscription_billing()))
              .replace("__DEPLETEDNOBAR__", json.dumps(_depleted_subscription_no_bar_billing()))
              .replace("__PAUSEDPAY__", json.dumps(_paused_credits_billing(True)))
              .replace("__PAUSEDNOPAY__", json.dumps(_paused_credits_billing(False)))
              .replace("__PAUSEDUNKNOWN__", json.dumps(_paused_credits_billing(None)))
              .replace("__EXHAUSTED__", json.dumps(_exhausted_window_billing()))
              .replace("__TOPTIEREXHAUSTED__", json.dumps(_top_tier_exhausted_window_billing()))
              .replace("__PAUSED_PAY_EMPTY__", json.dumps(_paused_pay_empty_wallet_billing()))
              .replace("__EXHAUSTED_PAY_EMPTY__", json.dumps(_exhausted_pay_empty_wallet_billing()))
              .replace("__PAUSED_CLI__", json.dumps(_cli_paused_plus_payload()))
              .replace("__PAUSED_WALLET__", json.dumps(_paused_wallet_zero_billing()))
              .replace("__MASTER__", json.dumps(_cli_master_payload()))
              .replace("__MASTER_FREE__", json.dumps(_cli_free_master_payload()))
              .replace("__MASTER_UNAVAILABLE__", json.dumps(_cli_unavailable_master_payload()))
              .replace("__MASTER_NO_AGE__", json.dumps(_cli_master_no_age_payload()))
              .replace("__MASTER_BALANCE__", json.dumps(_cli_master_balance_payload()))
              .replace("__MASTER_UNKNOWN__", json.dumps(_cli_unknown_master_payload()))
              .replace("__MASTER_BAD_FACT__", json.dumps(_cli_bad_fact_payload())))
    completed = subprocess.run(
        ["node", "-e", runner, str(PLUGIN)],
        check=False, capture_output=True, text=True, timeout=20,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "billing probe failed: exit=%s stderr=%s"
            % (completed.returncode, completed.stderr.strip())
        )
    return json.loads(completed.stdout)


class BillingMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.r = _run_probe()

    def test_credits_plan_maps_plan_bar_and_account_balance(self) -> None:
        row = self.r["rowCredit"]
        self.assertEqual("Pro", row["plan"])
        self.assertEqual("active", row["access_state"])
        self.assertEqual(["Subscription"], [w["label"] for w in row["windows"]])
        self.assertEqual(30, row["windows"][0]["used_percent"])
        self.assertIn("Cycle ends: Dec 3, 2026", row["details"])
        self.assertIn("Monthly cap: $6.00 of $200.00", row["details"])
        # One Account balance is the money; the separate subscription/top-up
        # component lines are intentionally not rendered.
        self.assertNotIn("$14.00 of $20.00 subscription credits left", row["details"])
        self.assertNotIn("$12.00 of $12.00 top-up credits left", row["details"])
        # The portal's own total is a prominent, explicitly labeled balance;
        # it is never rebuilt from the component bars.
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "26.00", "Account balance")],
        )
        self.assertNotIn("Account balance", "\n".join(row["details"]))
        self.assertIsNone(row["api_calls_available"])

    def test_account_balance_requires_a_canonical_usd_amount(self) -> None:
        # A foreign-currency display is not silently relabeled as USD.
        foreign = self.r["rowCreditForeignTotal"]
        self.assertEqual([], foreign["account_balances"])
        # Grouping is accepted while preserving the exact reported decimals.
        grouped = self.r["rowCreditGroupedTotal"]
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in grouped["account_balances"]],
            [("USD", "1234.50", "Account balance")],
        )

    def test_credit_plan_wallet_field_is_not_an_account_balance(self) -> None:
        """The raw top-level wallet is read only for a usage-window plan.

        A credit-grant plan's money is its usage total/component model; an
        unrelated top-level `balance_display` must not become a second or
        fabricated Account balance.
        """
        row = self.r["rowCreditWalletIgnored"]
        self.assertEqual("Pro", row["plan"])
        self.assertEqual([], row["account_balances"])

    def test_window_plan_maps_gauges_and_iso_resets(self) -> None:
        row = self.r["rowWindow"]
        self.assertEqual("Pro Max", row["plan"])
        self.assertEqual(["Daily", "Weekly"], [w["label"] for w in row["windows"]])
        self.assertEqual([20, 75], [w["used_percent"] for w in row["windows"]])
        self.assertTrue(row["windows"][0]["reset_at"].endswith(".000Z"))
        self.assertIsNone(row["windows"][1]["reset_at"])
        self.assertEqual("active", row["access_state"])
        self.assertNotIn("windows_usable", row)
        # `usage.available` describes the separate dollar-credit model, which
        # is always unavailable for a window plan. Its authoritative gauges
        # must stay usable and rankable.
        unavailable = self.r["rowWindowUnavailable"]
        self.assertEqual("Pro Max", unavailable["plan"])
        self.assertEqual([20, 75], [w["used_percent"] for w in unavailable["windows"]])
        self.assertEqual("active", unavailable["access_state"])
        self.assertNotIn("windows_usable", unavailable)

    def test_live_plus_window_uses_exact_card_labels(self) -> None:
        row = self.r["rowPlusWindow"]
        self.assertEqual("Hermes Plus", row["plan"])
        self.assertEqual("active", row["access_state"])
        self.assertEqual(
            ["Daily", "Weekly", "Monthly", "Large models (Monthly)"],
            [w["label"] for w in row["windows"]],
        )
        self.assertEqual([0, 0, 0, 0], [w["used_percent"] for w in row["windows"]])
        for window in row["windows"]:
            self.assertTrue(window["reset_at"].endswith("Z"))
        # Billing's dollar-credit model is unavailable for a usage-window
        # plan, but its own `balance_display` is still the account wallet.
        # The mapping carries that one Account balance; the compatible cached
        # account facts arrive through the merge below.
        self.assertEqual(["Monthly cap: $0 of $1000"], row["details"])
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "9.99", "Account balance")],
        )
        self.assertTrue(row["usage_window_plan"])
        self.assertFalse(row["credits_model_available"])

    def test_paused_window_plan_is_paused_not_usable(self) -> None:
        row = self.r["rowWindowPaused"]
        self.assertEqual("Pro Max", row["plan"])
        self.assertEqual("paused", row["access_state"])
        # No credits_pay answer from an older portal: unknown, not a denial.
        self.assertIsNone(row["api_calls_available"])
        self.assertEqual([20, 75], [w["used_percent"] for w in row["windows"]])
        self.assertIn("Status: included usage paused - subscription payment failed",
                      row["details"])

    def test_signed_in_free_plan_is_free_not_depleted(self) -> None:
        # Real billing shape: status "depleted" is the aggregate paid-credit
        # verdict; plan_name "Free" + no plan window is an explicit free plan.
        row = self.r["rowFreePlan"]
        self.assertIsNotNone(row)
        self.assertEqual("Free", row["plan"])
        self.assertEqual("free", row["access_state"])
        self.assertEqual([], row["windows"])
        self.assertIsNone(row["api_calls_available"])
        self.assertIn("Free tier - free models only", row["details"])
        self.assertFalse(any("depleted" in line.lower() for line in row["details"]))

    def test_free_plan_with_purchased_credits_is_not_active(self) -> None:
        """Healthy/available can come from top-up credits, not a paid plan."""
        row = self.r["rowFreeTopup"]
        self.assertEqual("Free", row["plan"])
        self.assertEqual("free", row["access_state"])
        self.assertEqual([], row["windows"])
        # Purchased credits fund calls, so availability is yes; the plan is
        # still Free and no subscription renewal is claimed.
        self.assertIs(True, row["api_calls_available"])
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "10.00", "Account balance")],
        )
        body = json.dumps(row)
        self.assertIn("Free tier - paid models via purchased credits", body)
        self.assertNotIn("Top-up credits", body)
        self.assertNotIn("Free tier - free models only", body)
        self.assertNotIn("Subscription credits", body)
        self.assertNotIn("Cycle ends", body)
        self.assertNotIn("Account balance:", body)

    def test_planless_free_topup_with_explicit_no_subscription_is_free(self) -> None:
        """An explicit no-subscription answer outranks the aggregate paid flag.

        The payload has no plan name and a positive purchased top-up, which
        makes core's aggregate usage "healthy". It must stay a Free plan with
        its visible Account balance, and its padded renewal date must be
        dropped.
        """
        for key in ("rowFreeTopupNoPlanName", "rowFreeTopupNoPlanNameTopLevel"):
            row = self.r[key]
            self.assertEqual("Free", row["plan"], key)
            self.assertEqual("free", row["access_state"], key)
            self.assertEqual([], row["windows"], key)
            self.assertIs(True, row["api_calls_available"], key)
            self.assertEqual(
                [(b["currency"], b["total_balance"], b["label"])
                 for b in row["account_balances"]],
                [("USD", "10.00", "Account balance")],
                key,
            )
            body = json.dumps(row)
            self.assertIn("Free tier - paid models via purchased credits", body)
            self.assertNotIn("Top-up credits", body)
            self.assertNotIn("Cycle ends", body)

    def test_planless_topup_without_explicit_no_subscription_never_claims_active(self) -> None:
        """No plan identity is not a paid-plan claim; no stale renewal is shown."""
        self.assertIsNone(self.r["rowFreeTopupNoPlanNameNoFlag"])

    def test_free_topup_zero_is_known_zero_without_renewal(self) -> None:
        row = self.r["rowFreeTopupZero"]
        self.assertEqual("Free", row["plan"])
        self.assertEqual("free", row["access_state"])
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "0.00", "Account balance")],
        )
        self.assertIsNone(row["api_calls_available"])
        self.assertNotIn("Cycle ends", json.dumps(row))
        self.assertNotIn("Account balance", "\n".join(row["details"]))
        self.assertIn("Free tier - free models only", "\n".join(row["details"]))
        self.assertNotIn("Top-up credits: $0.00", "\n".join(row["details"]))

    def test_free_topup_without_total_field_stays_available_but_unpriced(self) -> None:
        """A missing total is unknown, never zero or a sum of component bars."""
        row = self.r["rowFreeTopupNoTotal"]
        self.assertEqual("Free", row["plan"])
        self.assertEqual("free", row["access_state"])
        self.assertEqual([], row["account_balances"])
        self.assertIs(True, row["api_calls_available"])
        self.assertNotIn("Cycle ends", json.dumps(row))
        body = "\n".join(row["details"])
        self.assertIn("Free tier - paid models via purchased credits", body)
        self.assertNotIn("Top-up credits", body)
        self.assertNotIn("Account balance", body)

    def test_free_funded_total_without_a_named_topup_stays_neutral(self) -> None:
        """A positive total with no component name is not \"free models only\"."""
        row = self.r["rowFreeFundedNoTopup"]
        self.assertEqual("Free", row["plan"])
        self.assertEqual("free", row["access_state"])
        self.assertIs(True, row["api_calls_available"])
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "5.00", "Account balance")],
        )
        body = "\n".join(row["details"])
        self.assertIn("Free tier - credits available", body)
        self.assertNotIn("free models only", body)
        self.assertNotIn("purchased credits", body)
        self.assertNotIn("Top-up credits", body)
        self.assertNotIn("Cycle ends", body)

    def test_supported_topup_only_shape_becomes_the_account_balance(self) -> None:
        """An absent total may use the full wallet only when subscription is zero."""
        row = self.r["rowFreeTopupOnlyTotal"]
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "10.00", "Account balance")],
        )
        body = "\n".join(row["details"])
        self.assertIn("Free tier - paid models via purchased credits", body)
        self.assertNotIn("Top-up credits", body)
        # Without the explicit zero-included-credit fact, no total is
        # fabricated from the wallet alone.
        unknown = self.r["rowFreeTopupUnknownSub"]
        self.assertEqual([], unknown["account_balances"])
        self.assertNotIn("Account balance", "\n".join(unknown["details"]))

    def test_free_tier_is_authoritative(self) -> None:
        row = self.r["rowFree"]
        self.assertEqual("Free", row["plan"])
        self.assertEqual("free", row["access_state"])
        self.assertEqual([], row["windows"])
        self.assertIn("Free tier model: nous/welcome", row["details"])
        self.assertIsNone(row["api_calls_available"])

    def test_status_free_without_free_tier_never_claims_free(self) -> None:
        self.assertIsNone(self.r["rowStatusFree"])
        self.assertIsNone(self.r["rowUnsupported"])
        # An explicit plan_name "Free" still needs the free-tier flag; the
        # window plan remains the only plan identity.
        self.assertEqual("Pro", self.r["rowFreeNameOnly"]["plan"])

    def test_logged_out_billing_falls_back_to_the_cache(self) -> None:
        # Core's BillingAuthError also answers `logged_in:false, error:null`,
        # so that shape is not a confirmed sign-out. It must fall back to the
        # cached row rather than hiding a healthy account.
        self.assertIsNone(self.r["rowLoggedOut"])
        # A read that failed to load also falls back, as before.
        self.assertIsNone(self.r["rowLoggedOutError"])
        self.assertTrue(self.r["mergedLoggedOutUnchanged"])

    def test_low_balance_is_not_an_active_green_row(self) -> None:
        row = self.r["rowLow"]
        self.assertEqual("Plus", row["plan"])
        self.assertEqual("unknown", row["access_state"])
        self.assertIsNone(row["api_calls_available"])
        self.assertEqual([], row["windows"])
        self.assertIn("Low balance - top up soon", row["details"])
        # The exact total field is trustworthy even when the aggregate status
        # is only a warning; it is shown prominently, not reworded as a
        # different wallet concept.
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "3.40", "Account balance")],
        )
        # A top-up-only low snapshot has no total field and no explicit
        # zero-included-credit proof, so no Account balance is invented and no
        # separate component line is rendered.
        bare = self.r["rowLowBare"]
        self.assertIsNotNone(bare)
        self.assertIsNone(bare["plan"])
        self.assertEqual("unknown", bare["access_state"])
        self.assertEqual([], bare["account_balances"])
        self.assertNotIn("Top-up credits", bare["details"])

    def test_depleted_is_the_only_state_that_claims_no_api_calls(self) -> None:
        row = self.r["rowDepleted"]
        self.assertEqual("depleted", row["access_state"])
        self.assertIs(False, row["api_calls_available"])
        self.assertEqual(100, row["windows"][0]["used_percent"])

    def test_bare_depleted_status_is_not_proof_of_exhaustion(self) -> None:
        """A status string alone must not overwrite a richer cached row."""
        self.assertIsNone(self.r["rowDepletedBare"])
        self.assertEqual("Tier 9", self.r["mergedBareDepleted"]["providers"]["nous"]["plan"])
        self.assertNotEqual("depleted",
                            self.r["mergedBareDepleted"]["providers"]["nous"].get("access_state"))

    def test_real_depleted_subscription_is_depleted(self) -> None:
        # Core shape: paid plan, zero remaining credits, plan bar fully spent.
        row = self.r["rowDepletedSubscription"]
        self.assertEqual("Pro", row["plan"])
        self.assertEqual("depleted", row["access_state"])
        self.assertIs(False, row["api_calls_available"])
        self.assertEqual(100, row["windows"][0]["used_percent"])
        self.assertIn("Status: access depleted - top up to restore", row["details"])
        merged = self.r["mergedDepletedSubscription"]["providers"]["nous"]
        self.assertEqual("depleted", merged["access_state"])
        self.assertNotEqual("Tier 9", merged["plan"])
        # The zero balance is corroboration even without a plan bar.
        no_bar = self.r["rowDepletedSubscriptionNoBar"]
        self.assertEqual("depleted", no_bar["access_state"])
        self.assertIs(False, no_bar["api_calls_available"])

    def test_member_spend_cap_with_positive_credits_is_not_depleted(self) -> None:
        # Core shape: aggregate paid_access=false because the org member cap
        # was exceeded, while subscription/top-up credits remain positive.
        # Topping up cannot restore this, so neither row nor merge may claim it.
        self.assertIsNone(self.r["rowMemberCap"])
        self.assertTrue(self.r["mergedMemberCapUnchanged"])
        merged = self.r["mergedMemberCap"]["providers"]["nous"]
        self.assertEqual("Tier 9", merged["plan"])
        blob = json.dumps(self.r["mergedMemberCap"]).lower()
        self.assertNotIn("top up to restore", blob)
        self.assertNotIn("access depleted", blob)

    def test_signed_in_free_without_a_plan_is_not_depleted(self) -> None:
        # Core shape: `paid_service_access:false` makes status "depleted" even
        # for a signed-in Free account: plan_name and every balance are null.
        self.assertIsNone(self.r["rowSignedInFree"])
        self.assertTrue(self.r["mergedSignedInFreeUnchanged"])
        merged = self.r["mergedSignedInFree"]["providers"]["nous"]
        self.assertEqual("Free", merged["plan"])
        self.assertEqual("free", merged["access_state"])
        self.assertNotIn("depleted", json.dumps(merged).lower())

    def test_paid_plan_without_credit_evidence_is_not_depleted(self) -> None:
        # A paid-tier name with no balance cannot prove exhaustion.
        self.assertIsNone(self.r["rowPaidNoBalance"])
        self.assertTrue(self.r["mergedPaidNoBalanceUnchanged"])
        self.assertEqual("Tier 9",
                         self.r["mergedPaidNoBalance"]["providers"]["nous"]["plan"])

    def test_paused_plan_reports_credit_continuation_from_credits_pay(self) -> None:
        paid = self.r["rowPausedPay"]
        self.assertEqual("paused", paid["access_state"])
        self.assertIs(True, paid["api_calls_available"])
        self.assertIn("purchased credits still apply", " ".join(paid["details"]))
        # Explicit false is proof that purchased credits do not continue.
        stops = self.r["rowPausedNoPay"]
        self.assertEqual("paused", stops["access_state"])
        self.assertIs(False, stops["api_calls_available"])
        self.assertNotIn("purchased credits still apply", " ".join(stops["details"]))
        # An older portal omits the field: stay unknown on both flags.
        unknown = self.r["rowPausedUnknown"]
        self.assertEqual("paused", unknown["access_state"])
        self.assertIsNone(unknown["api_calls_available"])
        self.assertNotIn("purchased credits still apply", " ".join(unknown["details"]))

    def test_paused_plan_never_borrows_a_cached_total(self) -> None:
        """A paused plan withholds its included credits; its cached total lies.

        The cached row still reports $18 from the paused subscription, but
        Billing's explicitly empty wallet is the spendable amount. The merge
        must not replace that authoritative wallet with the cached total.
        """
        row = self.r["mergedPausedCacheTotal"]["providers"]["nous"]
        self.assertEqual("paused", row["access_state"])
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "0.00", "Account balance")],
        )
        self.assertNotIn("18.00", json.dumps(row))

    def test_active_exhausted_allowance_with_credits_pay_false_is_unusable(self) -> None:
        """A spent general allowance plus `credits_pay: false` proves a stop."""
        row = self.r["rowActiveExhaustedNoPay"]
        self.assertEqual("active", row["access_state"])
        self.assertIs(False, row["api_calls_available"])
        # Only an all-scope gauge gates every paid model; a full top-tier
        # gauge alone must not become a general denial.
        top_tier = self.r["rowTopTierExhaustedNoPay"]
        self.assertEqual("active", top_tier["access_state"])
        self.assertIsNone(top_tier["api_calls_available"])

    def test_credits_pay_with_an_explicitly_empty_wallet_is_unusable(self) -> None:
        """`credits_pay` promises purchases; Billing's zero wallet voids it."""
        paused = self.r["rowPausedPayEmptyWallet"]
        self.assertEqual("paused", paused["access_state"])
        self.assertIs(False, paused["api_calls_available"])
        self.assertNotIn("purchased credits still apply", " ".join(paused["details"]))
        exhausted = self.r["rowActiveExhaustedPayEmptyWallet"]
        self.assertEqual("active", exhausted["access_state"])
        self.assertIs(False, exhausted["api_calls_available"])

    def test_pane_refresh_button_invalidates_quota_and_billing(self) -> None:
        """The pane's own Refresh control must repaint both sources.

        `quota refresh` rewrites only the CLI cache. Clicking the real button
        runs the component's mutation and its onSuccess, which must invalidate
        the Billing query too; otherwise the card keeps a stale Billing half.
        """
        out = run(
            component="pane",
            data=_cli_plus_window_payload(),
            billingData=_plus_window_billing(),
            clickTitle="Refresh quota",
        )
        self.assertTrue(out["mutated"])
        self.assertEqual(
            [["quota", "widget", "local", "work"], ["quota", "billing", "local", "work"]],
            out["invalidated"],
        )
        self.assertEqual(["quota", "refresh"], out["cli"][0]["argv"][-2:])

    def test_billing_row_carries_no_payment_or_org_fields(self) -> None:
        blob = json.dumps(self.r["rowCredit"]) + json.dumps(self.r["mergedCredit"])
        for private in ("9999", "SECRET-ORG", "payment_method", "auto_reload",
                        "org_name", "org_slug", "role", "card"):
            self.assertNotIn(private, blob)

    def test_merge_replaces_the_nous_row_only(self) -> None:
        merged = self.r["mergedCredit"]
        row = merged["providers"]["nous"]
        self.assertEqual("Pro", row["plan"])
        self.assertEqual(30, row["windows"][0]["used_percent"])
        self.assertNotIn("stale cache detail", json.dumps(row))
        self.assertEqual({"windows": []}, merged["providers"]["anthropic"])
        self.assertEqual(30, merged["age_s"])

    def test_free_merge_drops_stale_paid_windows(self) -> None:
        row = self.r["mergedFree"]["providers"]["nous"]
        self.assertEqual("Free", row["plan"])
        self.assertEqual([], row["windows"])
        self.assertEqual("free", row["access_state"])

    def test_fallbacks_keep_the_cached_payload_identical(self) -> None:
        self.assertTrue(self.r["mergedStatusFreeUnchanged"])
        self.assertTrue(self.r["mergedUnavailableUnchanged"])
        # Billing can still supply a row when the cache had none.
        self.assertEqual("Pro", self.r["mergedNoProvider"]["providers"]["nous"]["plan"])

    def test_merge_keeps_quota_age_and_separates_billing_freshness(self) -> None:
        # Global `age_s` is quota-cache provenance: an older/fresher billing
        # read must not rewrite it for unrelated providers (Codex).
        older = self.r["mergedAgeOlderBilling"]
        self.assertEqual(30, older["age_s"])
        self.assertEqual(120, older["providers"]["nous"]["billing_age_s"])
        self.assertTrue(older["providers"]["nous"]["billing"])
        fresh = self.r["mergedAgeFreshBilling"]
        self.assertEqual(30, fresh["age_s"])
        self.assertEqual(5, fresh["providers"]["nous"]["billing_age_s"])
        no_billing_age = self.r["mergedAgeNoBillingAge"]
        self.assertEqual(30, no_billing_age["age_s"])
        self.assertNotIn("billing_age_s", no_billing_age["providers"]["nous"])
        missing_cache = self.r["mergedAgeMissingCache"]
        self.assertNotIn("age_s", missing_cache)
        self.assertEqual(42, missing_cache["providers"]["nous"]["billing_age_s"])


class IndependentSourceMergeTests(unittest.TestCase):
    """Billing owns plan/windows/money; a compatible account snapshot may add.

    The cached CLI account row is a whole-row fallback only. For a
    credit-grant plan its financial facts and Account balance never
    decorate a Billing row, because `billing.state` exposes no signed-in user
    id. A usage-window plan is the exception: core's dollar-credit model is
    unavailable there, so a fresh cached row with the same exact plan label,
    access state and pseudonymous organisation digest may supply the Account
    balance and the renewal/spend lines. The account's component amounts are
    folded into that total, never rendered as separate lines. That is a
    same-organisation hint, not account proof: another user in the same
    organisation can match, and the docs disclose that residual limitation.
    A rate-limit line follows the same rule. A documented Billing plan with no
    live claim shows its `(published)` tier ceiling instead, including when a
    low balance maps the access state to `unknown`.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.r = _run_probe()

    @staticmethod
    def _row(payload):
        return payload["providers"]["nous"]

    def test_same_org_free_actual_rate_limits_merge_beside_billing(self) -> None:
        row = self._row(self.r["mergedMasterFreeWithOrg"])
        self.assertEqual("Free", row["plan"])
        body = "\n".join(row["details"])
        self.assertIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH", body)
        self.assertNotIn("Rate ceiling", body)
        self.assertEqual(1, body.count("Rate limits:"))

    def test_org_mismatch_or_stale_cache_never_merges_actual_rates(self) -> None:
        for key in ("mergedMasterFreeOrgMismatch", "mergedMasterFreeWithOrgStale"):
            with self.subTest(key=key):
                row = self._row(self.r[key])
                body = "\n".join(row["details"])
                self.assertNotIn("Rate limits:", body)
                self.assertIn("Rate ceiling: 50 RPM / 500k TPM (published)", body)

    def test_malformed_or_forged_rate_line_never_reaches_the_card(self) -> None:
        row = self._row(self.r["mergedMalformedRateWithOrg"])
        body = "\n".join(row["details"])
        self.assertNotIn("Rate limits:", body)
        self.assertNotIn("forged", body)
        self.assertIn("Rate ceiling: 50 RPM / 500k TPM (published)", body)

    def test_same_org_paid_actual_rates_merge_and_keep_rph(self) -> None:
        row = self._row(self.r["mergedPaidActualRatesWithOrg"])
        self.assertEqual("Plus", row["plan"])
        body = "\n".join(row["details"])
        self.assertIn("Rate limits: 400 RPM · 4M TPM · 16.8k RPH", body)
        self.assertNotIn("Rate ceiling", body)

    def test_renewal_is_neutral_because_cancellation_is_unreliable(self) -> None:
        """`billing.state` has no cancellation fact, so a cancelled plan is
        indistinguishable from a renewing one. The card must not assert
        "Renews"; "Cycle ends" is true in both cases."""
        row = self.r["rowCredit"]
        details = "\n".join(row["details"])
        self.assertIn("Cycle ends: Dec 3, 2026", details)
        self.assertNotIn("Renews", details)
        self.assertEqual(row["detail_facts"]["cycle_ends"], "Cycle ends: Dec 3, 2026")

    def test_live_plus_window_merges_one_account_balance(self) -> None:
        """The live Plus shape: Billing has the gauges and the wallet balance.

        Core's dollar-credit model is unavailable on a usage-window plan, so
        Billing's own `balance_display` supplies the Account balance. The
        account row's component lines are folded into that one total, never
        rendered beside it.
        """
        row = self._row(self.r["mergedPlusWindow"])
        self.assertEqual("Hermes Plus", row["plan"])
        self.assertEqual("active", row["access_state"])
        self.assertEqual(
            ["Daily", "Weekly", "Monthly", "Large models (Monthly)"],
            [w["label"] for w in row["windows"]],
        )
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "9.99", "Account balance")],
        )
        body = "\n".join(row["details"])
        self.assertNotIn("Subscription credits", body)
        self.assertNotIn("Top-up credits", body)
        self.assertIn("Cycle ends: 2026-12-06T13:00:47.000Z", body)
        # Billing's monthly-cap line owns the cap answer, so the account
        # qualifier is normalized away instead of contradicting it.
        self.assertIn("Spend this period: $0.00", body)
        self.assertNotIn("no cap reported", body)
        self.assertNotIn("Rate limits", body)
        self.assertNotIn("Tool pool", body)
        # keep_going_on_credits is false on this plan: the balance is visible,
        # but the card must not claim it automatically pays included
        # subscription usage.
        self.assertNotIn("paid models via purchased credits", body)
        self.assertNotIn("credits still apply", body)
        self.assertEqual(1, json.dumps(row).count("Account balance"))

    def test_plus_window_component_lines_never_render(self) -> None:
        zero = self._row(self.r["mergedPlusWindowZeroSubscription"])
        self.assertNotIn("Subscription credits", "\n".join(zero["details"]))
        positive = self._row(self.r["mergedPlusWindowPositiveSubscription"])
        details = "\n".join(positive["details"])
        self.assertNotIn("Subscription credits", details)
        self.assertNotIn("Top-up credits", details)

    def test_plus_window_money_requires_matching_identity_state_and_freshness(self) -> None:
        """A same-org, same-plan cached row still fails closed when it is stale.

        A wrong plan, organisation or access state is also refused. The one
        Account balance is Billing's own wallet, so it survives every one of
        these; only the cached account facts (renewal/spend/components) are
        withheld.
        """
        for key in (
            "mergedPlusWindowStale",
            "mergedPlusWindowWrongPlan",
            "mergedPlusWindowWrongOrg",
            "mergedPlusWindowWrongState",
        ):
            with self.subTest(key=key):
                row = self._row(self.r[key])
                self.assertEqual(
                    [(b["currency"], b["total_balance"], b["label"])
                     for b in row["account_balances"]],
                    [("USD", "9.99", "Account balance")],
                    key,
                )
                body = "\n".join(row["details"])
                self.assertNotIn("Account balance", body, key)
                self.assertNotIn("Top-up credits", body, key)
                self.assertNotIn("Subscription credits", body, key)
                self.assertNotIn("Cycle ends:", body, key)
                self.assertNotIn("Spend this period", body, key)

    def test_plus_window_billing_wallet_never_needs_the_account_cache(self) -> None:
        """The Billing wallet alone produces the card's one Account balance.

        A missing, stale or unmatched cached account row must not be required
        for money Billing itself reported. The cache may only add its
        independently checked renewal/spend facts.
        """
        row = self._row(self.r["mergedPlusWindowNoMaster"])
        self.assertEqual("Hermes Plus", row["plan"])
        self.assertEqual(
            [w["label"] for w in row["windows"]],
            ["Daily", "Weekly", "Monthly", "Large models (Monthly)"],
        )
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "9.99", "Account balance")],
        )
        body = "\n".join(row["details"])
        self.assertNotIn("Top-up credits", body)
        self.assertNotIn("Subscription credits", body)
        self.assertNotIn("Cycle ends:", body)
        self.assertNotIn("Spend this period", body)
        self.assertIn("Monthly cap: $0 of $1000", body)

    def test_plus_window_billing_money_outranks_cached_account_facts(self) -> None:
        row = self._row(self.r["mergedPlusWindowBillingMoney"])
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "4.00", "Account balance")],
        )
        self.assertNotIn("9.99", json.dumps(row))
        self.assertNotIn("Top-up credits", "\n".join(row["details"]))

    def test_plus_window_matching_cached_total_outranks_the_billing_wallet(self) -> None:
        """A fresh identity-matched total is the actual balance, not the wallet.

        Billing reports the purchased wallet when its dollar-credit model is
        unavailable; the account snapshot's `total_usable` can include more.
        That actual total wins only after the same freshness/plan/state/org
        checks; a missing or stale snapshot leaves Billing's wallet in place.
        """
        row = self._row(self.r["mergedPlusWindowCacheTotal"])
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "14.99", "Account balance")],
        )
        self.assertNotIn("9.99", json.dumps(row))

    def test_published_ceilings_cover_every_documented_desktop_tier(self) -> None:
        expected = {
            "Free": "Rate ceiling: 50 RPM / 500k TPM (published)",
            "Basic": "Rate ceiling: 400 RPM / 2M TPM (published)",
            "Plus": "Rate ceiling: 400 RPM / 4M TPM (published)",
            "Hermes Plus": "Rate ceiling: 400 RPM / 4M TPM (published)",
            "Super": "Rate ceiling: 800 RPM / 8M TPM (published)",
            "Scale": "Rate ceiling: 600 RPM / 6M TPM (published)",
            "Ultra": "Rate ceiling: 1.6k RPM / 16M TPM (published)",
            "Default paid users": "Rate ceiling: 180 RPM / 720k TPM (published)",
            "Pro": None,
        }
        self.assertEqual(expected, self.r["publishedCeilings"])

    def test_merged_row_never_carries_the_billing_org_match_key(self) -> None:
        for key in ("mergedMasterFreeWithOrg", "mergedPaidActualRatesWithOrg"):
            blob = json.dumps(self._row(self.r[key]))
            self.assertNotIn("_billing_org_key", blob, key)
            self.assertNotIn("org_synthetic", blob, key)

    def test_active_billing_keeps_only_its_own_facts(self) -> None:
        row = self._row(self.r["mergedMasterActive"])
        self.assertEqual("Pro", row["plan"])
        self.assertEqual("active", row["access_state"])
        self.assertEqual(30, self.r["mergedMasterActive"]["age_s"])
        body = "\n".join(row["details"])
        # Billing's own renewal/cap lines survive; the component credit lines
        # do not exist on any Nous card anymore.
        self.assertIn("Cycle ends: Dec 3, 2026", body)
        self.assertIn("Monthly cap: $6.00 of $200.00", body)
        for gone in (
            "$14.00 of $20.00 subscription credits left",
            "$12.00 of $12.00 top-up credits left",
        ):
            self.assertNotIn(gone, body)
        # No cached master fact or balance is merged, even when the two
        # entitlement states happen to match.
        for stale in (
            "Rollover", "Rate limits", "Total usable: $26.00",
            "Spend this period", "Subscription credits: $14.00",
            "Top-up credits: $12.00",
        ):
            self.assertNotIn(stale, body)
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "26.00", "Account balance")],
        )
        self.assertNotIn("Tier 9", body)

    def test_active_billing_ignores_unknown_master_rate_limits(self) -> None:
        """Rate limits are plan-specific; unknown master state proves no plan."""
        row = self._row(self.r["mergedMasterUnknownIntoActive"])
        self.assertEqual("Pro", row["plan"])
        self.assertEqual("active", row["access_state"])
        body = "\n".join(row["details"])
        self.assertNotIn("Rate limits", body)
        self.assertNotIn("Account balance", body)
        self.assertNotIn("Spend this period", body)
        self.assertNotIn("subscription credits left", body)
        self.assertNotIn("Top-up credits", body)

    def test_unknown_master_state_never_merges_into_same_plan_active(self) -> None:
        """Same org and label are not enough when the cached state is unknown."""
        row = self._row(self.r["mergedUnknownSamePlanIntoActive"])
        self.assertEqual("Plus", row["plan"])
        self.assertEqual("active", row["access_state"])
        body = "\n".join(row["details"])
        self.assertNotIn("Rate limits", body)
        self.assertIn("Rate ceiling: 400 RPM / 4M TPM (published)", body)

    def test_low_balance_paid_exact_plan_keeps_published_ceiling(self) -> None:
        """A low balance is unknown access, not a reason to drop the plan ceiling."""
        row = self._row(self.r["mergedLowPaidPublished"])
        self.assertEqual("Plus", row["plan"])
        self.assertEqual("unknown", row["access_state"])
        body = "\n".join(row["details"])
        self.assertIn("Low balance - top up soon", body)
        self.assertIn("Rate ceiling: 400 RPM / 4M TPM (published)", body)
        self.assertNotIn("Rate limits:", body)

    def test_master_spend_never_merges_into_billing(self) -> None:
        body = "\n".join(self._row(self.r["mergedMasterNoCap"])["details"])
        self.assertNotIn("Spend this period", body)
        self.assertNotIn("Rate limits", body)

    def test_free_billing_keeps_only_non_financial_cached_facts(self) -> None:
        for key in ("mergedMasterFree", "mergedMasterFreePlan"):
            row = self._row(self.r[key])
            self.assertEqual("Free", row["plan"])
            self.assertEqual("free", row["access_state"])
            body = "\n".join(row["details"])
            # Billing's own Free line survives. The cached rate claim and the
            # non-financial free tool pool merge only because the fixture's
            # same-profile snapshot proves the same Free plan, state and
            # organisation; financial facts never merge.
            self.assertIn("Free tier - free models only", body)
            self.assertIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH", body)
            self.assertIn("Tool pool: browser-use, firecrawl", body)
            self.assertEqual(1, body.count("Tool pool:"))
            for stale in ("Tier 9", "Total usable",
                          "Rollover", "Spend this period"):
                self.assertNotIn(stale, body)

    def test_disabled_stale_unknown_or_paid_tool_pool_is_never_shown(self) -> None:
        """Only a fresh, usable Free snapshot may contribute the pool line.

        The fact is emitted on the account fetcher only when
        `tool_access.enabled` is true, so absence means disabled/unknown. A
        stale snapshot, a failed/unknown account read, and a paid Billing row
        all suppress it; a paid plan has no free pool to show.
        """
        for key in (
            "mergedToolPoolDisabled",
            "mergedToolPoolStale",
            "mergedToolPoolUnavailable",
            "mergedToolPoolUnknown",
            "mergedToolPoolPaid",
            "mergedToolPoolNoOrg",
            "mergedToolPoolOrgMismatch",
        ):
            row = self._row(self.r[key])
            self.assertNotIn("Tool pool", "\n".join(row["details"]), key)

    def test_free_tool_pool_survives_the_org_guard_when_org_proof_matches(self) -> None:
        """A genuinely same-organisation Free cache keeps the visible pool."""
        row = self._row(self.r["mergedMasterFree"])
        self.assertEqual("Free", row["plan"])
        self.assertEqual("free", row["access_state"])
        self.assertIn("Tool pool: browser-use, firecrawl", "\n".join(row["details"]))

    def test_tool_pool_rejects_control_text_and_never_duplicates(self) -> None:
        control = self._row(self.r["mergedToolPoolControl"])
        self.assertNotIn("Tool pool", "\n".join(control["details"]))
        duplicate = self._row(self.r["mergedToolPoolDuplicate"])
        body = "\n".join(duplicate["details"])
        self.assertEqual(1, body.count("Tool pool:"), body)

    def test_tool_pool_is_stable_across_repeated_merges(self) -> None:
        """Re-rendering the same fresh snapshot does not drop or duplicate it."""
        first = "\n".join(self._row(self.r["mergedMasterFree"])["details"])
        again = _run_probe()
        second = "\n".join(self._row(again["mergedMasterFree"])["details"])
        self.assertEqual(first, second)
        self.assertEqual(1, second.count("Tool pool:"))

    def test_stale_paid_master_never_leaks_into_free_billing(self) -> None:
        row = self._row(self.r["mergedMasterIntoFree"])
        self.assertEqual("Free", row["plan"])
        self.assertEqual("free", row["access_state"])
        body = "\n".join(row["details"])
        for stale in ("Tier 9", "Rate limits", "Total usable", "Rollover",
                      "Spend this period", "Subscription credits"):
            self.assertNotIn(stale, body)

    def test_stale_free_master_never_leaks_into_paid_billing(self) -> None:
        row = self._row(self.r["mergedMasterFreeIntoActive"])
        self.assertEqual("Pro", row["plan"])
        self.assertEqual("active", row["access_state"])
        body = "\n".join(row["details"])
        self.assertNotIn("Rate limits", body)
        self.assertNotIn("Tool pool", body)

    def test_depleted_billing_does_not_reuse_an_active_master_row(self) -> None:
        row = self._row(self.r["mergedMasterIntoDepleted"])
        self.assertEqual("depleted", row["access_state"])
        body = "\n".join(row["details"])
        self.assertIn("Status: access depleted - top up to restore", body)
        for stale in ("Rate limits", "Total usable", "Rollover", "Spend this period"):
            self.assertNotIn(stale, body)

    def test_failed_master_source_contributes_nothing(self) -> None:
        row = self._row(self.r["mergedMasterUnavailable"])
        self.assertEqual("Pro", row["plan"])
        body = "\n".join(row["details"])
        for stale in ("Rate limits", "Total usable", "Rollover", "Spend this period"):
            self.assertNotIn(stale, body)

    def test_missing_or_old_cache_age_contributes_nothing(self) -> None:
        for key in ("mergedMasterNoAge", "mergedMasterTooOld"):
            row = self._row(self.r[key])
            self.assertEqual("Pro", row["plan"])
            body = "\n".join(row["details"])
            self.assertNotIn("Rate limits", body)
            self.assertNotIn("Total usable", body)

    def test_master_balance_never_fills_a_missing_billing_total(self) -> None:
        """A missing Billing total stays unknown; the cached amount is not proof."""
        row = self._row(self.r["mergedMasterBalanceFallback"])
        self.assertEqual("Pro", row["plan"])
        self.assertEqual([], row["account_balances"])
        self.assertNotIn("Total usable", "\n".join(row["details"]))

    def test_billing_total_wins_over_a_stale_master_balance(self) -> None:
        row = self._row(self.r["mergedMasterBalanceOverridden"])
        self.assertEqual(
            [(b["currency"], b["total_balance"], b["label"])
             for b in row["account_balances"]],
            [("USD", "26.00", "Account balance")],
        )
        self.assertNotIn("99.00", json.dumps(row))

    def test_master_balance_does_not_cross_a_free_paid_boundary(self) -> None:
        row = self._row(self.r["mergedMasterBalanceIntoFree"])
        self.assertEqual("Free", row["plan"])
        self.assertEqual([], row["account_balances"])

    def test_sanitized_billing_row_gets_no_master_facts(self) -> None:
        row = self._row(self.r["mergedMasterSanitized"])
        self.assertEqual("Pro", row["plan"])
        self.assertNotIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH",
                         "\n".join(row["details"]))
        blob = json.dumps(row)
        for private in ("SECRET-ORG", "9999", "payment_method", "auto_reload",
                        "org_name", "org_slug", "role", "card"):
            self.assertNotIn(private, blob)

    def test_low_bare_billing_does_not_backfill_a_master_plan_or_facts(self) -> None:
        row = self._row(self.r["mergedMasterIntoLowBare"])
        self.assertIsNone(row["plan"])
        self.assertEqual("unknown", row["access_state"])
        body = "\n".join(row["details"])
        self.assertIn("Low balance - top up soon", body)
        for stale in ("Tier 9", "Rate limits", "Total usable", "Rollover"):
            self.assertNotIn(stale, body)

    def test_both_sources_failing_keeps_the_cached_payload_identical(self) -> None:
        self.assertTrue(self.r["mergedBothFailSame"])
        row = self._row(self.r["mergedBothFail"])
        self.assertEqual("fetch-error", row["unavailable_reason"])
        self.assertEqual([], row["windows"])

    def test_master_fact_values_must_be_strings(self) -> None:
        row = self._row(self.r["mergedMasterBadFact"])
        self.assertEqual("Pro", row["plan"])
        body = "\n".join(row["details"])
        self.assertNotIn("Rate limits", body)
        self.assertNotIn("Total usable", body)
        self.assertNotIn("forged", json.dumps(row))


class BillingRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.r = _run_probe()

    def test_exact_route_reads_billing_without_a_profile_param(self) -> None:
        result = self.r["routeExact"]
        self.assertEqual("Pro", result["result"]["usage"]["plan_name"])
        self.assertEqual(1, len(result["calls"]))
        call = result["calls"][0]
        self.assertEqual("billing.state", call["method"])
        self.assertEqual({}, call["params"])
        self.assertEqual("shared", call["route"]["profile"])
        self.assertEqual("shared", call["route"]["targetProfile"])
        self.assertGreater(call["timeoutMs"], 0)
        self.assertLessEqual(call["timeoutMs"], 8000)

    def test_unresolvable_or_mismatched_routes_make_no_billing_call(self) -> None:
        for name in ("routeMismatch", "routeAmbiguous", "routeMissing", "noOwner"):
            case = self.r[name]
            self.assertIsNone(case["result"], name)
            self.assertEqual([], case["calls"], name)

    def test_legacy_single_connection_names_the_profile(self) -> None:
        case = self.r["legacy"]
        self.assertEqual("Pro", case["result"]["usage"]["plan_name"])
        self.assertEqual(1, len(case["calls"]))
        call = case["calls"][0]
        self.assertEqual("billing.state", call["method"])
        self.assertEqual({"profile": "default"}, call["params"])
        self.assertGreater(call["timeoutMs"], 0)
        self.assertIsNone(self.r["noLiveDoor"])

    def test_gateway_errors_and_bad_shapes_fall_back(self) -> None:
        self.assertIsNone(self.r["oldGateway"])
        self.assertIsNone(self.r["unknownProfile"])
        self.assertIsNone(self.r["malformed"])

    def test_timeout_is_bounded_and_late_rejection_is_handled(self) -> None:
        self.assertIsNone(self.r["timeoutResult"])
        self.assertLess(self.r["timeoutMs"], 300)
        self.assertEqual(0, self.r["unhandled"])

    def test_hung_route_discovery_recovers_on_a_later_cadence(self) -> None:
        self.assertIsNone(self.r["routeHangFirst"])
        self.assertIsNotNone(self.r["routeHangSecond"])
        self.assertEqual(2, self.r["routeHangCalls"])

    def test_never_settling_gateway_releases_the_dedupe_entry(self) -> None:
        self.assertIsNone(self.r["neverFirst"])
        self.assertIsNone(self.r["neverSecond"])
        self.assertEqual(2, self.r["neverCalls"])

    def test_timed_out_read_stays_deduped_until_the_socket_settles(self) -> None:
        self.assertIsNone(self.r["overlapFirst"])
        self.assertIsNone(self.r["overlapSecond"])
        self.assertEqual(1, self.r["overlapCallsBeforeIdle"])
        # Only after the abandoned request settles may the next cadence start
        # exactly one new read.
        self.assertEqual(2, self.r["overlapCallsAfterIdle"])
        self.assertIsNone(self.r["overlapThird"])
        self.assertEqual(0, self.r["unhandled"])

    def test_explicit_refresh_invalidates_quota_and_billing(self) -> None:
        self.assertEqual([
            ["quota", "remote-a", "shared"],
            ["quota", "billing", "remote-a", "shared"],
        ], self.r["invalidated"])

    def test_one_in_flight_read_per_scope_then_one_per_cadence(self) -> None:
        dedupe = self.r["dedupe"]
        self.assertEqual(1, dedupe["calls"])
        self.assertTrue(dedupe["samePromise"])
        self.assertTrue(dedupe["sameValue"])
        self.assertEqual(2, dedupe["afterSettled"])

    def test_profile_switch_uses_a_separate_read(self) -> None:
        self.assertEqual(2, self.r["profileSwitchCalls"])
        self.assertEqual({"shared", "home"}, set(self.r["profileSwitchRoutes"]))
        self.assertNotEqual(self.r["keys"]["a"], self.r["keys"]["b"])
        self.assertEqual(["quota", "billing", None, None], self.r["keys"]["none"])

    def test_staggered_observers_share_one_cadence_window(self) -> None:
        cadence = self.r["cadence"]
        self.assertEqual(1, cadence["calls"])
        self.assertTrue(cadence["sameRow"])
        self.assertTrue(cadence["sameObservedAt"])
        # Only the whitelisted row outlives the request, never the gateway
        # response with card/payment/organization decoys.
        for private in ("9999", "SECRET-ORG", "payment_method", "auto_reload",
                        "org_name", "org_slug", "role", '"card"'):
            self.assertNotIn(private, cadence["serialized"])
        # An explicit refresh bypasses the throttle.
        self.assertEqual(2, cadence["afterReset"])

    def test_hook_uses_its_own_namespaced_cadence_key(self) -> None:
        hook = self.r["hook"]
        self.assertEqual({"marker": "billing"}, hook["data"])
        self.assertTrue(hook["noScopeDataUndefined"])
        enabled, disabled = hook["options"]
        self.assertEqual(["quota", "billing", "remote-a", "shared"], enabled["key"])
        self.assertTrue(enabled["enabled"])
        self.assertFalse(enabled["retry"])
        self.assertEqual(600000, enabled["refetchInterval"])
        self.assertFalse(enabled["refetchOnWindowFocus"])
        self.assertEqual(300000, enabled["staleTime"])
        self.assertFalse(enabled["structuralSharing"])
        self.assertFalse(enabled["hasPlaceholder"])
        self.assertEqual("function", enabled["queryFn"])
        self.assertFalse(disabled["enabled"])

    def test_query_cache_receives_only_the_sanitized_row(self) -> None:
        row = self.r["hookQueryRow"]
        self.assertEqual("nous", row["label"])
        self.assertEqual("Pro", row["plan"])
        blob = json.dumps(row)
        for private in ("9999", "SECRET-ORG", "payment_method", "auto_reload",
                        "org_name", "org_slug", "role", "card",
                        "_billing_org_key", "1523bbad0010557d"):
            self.assertNotIn(private, blob)
        self.assertEqual("Pro", self.r["mergedSanitizedRow"]["providers"]["nous"]["plan"])

    def test_query_hook_copy_keeps_the_same_org_live_rate_merge(self) -> None:
        row = self.r["hookQueryRow"]
        self.assertNotIn("_billing_org_key", json.dumps(row))
        body = "\n".join(self.r["mergedHookLiveRate"]["providers"]["nous"]["details"])
        self.assertIn("Rate limits: 50 RPM · 500k TPM", body)


class BillingRenderTests(unittest.TestCase):
    def test_pane_renders_billing_plan_windows_and_details(self) -> None:
        out = run(component="pane", data=_cli_payload(),
                  billingData=_credit_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Pro"], _plans(out["tree"]))
        body = text(out["tree"])
        for expected in ("Cycle ends: Dec 3, 2026",
                         "Monthly cap: $6.00 of $200.00",
                         "Account balance",
                         "$26.00 USD"):
            self.assertIn(expected, body)
        for gone in ("$14.00 of $20.00 subscription credits left",
                     "$12.00 of $12.00 top-up credits left",
                     "Top-up credits"):
            self.assertNotIn(gone, body)
        self.assertNotIn("Tier 9", body)
        self.assertNotIn("stale cache detail", body)
        queries = _billing_queries(out)
        self.assertEqual(1, len(queries))
        self.assertEqual(["quota", "billing", "local", "work"], queries[0]["key"])
        self.assertTrue(queries[0]["enabled"])

    def test_pane_billing_does_not_merge_cached_master_facts(self) -> None:
        out = run(component="pane", data=_cli_master_payload(),
                  billingData=_credit_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Pro"], _plans(out["tree"]))
        body = text(out["tree"])
        for expected in (
            "Account balance",
            "$26.00 USD",
        ):
            self.assertIn(expected, body)
        for stale in (
            "Rate limits", "Rollover", "Spend this period",
            "Total usable: $26.00", "Subscription credits: $14.00",
            "Top-up credits: $12.00",
        ):
            self.assertNotIn(stale, body)
        self.assertNotIn("Tier 9", body)

    def test_pane_free_billing_merges_non_financial_cached_facts(self) -> None:
        out = run(component="pane", data=_cli_free_master_payload(),
                  billingData=_free_plan_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Free"], _plans(out["tree"]))
        body = text(out["tree"])
        self.assertIn("Free tier - free models only", body)
        self.assertIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH", body)
        self.assertIn("Tool pool: browser-use, firecrawl", body)
        self.assertNotIn("Tier 9", body)
        self.assertNotIn("access depleted", body)

    def test_pane_free_topup_is_visible_but_still_free(self) -> None:
        """The $10 wallet is one prominent Account balance, not a subscription."""
        out = run(component="pane", data=_cli_free_master_payload(),
                  billingData=_free_topup_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Free"], _plans(out["tree"]))
        body = text(out["tree"])
        self.assertIn("Account balance", body)
        self.assertIn("$10.00 USD", body)
        self.assertEqual(1, body.count("Account balance"))
        self.assertNotIn("Top-up credits", body)
        self.assertIn("API calls available: yes", body)
        self.assertNotIn("Cycle ends", body)
        self.assertNotIn("Tier 9", body)
        self.assertNotIn("access depleted", body)

        card = text(run(expr="QuotaCard({close(){}})", data=_cli_free_master_payload(),
                        billingData=_free_topup_billing(),
                        storage={"monitorEnabled": False})["value"])
        self.assertIn("Account balance", card)
        self.assertIn("$10.00 USD", card)
        self.assertNotIn("Top-up credits", card)
        self.assertNotIn("Cycle ends", card)

    def test_pane_free_billing_hides_stale_paid_master_facts(self) -> None:
        out = run(component="pane", data=_cli_master_payload(),
                  billingData=_free_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Free"], _plans(out["tree"]))
        body = text(out["tree"])
        for stale in ("Tier 9", "Rate limits", "Total usable", "Rollover",
                      "Spend this period"):
            self.assertNotIn(stale, body)

    def test_pane_billing_failure_keeps_the_full_master_row(self) -> None:
        out = run(component="pane", data=_cli_master_payload(),
                  billingIsError=True, storage={"monitorEnabled": False})
        self.assertEqual(["Tier 9"], _plans(out["tree"]))
        body = text(out["tree"])
        self.assertIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH", body)
        self.assertIn("Total usable: $26.00", body)

    def test_pane_failed_master_source_hides_its_facts(self) -> None:
        out = run(component="pane", data=_cli_unavailable_master_payload(),
                  billingData=_credit_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Pro"], _plans(out["tree"]))
        body = text(out["tree"])
        self.assertNotIn("Rate limits", body)
        self.assertNotIn("Spend this period", body)
        # Billing's own total is still legitimate; only the cached master
        # detail with that identity must not survive.
        self.assertIn("$26.00 USD", body)
        self.assertNotIn("Total usable: $26.00", body)

    def test_profile_switch_never_paints_another_profiles_cached_facts(self) -> None:
        work = _cli_master_payload()
        home = _cli_master_payload()
        home["providers"]["nous"]["detail_facts"]["rate_limits"] = "Rate limits: 99 RPM"
        out = run(component="pane",
                  dataByProfile={"work": work, "home": home},
                  billingByProfile={"work": _credit_billing(),
                                    "home": _window_billing()},
                  owner={"connectionId": "local", "profile": "home"},
                  storage={"monitorEnabled": False})
        self.assertEqual(["Pro Max"], _plans(out["tree"]))
        body = text(out["tree"])
        self.assertNotIn("Rate limits", body)
        self.assertNotIn("Tier 9", body)

    def test_card_real_free_shape_keeps_free_status_without_paid_leak(self) -> None:
        # The signed-in Free shape: an explicit plan identity with no paid
        # bar. The compact card shows the authoritative status; the pane (not
        # the card summary) carries the full fact list.
        out = run(expr="QuotaCard({close(){}})", data=_cli_free_master_payload(),
                  billingData=_free_plan_billing(), storage={"monitorEnabled": False})
        body = text(out["value"])
        self.assertIn("Free", body)
        self.assertIn("Free tier - free models only", body)
        self.assertNotIn("Tier 9", body)
        self.assertNotIn("access depleted", body)
        self.assertNotIn("Rate limits", body)

    def test_pane_renders_window_plan_gauges(self) -> None:
        out = run(component="pane", data=_cli_payload(),
                  billingData=_window_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Pro Max"], _plans(out["tree"]))
        self.assertEqual(["Daily", "Weekly"], _windows(out["tree"]))
        body = text(out["tree"])
        self.assertIn("80% left", body)
        self.assertIn("25% left", body)
        self.assertNotIn("Tier 9", body)

    def test_pane_renders_live_plus_money_and_exact_window_labels(self) -> None:
        out = run(component="pane", data=_cli_plus_window_payload(),
                  billingData=_plus_window_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Hermes Plus"], _plans(out["tree"]))
        self.assertEqual(
            ["Daily", "Weekly", "Monthly", "Large models (Monthly)"],
            _windows(out["tree"]),
        )
        body = text(out["tree"])
        self.assertIn("Account balance", body)
        self.assertIn("$9.99", body)
        self.assertNotIn("Subscription credits", body)
        self.assertNotIn("Top-up credits", body)
        self.assertIn("Cycle ends: 2026-12-06T13:00:47.000Z", body)
        self.assertIn("Spend this period: $0.00", body)
        self.assertNotIn("no cap reported", body)
        self.assertNotIn("Rate limits", body)
        self.assertNotIn("Tool pool", body)
        self.assertNotIn("paid models via purchased credits", body)
        self.assertNotIn("credits still apply", body)
        self.assertEqual(1, body.count("Account balance"))
        self.assertNotIn("Top-up credits", body)

    def test_billing_supplies_a_row_when_the_cache_has_none(self) -> None:
        out = run(component="pane", billingData=_credit_billing(),
                  storage={"monitorEnabled": False})
        self.assertEqual(["Pro"], _plans(out["tree"]))
        body = text(out["tree"])
        self.assertIn("Account balance", body)
        self.assertIn("$26.00 USD", body)
        self.assertNotIn("subscription credits left", body)

    def test_status_chip_uses_billing_then_falls_back(self) -> None:
        primary = run(component="status", data=_cli_payload(used=5),
                      billingData=_credit_billing(), storage={"monitorEnabled": False})
        labels = [n["props"].get("aria-label", "") for n in nodes(primary["tree"])
                  if n.get("type") == "button"]
        self.assertTrue(any("Nous Portal 70%" in label for label in labels), labels)
        fallback = run(component="status", data=_cli_payload(used=5),
                       billingIsError=True, storage={"monitorEnabled": False})
        labels = [n["props"].get("aria-label", "") for n in nodes(fallback["tree"])
                  if n.get("type") == "button"]
        self.assertTrue(any("Nous Portal 95%" in label for label in labels), labels)

    def test_failure_keeps_the_cached_row_without_toast_or_log(self) -> None:
        out = run(component="pane", data=_cli_payload(),
                  billingIsError=True, storage={"monitorEnabled": False})
        self.assertEqual(["Tier 9"], _plans(out["tree"]))
        body = text(out["tree"])
        self.assertIn("stale cache detail", body)
        self.assertEqual([], out["toasts"])
        self.assertEqual([], out["logs"])
        self.assertEqual(1, len(_billing_queries(out)))

    def test_logged_out_billing_keeps_the_cached_row(self) -> None:
        # `logged_in:false` with no error is also core's BillingAuthError
        # shape, not proof of a sign-out: fall back to the cache.
        out = run(component="pane", data=_cli_payload(),
                  billingData={"ok": True, "logged_in": False,
                               "free_tier_account": False,
                               "usage": {"available": False},
                               "card": {"brand": "Visa", "last4": "9999"}},
                  storage={"monitorEnabled": False})
        body = text(out["tree"])
        self.assertIn("Tier 9", body)
        self.assertNotIn("Free", body)

    def test_unloadable_logged_out_billing_keeps_the_cached_row(self) -> None:
        # `error` marks a failed billing load, not a confirmed sign-out.
        out = run(component="pane", data=_cli_payload(),
                  billingData={"ok": True, "logged_in": False,
                               "free_tier_account": False,
                               "error": "could not load billing state",
                               "usage": {"available": False}},
                  storage={"monitorEnabled": False})
        self.assertEqual(["Tier 9"], _plans(out["tree"]))

    def test_bare_depleted_billing_keeps_the_richer_cached_row(self) -> None:
        # A bare status cannot prove exhaustion: keep the cached paid row and
        # never claim a top-up would restore access.
        out = run(component="pane", data=_cli_payload(),
                  billingData={"ok": True, "logged_in": True,
                               "usage": {"ok": True, "available": True,
                                         "status": "depleted"}},
                  storage={"monitorEnabled": False})
        body = text(out["tree"])
        self.assertIn("Tier 9", body)
        self.assertNotIn("access depleted", body)
        self.assertNotIn("top up to restore", body)
        self.assertEqual(["Tier 9"], _plans(out["tree"]))

    def test_member_cap_billing_keeps_the_cached_row(self) -> None:
        out = run(component="pane", data=_cli_payload(),
                  billingData=_member_cap_billing(),
                  storage={"monitorEnabled": False})
        body = text(out["tree"])
        self.assertIn("Tier 9", body)
        self.assertNotIn("access depleted", body)
        self.assertNotIn("top up to restore", body)

    def test_signed_in_free_billing_does_not_override_a_free_row(self) -> None:
        out = run(component="pane", data=_cli_free_payload(),
                  billingData=_signed_in_free_billing(),
                  storage={"monitorEnabled": False})
        body = text(out["tree"])
        self.assertEqual(["Free"], _plans(out["tree"]))
        self.assertNotIn("depleted", body.lower())
        self.assertNotIn("top up to restore", body.lower())

    def test_real_depleted_billing_replaces_a_stale_paid_row(self) -> None:
        out = run(component="pane", data=_cli_payload(),
                  billingData=_depleted_subscription_billing(),
                  storage={"monitorEnabled": False})
        body = text(out["tree"])
        self.assertNotIn("Tier 9", body)
        self.assertIn("Status: access depleted - top up to restore", body)
        self.assertEqual(["Pro"], _plans(out["tree"]))
        tones = _provider_tones(out["tree"], "nous")
        self.assertIn("bad", tones)
        self.assertNotIn("good", tones)

    def test_low_billing_balance_is_not_green(self) -> None:
        out = run(component="pane", data=_cli_payload(),
                  billingData={"ok": True, "logged_in": True,
                               "usage": {"ok": True, "available": True,
                                         "status": "low", "plan_name": "Plus",
                                         "total_spendable_display": "$3.40"}},
                  storage={"monitorEnabled": False})
        self.assertEqual(["Plus"], _plans(out["tree"]))
        self.assertIn("Low balance - top up soon", text(out["tree"]))
        tones = _provider_tones(out["tree"], "nous")
        self.assertNotIn("good", tones)

    def test_free_billing_is_green(self) -> None:
        out = run(component="pane", data=_cli_payload(),
                  billingData=_free_billing(),
                  storage={"monitorEnabled": False})
        self.assertEqual(["Free"], _plans(out["tree"]))
        self.assertIn("good", _provider_tones(out["tree"], "nous"))

    def test_footer_keeps_cache_age_and_labels_billing_freshness(self) -> None:
        # The cache age stays the cache age even when Billing is older; the
        # billing observation is shown separately instead of being folded
        # into the global `age_s`.
        older_billing = _cli_payload()
        older_billing["fetched_at"] = iso(-31)
        out = run(component="pane", data=older_billing,
                  billingData=_credit_billing(),
                  billingUpdatedAt=int((NOW - 120) * 1000),
                  storage={"monitorEnabled": False})
        body = text(out["tree"])
        self.assertIn("30s old · poll 600s", body)
        self.assertIn("Nous billing checked 2m ago", body)
        # Conversely a fresh billing read must not make a 10-minute-old cache
        # snapshot look current for every other provider.
        stale_cache = _cli_payload()
        stale_cache.update(age_s=600, fetched_at=iso(-601))
        out = run(component="pane", data=stale_cache,
                  billingData=_credit_billing(),
                  billingUpdatedAt=int((NOW - 1) * 1000),
                  storage={"monitorEnabled": False})
        body = text(out["tree"])
        self.assertIn("10m old", body)
        self.assertIn("Nous billing checked 1s ago", body)
        self.assertNotIn("0s old", body)

    def test_old_billing_read_does_not_mark_codex_stale(self) -> None:
        data = _cli_payload()
        data["age_s"] = 0
        data["providers"]["openai-codex"] = {
            "windows": [{"label": "Weekly", "used_percent": 6}],
        }
        out = run(component="status", data=data,
                  billingData=_credit_billing(),
                  billingUpdatedAt=int((NOW - 1000) * 1000),
                  storage={"monitorEnabled": False})
        labels = [n["props"].get("aria-label", "") for n in nodes(out["tree"])
                  if n.get("type") == "button"]
        self.assertTrue(any("OpenAI Codex 94%" in label for label in labels), labels)
        self.assertFalse(any("OpenAI Codex" in label and "stale" in label
                             for label in labels), labels)

    def test_billing_repaint_keeps_the_quota_freshness_revision(self) -> None:
        # One Billing poll rebuilds the merged payload. The freshness clock
        # must key on the quota-cache revision, not the merged wrapper, or a
        # stale cache stays "fresh" forever.
        out = run(
            expr="(() => {"
                 " const first = useMergedQuota();"
                 " const replay = useMergedQuota();"
                 " const workAnchor = quotaFreshnessAnchor(first.data);"
                 " const replayAnchor = quotaFreshnessAnchor(replay.data);"
                 " host.state.focusedSessionOwner.set({connectionId: 'local', profile: 'home'});"
                 " const home = useMergedQuota();"
                 " return {"
                 "  workRev: first.data.quota_revision,"
                 "  replayRev: replay.data.quota_revision,"
                 "  homeRev: home.data.quota_revision,"
                 "  workAnchor, replayAnchor, homeAnchor: quotaFreshnessAnchor(home.data),"
                 "  distinctWrappers: first.data !== replay.data,"
                 " };"
                 "})()",
            dataByProfile={"work": _cli_payload(), "home": _cli_payload()},
            billingByProfile={"work": _credit_billing(), "home": _credit_billing()},
            storage={"monitorEnabled": False},
        )
        value = out["value"]
        self.assertTrue(value["distinctWrappers"])
        self.assertEqual(value["workRev"], value["replayRev"])
        self.assertNotEqual(value["workRev"], value["homeRev"])
        self.assertEqual(value["workAnchor"], value["replayAnchor"])
        self.assertNotEqual(value["workAnchor"], value["homeAnchor"])

    def test_quota_error_flags_survive_when_billing_yields_no_row(self) -> None:
        # Billing unavailable is a fallback, not a primary row: the CLI error
        # must stay visible/truthful instead of being masked by the attempt.
        out = run(
            expr="(() => { const q = useMergedQuota();"
                 " return {isError: q.isError, isLoading: q.isLoading,"
                 " plan: q.data.providers.nous.plan}; })()",
            data=_cli_payload(), isError=True,
            billingData={"ok": True, "logged_in": True,
                         "usage": {"available": False}},
            storage={"monitorEnabled": False},
        )
        self.assertTrue(out["value"]["isError"])
        self.assertFalse(out["value"]["isLoading"])
        self.assertEqual("Tier 9", out["value"]["plan"])

    def test_quota_error_does_not_suppress_the_billing_row(self) -> None:
        out = run(component="status", data=_cli_payload(),
                  billingData=_credit_billing(), isError=True,
                  storage={"monitorEnabled": False})
        labels = [n["props"].get("aria-label", "") for n in nodes(out["tree"])
                  if n.get("type") == "button"]
        self.assertTrue(any("Nous Portal 70%" in label for label in labels), labels)

    def test_quota_loading_does_not_hide_billing(self) -> None:
        out = run(component="pane", data=None, quotaIsLoading=True,
                  billingData=_credit_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Pro"], _plans(out["tree"]))
        body = text(out["tree"])
        self.assertIn("Account balance", body)
        self.assertIn("$26.00 USD", body)

    def test_free_tier_overrides_a_paid_looking_cache(self) -> None:
        out = run(component="pane", data=_cli_payload(plan="Tier 9", used=5),
                  billingData=_free_billing(), storage={"monitorEnabled": False})
        self.assertEqual(["Free"], _plans(out["tree"]))
        self.assertEqual([], _windows(out["tree"]))
        body = text(out["tree"])
        self.assertIn("nous/welcome", body)
        self.assertNotIn("Tier 9", body)
        self.assertNotIn("Stale cache window", body)

    def test_signed_in_free_plan_overrides_a_depleted_paid_cache(self) -> None:
        out = run(component="pane", data=_cli_payload(plan="Tier 9", used=5),
                  billingData=_free_plan_billing(),
                  storage={"monitorEnabled": False})
        self.assertEqual(["Free"], _plans(out["tree"]))
        self.assertEqual([], _windows(out["tree"]))
        body = text(out["tree"])
        self.assertIn("Free tier - free models only", body)
        self.assertNotIn("Tier 9", body)
        self.assertNotIn("access depleted", body)

    def test_status_free_without_free_tier_is_not_a_plan_label(self) -> None:
        out = run(component="pane", data=_cli_payload(),
                  billingData={"ok": True, "logged_in": True,
                               "usage": {"ok": True, "available": True,
                                         "status": "free"}},
                  storage={"monitorEnabled": False})
        self.assertEqual(["Tier 9"], _plans(out["tree"]))
        self.assertNotIn("Free tier", text(out["tree"]))

    def test_profile_switch_paints_that_profiles_billing_only(self) -> None:
        work = _credit_billing()
        home = _window_billing()
        out_work = run(component="pane",
                       dataByProfile={"work": _cli_payload(), "home": _cli_payload()},
                       billingByProfile={"work": work, "home": home},
                       owner={"connectionId": "local", "profile": "work"},
                       storage={"monitorEnabled": False})
        self.assertEqual(["Pro"], _plans(out_work["tree"]))
        out_home = run(component="pane",
                       dataByProfile={"work": _cli_payload(), "home": _cli_payload()},
                       billingByProfile={"work": work, "home": home},
                       owner={"connectionId": "local", "profile": "home"},
                       storage={"monitorEnabled": False})
        self.assertEqual(["Pro Max"], _plans(out_home["tree"]))
        self.assertEqual(["Daily", "Weekly"], _windows(out_home["tree"]))
        self.assertEqual(["quota", "billing", "local", "home"],
                         _billing_queries(out_home)[0]["key"])
        # A profile with no billing payload must not inherit the other
        # profile's row from a previous render/query.
        out_missing = run(component="pane",
                          dataByProfile={"work": _cli_payload(), "home": _cli_payload()},
                          billingByProfile={"work": work},
                          owner={"connectionId": "local", "profile": "home"},
                          storage={"monitorEnabled": False})
        self.assertEqual(["Tier 9"], _plans(out_missing["tree"]))
        self.assertNotIn("Pro", text(out_missing["tree"]))

    def test_no_focused_owner_means_disabled_billing_query(self) -> None:
        out = run(component="pane", noScope=True,
                  data=_cli_payload(), billingData=_credit_billing(),
                  storage={"monitorEnabled": False})
        self.assertEqual(["Tier 9"], _plans(out["tree"]))
        queries = _billing_queries(out)
        self.assertEqual(1, len(queries))
        self.assertFalse(queries[0]["enabled"])
        self.assertEqual(["quota", "billing", None, None], queries[0]["key"])

    def test_hidden_status_bar_disables_the_billing_query(self) -> None:
        out = run(component="status", data=_cli_payload(),
                  billingData=_credit_billing(),
                  storage={"showStatusBar": False})
        queries = _billing_queries(out)
        self.assertEqual(1, len(queries))
        self.assertFalse(queries[0]["enabled"])

    def test_hidden_agents_chip_disables_the_billing_query(self) -> None:
        # The agents chip runs the same quota hook but renders nothing when the
        # bar is hidden or there is no monitor snapshot. Neither case may keep
        # a Nous portal read alive.
        hidden_bar = run(component="agents", data=_cli_payload(),
                         billingData=_credit_billing(), monitor=snapshot(),
                         storage={"showStatusBar": False})
        queries = _billing_queries(hidden_bar)
        self.assertEqual(1, len(queries))
        self.assertFalse(queries[0]["enabled"])
        no_snapshot = run(component="agents", data=_cli_payload(),
                          billingData=_credit_billing(),
                          storage={"monitorEnabled": True})
        queries = _billing_queries(no_snapshot)
        self.assertEqual(1, len(queries))
        self.assertFalse(queries[0]["enabled"])

    def test_unavailable_nous_windows_do_not_win_the_worst_chip(self) -> None:
        # A cached positive window can survive after access was revoked.
        # Its state, not the stale percentage, decides ranking.
        data = _cli_payload()
        data["providers"]["nous"] = {
            "label": "nous",
            "plan": "Tier 9",
            "windows": [{"label": "Subscription", "used_percent": 5}],
            "details": ["Paid access is unavailable"],
            "access_state": "unavailable",
            "api_calls_available": None,
        }
        data["providers"]["anthropic"] = {
            "windows": [{"label": "Weekly", "used_percent": 20}],
        }
        out = run(component="status", data=data,
                  storage={"monitorEnabled": False, "statusbarMode": "worst"})
        body = text(out["tree"])
        self.assertIn("Anthropic 80%", body)
        self.assertNotIn("Nous Portal 95%", body)

    def test_window_gauges_still_win_when_dollar_usage_is_unavailable(self) -> None:
        # `usage.available:false` is the dollar-credit model's verdict; the
        # plan's own gauges are authoritative and remain rankable.
        billing = _window_billing()
        billing["usage"] = {"ok": True, "available": False}
        data = _cli_payload()
        data["providers"]["anthropic"] = {
            "windows": [{"label": "Weekly", "used_percent": 50}],
        }
        out = run(component="status", data=data, billingData=billing,
                  storage={"monitorEnabled": False, "statusbarMode": "worst"})
        body = text(out["tree"])
        self.assertIn("Nous Portal 25%", body)
        self.assertNotIn("Anthropic 50%", body)

    def test_window_gauges_render_when_dollar_usage_is_unavailable(self) -> None:
        billing = _window_billing()
        billing["usage"] = {"ok": True, "available": False}
        out = run(component="pane", data=_cli_payload(), billingData=billing,
                  storage={"monitorEnabled": False})
        self.assertEqual(["Pro Max"], _plans(out["tree"]))
        self.assertEqual(["Daily", "Weekly"], _windows(out["tree"]))

    def test_paused_window_billing_is_not_ranked_and_shows_status(self) -> None:
        data = _cli_payload()
        data["providers"]["anthropic"] = {
            "windows": [{"label": "Weekly", "used_percent": 50}],
        }
        out = run(component="status", data=data,
                  billingData=_paused_window_billing(),
                  storage={"monitorEnabled": False, "statusbarMode": "worst"})
        body = text(out["tree"])
        self.assertIn("Anthropic 50%", body)
        self.assertNotIn("Nous Portal 20%", body)
        pane = run(component="pane", data=_cli_payload(),
                   billingData=_paused_window_billing(),
                   storage={"monitorEnabled": False})
        self.assertIn("Status: included usage paused - subscription payment failed",
                      text(pane["tree"]))

    def test_free_billing_has_a_named_worst_mode_chip(self) -> None:
        # A free account has no percentage window; worst mode must show its
        # confirmed entitlement instead of a meaningless "Q:none".
        out = run(component="status", data=_cli_payload(),
                  billingData=_free_billing(),
                  storage={"monitorEnabled": False, "statusbarMode": "worst"})
        body = text(out["tree"])
        self.assertIn("Nous Portal", body)
        self.assertIn("Free", body)
        self.assertNotIn("Q:none", body)

    def test_disabled_nous_disables_the_billing_query(self) -> None:
        out = run(component="status", data=_cli_payload(),
                  billingData=_credit_billing(), disabled=["nous"],
                  storage={"monitorEnabled": False})
        queries = _billing_queries(out)
        self.assertEqual(1, len(queries))
        self.assertFalse(queries[0]["enabled"])
        labels = [n["props"].get("aria-label", "") for n in nodes(out["tree"])
                  if n.get("type") == "button"]
        self.assertFalse(any("Nous" in label for label in labels), labels)
        pane = run(component="pane", data=_cli_payload(),
                   billingData=_credit_billing(), disabled=["nous"],
                   storage={"monitorEnabled": False})
        pane_queries = _billing_queries(pane)
        self.assertEqual(1, len(pane_queries))
        self.assertFalse(pane_queries[0]["enabled"])

    def test_card_uses_billing_and_never_stores_private_fields(self) -> None:
        snapshot = _cli_payload()
        out = run(expr="QuotaCard({close(){}})", data=snapshot,
                  billingData=_credit_billing(),
                  storage={"monitorEnabled": False,
                           "lastPayload:local:work": json.dumps(snapshot)})
        body = text(out["value"])
        self.assertIn("Pro", body)
        self.assertNotIn("Tier 9", body)
        stored = json.dumps(out["storage"])
        self.assertNotIn("9999", stored)
        self.assertNotIn("SECRET-ORG", stored)
        self.assertNotIn("payment_method", stored)
        self.assertNotIn('"card"', stored)
        self.assertNotIn("billing", stored)

    def test_card_labels_billing_freshness_separately(self) -> None:
        out = run(expr="QuotaCard({close(){}})", data=_cli_payload(),
                  billingData=_credit_billing(),
                  billingUpdatedAt=int((NOW - 120) * 1000),
                  storage={"monitorEnabled": False})
        body = text(out["value"])
        self.assertIn("Nous billing checked 2m ago", body)
        self.assertIn("checked 30s ago", body)

    def test_card_includes_a_free_plan_row(self) -> None:
        out = run(expr="QuotaCard({close(){}})", data=_cli_payload(),
                  billingData=_free_plan_billing(),
                  storage={"monitorEnabled": False})
        self.assertIn("nous", _card_providers(out["value"]))
        body = text(out["value"])
        self.assertIn("Free", body)
        self.assertIn("Free tier - free models only", body)
        self.assertNotIn("Tier 9", body)

    def test_card_includes_a_depleted_row_with_its_status(self) -> None:
        out = run(expr="QuotaCard({close(){}})", data=_cli_payload(),
                  billingData=_depleted_subscription_billing(),
                  storage={"monitorEnabled": False})
        self.assertIn("nous", _card_providers(out["value"]))
        body = text(out["value"])
        self.assertIn("Status: access depleted - top up to restore", body)
        self.assertNotIn("Tier 9", body)

    def test_card_includes_a_plan_only_cached_row(self) -> None:
        # No window and no balance: the plan identity alone still earns a row.
        data = _cli_payload()
        data["providers"]["nous"] = {
            "label": "nous",
            "plan": "Tier 9",
            "windows": [],
            "details": [],
            "access_state": None,
            "api_calls_available": None,
        }
        out = run(expr="QuotaCard({close(){}})", data=data,
                  storage={"monitorEnabled": False})
        self.assertIn("nous", _card_providers(out["value"]))
        self.assertIn("Tier 9", text(out["value"]))

    def test_chip_and_pane_share_one_billing_query_key(self) -> None:
        out = run(expr="[QuotaPane({}), StatusBar()]", data=_cli_payload(),
                  billingData=_credit_billing(), storage={"monitorEnabled": False})
        queries = _billing_queries(out)
        self.assertEqual(2, len(queries))
        self.assertEqual({tuple(q["key"]) for q in queries},
                         {("quota", "billing", "local", "work")})
        self.assertTrue(all(q["enabled"] for q in queries))


class PausedCreditsRenderTests(unittest.TestCase):
    """A paused plan and paid-call availability are two separate facts.

    Billing's `credits_pay` answer decides whether purchased credits keep
    serving API calls while the included plan is paused. The renderer must not
    paint that row green (healthy included access) or red (calls unusable and
    contradicting the chip's "available"), and it must never rank the paused
    plan's own windows as remaining included quota.
    """

    STORAGE = {"monitorEnabled": False}

    def _render(self, credits_pay):
        billing = _paused_credits_billing(credits_pay)
        status = run(component="status", data=_cli_payload(),
                     billingData=billing, storage=self.STORAGE)
        card = run(expr="QuotaCard({close(){}})", data=_cli_payload(),
                   billingData=billing, storage=self.STORAGE)
        pane = run(component="pane", data=_cli_payload(),
                   billingData=billing, storage=self.STORAGE)
        return status, card, pane

    def test_purchased_credits_keep_calls_available_as_a_warning(self) -> None:
        status, card, pane = self._render(True)
        chip = _chip_button(status["tree"])
        self.assertIn("Nous Portal available", chip["props"]["aria-label"])
        self.assertEqual("var(--ui-yellow)", _chip_dot_color(chip))
        self.assertNotEqual("var(--ui-red)", _chip_dot_color(chip))
        self.assertNotEqual("var(--ui-green)", _chip_dot_color(chip))
        tip = tooltip_text(status["tree"])
        self.assertIn("API calls available: yes", tip)
        self.assertIn("purchased credits still apply", tip)

        status_node = _card_status_node(card["value"])
        self.assertEqual("var(--ui-yellow)", status_node["props"]["style"]["color"])
        self.assertIn("purchased credits still apply",
                      str(status_node["props"]["children"]))
        self.assertIn("Pro Max", text(card["value"]))
        # The paused plan's gauges are not remaining included quota.
        self.assertEqual([], _prop_nodes(card["value"], "data-card-window"))

        tones = _provider_tones(pane["tree"], "nous")
        self.assertIn("warn", tones)
        self.assertNotIn("bad", tones)
        self.assertNotIn("good", tones)
        self.assertIn("API calls available: yes", text(pane["tree"]))
        self.assertEqual([], _prop_nodes(pane["tree"], "data-window"))

    def test_explicit_denial_is_unavailable_and_red(self) -> None:
        status, card, pane = self._render(False)
        chip = _chip_button(status["tree"])
        self.assertTrue(chip["props"]["aria-label"].startswith("Nous Portal unavailable"),
                        chip["props"]["aria-label"])
        self.assertEqual("var(--ui-red)", _chip_dot_color(chip))
        self.assertIn("API calls available: no", tooltip_text(status["tree"]))
        self.assertNotIn("purchased credits still apply", tooltip_text(status["tree"]))

        status_node = _card_status_node(card["value"])
        self.assertEqual("var(--ui-red)", status_node["props"]["style"]["color"])
        self.assertNotIn("purchased credits still apply",
                         text(card["value"]))
        self.assertEqual([], _prop_nodes(card["value"], "data-card-window"))

        tones = _provider_tones(pane["tree"], "nous")
        self.assertIn("bad", tones)
        self.assertNotIn("warn", tones)
        self.assertNotIn("good", tones)
        self.assertIn("API calls available: no", text(pane["tree"]))
        self.assertEqual([], _prop_nodes(pane["tree"], "data-window"))

    def test_unknown_continuation_warns_without_claiming_availability(self) -> None:
        status, card, pane = self._render(None)
        chip = _chip_button(status["tree"])
        # No credits_pay answer: the chip falls back to the plan name, not a
        # claim that paid calls are available.
        self.assertTrue(chip["props"]["aria-label"].startswith("Nous Portal Pro Max"),
                        chip["props"]["aria-label"])
        self.assertNotIn("available", chip["props"]["aria-label"])
        self.assertEqual("var(--ui-yellow)", _chip_dot_color(chip))
        self.assertNotEqual("var(--ui-red)", _chip_dot_color(chip))
        self.assertNotIn("API calls available", tooltip_text(status["tree"]))
        self.assertNotIn("purchased credits still apply", tooltip_text(status["tree"]))

        status_node = _card_status_node(card["value"])
        self.assertEqual("var(--ui-yellow)", status_node["props"]["style"]["color"])
        self.assertIn("paused", str(status_node["props"]["children"]).lower())
        self.assertNotIn("purchased credits still apply", text(card["value"]))
        self.assertEqual([], _prop_nodes(card["value"], "data-card-window"))

        tones = _provider_tones(pane["tree"], "nous")
        self.assertIn("warn", tones)
        self.assertNotIn("bad", tones)
        self.assertNotIn("good", tones)
        self.assertNotIn("API calls available", text(pane["tree"]))
        self.assertEqual([], _prop_nodes(pane["tree"], "data-window"))


class BillingDisclosureTests(unittest.TestCase):
    def test_manifest_does_not_claim_the_widget_never_uses_the_network(self) -> None:
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertNotIn("never does network I/O", manifest)
        self.assertIn("billing.state", manifest)
        self.assertIn("may query Nous Portal", manifest)

    def test_manifest_discloses_rule_9_and_does_not_overclaim_support(self) -> None:
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("catalog rule 9", manifest)
        self.assertIn("hermes_cli.nous_account", manifest)
        self.assertIn("not a documented plugin API", manifest)
        # The old text claimed a method that does not exist and an unavailable
        # state the merge never produces. Both were false.
        self.assertNotIn("billing-desktop-only", manifest)
        self.assertNotIn("public, read-only", manifest)
        self.assertNotIn("older gateway shows the card's unavailable state", manifest)

    def test_readme_and_security_disclose_the_rule_9_coupling(self) -> None:
        for name in ("README.md", "docs/security.md", "docs/providers.md"):
            with self.subTest(name=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("rule 9", text.lower())
                self.assertIn("hermes_cli.nous_account", text)

    def test_user_docs_describe_the_billing_network_path(self) -> None:
        for name in ("README.md", "docs/security.md", "docs/usage.md",
                     "docs/providers.md"):
            with self.subTest(name=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("Nous Portal", text)
        security = (ROOT / "docs/security.md").read_text(encoding="utf-8")
        self.assertIn("gateway may contact Nous Portal", security)

    def test_docs_publish_the_nous_tier_table_with_its_source(self) -> None:
        providers = (ROOT / "docs/providers.md").read_text(encoding="utf-8")
        self.assertIn("https://portal.nousresearch.com/api-docs", providers)
        for row in ("| Free | 50 | 500,000 |", "| Basic | 400 | 2,000,000 |",
                    "| Plus | 400 | 4,000,000 |", "| Super | 800 | 8,000,000 |",
                    "| Scale | 600 | 6,000,000 |", "| Ultra | 1,600 | 16,000,000 |",
                    "| Default paid users | 180 | 720,000 |"):
            self.assertIn(row, providers)
        self.assertIn("invents an RPH", providers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
