/**
 * Hermes desktop widget for the `quota` plugin.
 *
 * Status bar:
 *   - 'all' mode: one chip per configured provider, side by side.
 *   - 'worst' mode: single chip with worst remaining % + tonal bar.
 *   - Both respect "Show status bar" setting.
 *
 * Route /quota (full pane) + docked right pane + sidebar nav.
 *
 * Data path: host.request('cli.exec', {argv: ['quota', 'status', '--json']})
 *   → Hermes CLI → quota_cache.json (offline, fast).
 * Refresh: host.request('cli.exec', {argv: ['quota', 'refresh']}).
 *
 * SINGLE SOURCE FILE: keep only at <hermes home>/desktop-plugins/quota/plugin.js.
 * Do NOT also place a copy under profiles/<name>/desktop-plugins/quota.
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls. Only these imports
 * resolve: @hermes/plugin-sdk, react, react/jsx-runtime. `ctx` comes from
 * register(ctx), not from an import.
 */

import {
	atom,
	cn,
	fmtDayTime,
	host,
	Input,
	icons,
	PANES_AREA,
	ROUTES_AREA,
	SegmentedControl,
	SIDEBAR_NAV_AREA,
	STATUSBAR_AREAS,
	StatusDot,
	Switch,
	useMutation,
	usePluginI18n,
	useQuery,
	useQueryClient,
	useValue,
} from "@hermes/plugin-sdk";
import { useEffect, useMemo, useRef, useState } from "react";
import { jsx, jsxs } from "react/jsx-runtime";

const ID = "quota";

// Module-level ctx handle (set in register). The data hook below needs it.
let CTX = null;

// ---- persisted settings (ctx.storage + useValue atoms) -------------------

const RESET_FORMAT_KEY = "resetFormat"; // 'relative' | 'absolute'
const RESET_FORMAT_DEFAULT = "relative";

const resetFormatAtom = atom(RESET_FORMAT_DEFAULT);

function readStored(key, fallback) {
	try {
		const v = CTX.storage.get(key);
		return v == null ? fallback : v;
	} catch {
		return fallback;
	}
}

function applyStored(key, atomRef, allowed, fallback) {
	try {
		const v = CTX.storage.get(key);
		atomRef.set(allowed.includes(v) ? v : fallback);
	} catch {
		/* noop */
	}
}

function setStored(key, value, atomRef) {
	try {
		CTX.storage.set(key, value);
		atomRef.set(value);
	} catch {
		/* noop */
	}
}

function applyStoredResetFormat() {
	applyStored(
		RESET_FORMAT_KEY,
		resetFormatAtom,
		["relative", "absolute"],
		RESET_FORMAT_DEFAULT,
	);
}

function setResetFormat(fmt) {
	setStored(RESET_FORMAT_KEY, fmt, resetFormatAtom);
}

// Cherry-picker: list of disabled provider IDs (persisted).
const DISABLED_PROVIDERS_KEY = "disabledProviders";
const DISABLED_PROVIDERS_DEFAULT = []; // empty = all enabled

const disabledProvidersAtom = atom(DISABLED_PROVIDERS_DEFAULT);

function applyStoredDisabledProviders() {
	try {
		const v = CTX.storage.get(DISABLED_PROVIDERS_KEY);
		disabledProvidersAtom.set(
			Array.isArray(v) ? v : DISABLED_PROVIDERS_DEFAULT,
		);
	} catch {
		/* noop */
	}
}

function setDisabledProviders(list) {
	setStored(DISABLED_PROVIDERS_KEY, list, disabledProvidersAtom);
}

function isProviderEnabled(pid) {
	const disabled = disabledProvidersAtom.get();
	return !disabled.includes(pid);
}

// Surface toggles: status bar on by default; docked pane off by default.
const SHOW_STATUSBAR_KEY = "showStatusBar";
const SHOW_STATUSBAR_DEFAULT = true;
const SHOW_DOCKED_PANE_KEY = "showDockedPane";
const SHOW_DOCKED_PANE_DEFAULT = false;

const showStatusBarAtom = atom(SHOW_STATUSBAR_DEFAULT);
const showDockedPaneAtom = atom(SHOW_DOCKED_PANE_DEFAULT);

function applyStoredBoolean(key, atomRef, fallback) {
	try {
		atomRef.set(readStored(key, fallback) === true);
	} catch {
		/* noop */
	}
}

function applyStoredSurfaceVisibility() {
	applyStoredBoolean(
		SHOW_STATUSBAR_KEY,
		showStatusBarAtom,
		SHOW_STATUSBAR_DEFAULT,
	);
	applyStoredBoolean(
		SHOW_DOCKED_PANE_KEY,
		showDockedPaneAtom,
		SHOW_DOCKED_PANE_DEFAULT,
	);
}

function setShowStatusBar(v) {
	setStored(SHOW_STATUSBAR_KEY, !!v, showStatusBarAtom);
}

function setShowDockedPane(v) {
	setStored(SHOW_DOCKED_PANE_KEY, !!v, showDockedPaneAtom);
	if (showDockedPaneAtom.get()) {
		registerDockedPane();
	} else {
		clearDockedPane();
	}
}

// statusbarMode: 'all' renders every configured provider side by side;
// 'worst' renders the single worst-provider chip (previous behaviour).
const STATUSBAR_MODE_KEY = "statusbarMode";
const STATUSBAR_MODE_DEFAULT = "all";

const statusbarModeAtom = atom(STATUSBAR_MODE_DEFAULT);

function applyStoredStatusbarMode() {
	applyStored(
		STATUSBAR_MODE_KEY,
		statusbarModeAtom,
		["all", "worst"],
		STATUSBAR_MODE_DEFAULT,
	);
}

function setStatusbarMode(mode) {
	setStored(STATUSBAR_MODE_KEY, mode, statusbarModeAtom);
}

// refreshInterval: statusbar/pane poll cadence in seconds (persisted as number).
const REFRESH_INTERVAL_KEY = "refreshInterval";
const REFRESH_INTERVAL_DEFAULT = 60;
const REFRESH_INTERVAL_MIN = 15;
const REFRESH_INTERVAL_MAX = 600;

const refreshIntervalAtom = atom(REFRESH_INTERVAL_DEFAULT);

function applyStoredRefreshInterval() {
	try {
		const v = CTX.storage.get(REFRESH_INTERVAL_KEY);
		const n = Number(v);
		const clamped = isNaN(n)
			? REFRESH_INTERVAL_DEFAULT
			: Math.max(
					REFRESH_INTERVAL_MIN,
					Math.min(REFRESH_INTERVAL_MAX, Math.round(n)),
				);
		refreshIntervalAtom.set(clamped);
	} catch {
		/* noop */
	}
}

function setRefreshInterval(seconds) {
	const n = Number(seconds);
	const clamped = isNaN(n)
		? REFRESH_INTERVAL_DEFAULT
		: Math.max(
				REFRESH_INTERVAL_MIN,
				Math.min(REFRESH_INTERVAL_MAX, Math.round(n)),
			);
	setStored(REFRESH_INTERVAL_KEY, clamped, refreshIntervalAtom);
}

// Apply every persisted setting from storage on plugin load.
function applyStoredAll() {
	applyStoredResetFormat();
	applyStoredSurfaceVisibility();
	applyStoredStatusbarMode();
	applyStoredRefreshInterval();
	applyStoredDisabledProviders();
}

// ---- provider identity (names from the model picker) -----------------------

// Provider icon SVGs from @lobehub/icons (https://lobehub.com/icons), MIT.
// Inlined as path data so the widget renders offline — no CDN fetch at render.
// Providers without an entry fall back to a text monogram badge.
const PROVIDER_SVGS = {
	anthropic: {
		viewBox: "0 0 24 24",
		body: '<path d="M13.827 3.52h3.603L24 20h-3.603l-6.57-16.48zm-7.258 0h3.767L16.906 20h-3.674l-1.343-3.461H5.017l-1.344 3.46H0L6.57 3.522zm4.132 9.959L8.453 7.687 6.205 13.48H10.7z"></path>',
	},
	"openai-codex": {
		viewBox: "0 0 24 24",
		body: '<path d="M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z"></path>',
	},
	openrouter: {
		viewBox: "0 0 24 24",
		body: '<path d="M18.654 3.87a5.087 5.087 0 110 10.174L23.7 19.09c.64.641.187 1.737-.72 1.737H8.48a8.479 8.479 0 010-16.958h10.175zM8.479 7.26a5.087 5.087 0 100 10.176 5.087 5.087 0 000-10.175z"></path>',
	},
	gemini: {
		viewBox: "0 0 24 24",
		body: '<path d="M20.616 10.835a14.147 14.147 0 01-4.45-3.001 14.111 14.111 0 01-3.678-6.452.503.503 0 00-.975 0 14.134 14.134 0 01-3.679 6.452 14.155 14.155 0 01-4.45 3.001c-.65.28-1.318.505-2.002.678a.502.502 0 000 .975c.684.172 1.35.397 2.002.677a14.147 14.147 0 014.45 3.001 14.112 14.112 0 013.679 6.453.502.502 0 00.975 0c.172-.685.397-1.351.677-2.003a14.145 14.145 0 013.001-4.45 14.113 14.113 0 016.453-3.678.503.503 0 000-.975 13.245 13.245 0 01-2.003-.678z"></path>',
	},
	kimi: {
		viewBox: "0 0 24 24",
		body: '<path d="M21.846 0a1.923 1.923 0 110 3.846H20.15a.226.226 0 01-.227-.226V1.923C19.923.861 20.784 0 21.846 0z"></path><path d="M11.065 11.199l7.257-7.2c.137-.136.06-.41-.116-.41H14.3a.164.164 0 00-.117.051l-7.82 7.756c-.122.12-.302.013-.302-.179V3.82c0-.127-.083-.23-.185-.23H3.186c-.103 0-.186.103-.186.23V19.77c0 .128.083.23.186.23h2.69c.103 0 .186-.102.186-.23v-3.25c0-.069.025-.135.069-.178l2.424-2.406a.158.158 0 01.205-.023l6.484 4.772a7.677 7.677 0 003.453 1.283c.108.012.2-.095.2-.23v-3.06c0-.117-.07-.212-.164-.227a5.028 5.028 0 01-2.027-.807l-5.613-4.064c-.117-.078-.132-.279-.028-.381z"></path>',
	},
	grok: {
		viewBox: "0 0 24 24",
		body: '<path d="M6.469 8.776L16.512 23h-4.464L2.005 8.776H6.47zm-.004 7.9l2.233 3.164L6.467 23H2l4.465-6.324zM22 2.582V23h-3.659V7.764L22 2.582zM22 1l-9.952 14.095-2.233-3.163L17.533 1H22z"></path>',
	},
	nous: {
		viewBox: "0 0 24 24",
		body: ' <path d="M5.938 12.835c.127-.039.285.02.373.143.028.038.036.092.046.14.003.014-.02.033-.04.05-.124-.098-.24-.194-.354-.291-.011-.01-.016-.027-.025-.042zM8.396 9.412c.195-.032.39-.06.588-.05a.54.54 0 01.148.026c.202.071.402.147.601.224.028.01.05.036.075.055l-.013.027a9.203 9.203 0 01-.26-.089c-.115-.038-.213-.077-.315-.098-.25-.05-.25-.046-.292-.014l.574.144c.275.139.55.276.823.417.042.022.09.057.107.098.026.06.063.076.117.072.066-.006.132-.017.213-.027l-.04.086c.051.08.142.02.216.064-.074.13-.247.09-.334.199l.061.074-.12.087c0 .106-.038.168-.306.243l.026.085-.196.042.07.124h-.25l-.007.137c-.081-.01-.161-.018-.244-.027l-.053.123c-.027-.008-.052-.011-.073-.023-.067-.038-.128-.056-.195.006-.019.017-.063.014-.093.008-.026-.006-.05-.029-.07-.042-.11.095-.11.095-.208.003-.057.046-.12.074-.186.011-.063.027-.123-.02-.178-.014-.07.007-.097-.035-.133-.07l-.13.033c-.013-.236-.194-.19-.34-.203.005-.072.05-.092.095-.094a.474.474 0 01.159.022c.164.05.32.12.496.138.203.021.405.029.601-.015.265-.059.52-.149.707-.365.049-.056.083-.127.117-.195.019-.038.02-.084-.02-.116a1.397 1.397 0 00-.382-.217c.024.12-.031.182-.115.221 0 .014-.004.025 0 .03.08.115.084.16-.007.267a1.39 1.39 0 01-.218.211.477.477 0 01-.641-.05 1.36 1.36 0 01-.133-.152c-.078-.107-.076-.108-.033-.236-.165-.08-.128-.226-.104-.364.008-.05.028-.096.049-.163-.04.014-.067.017-.087.032a.897.897 0 00-.316.357c-.007.016-.01.034-.02.047-.012.015-.034.038-.045.035-.02-.006-.037-.027-.05-.045-.008-.012-.007-.032-.012-.057h-.126l.053-.172a14.82 14.82 0 00-.039-.049l.11-.284c-.06.026-.091.044-.124.051-.03.007-.064 0-.095 0 0-.031-.01-.07.004-.092.149-.22.305-.428.593-.476z" /><path d="M8.06 10.788c-.003-.038-.004-.075.037-.062.016.006.034.048.028.067-.01.04-.038.032-.064-.005z" /><path clip-rule="evenodd" d="M11.981.009c.226-.012.453-.011.679 0 .247.01.495.024.74.062.401.064.798.157 1.19.273.463.138.92.299 1.356.511a7.31 7.31 0 012.948 2.642c.292.469.536.963.739 1.479.219.556.446 1.11.623 1.683.204.654.329 1.326.458 1.997.097.504.182 1.01.29 1.511.156.722.329 1.44.494 2.16.186.812.4 1.615.63 2.415.102.355.193.713.282 1.072.11.436.202.876.254 1.323.031.278.066.557.073.837a7.56 7.56 0 01-.017.88c-.037.413-.1.818-.226 1.212a5.017 5.017 0 01-.915 1.649l-.13.156.018.023c.043-.023.088-.041.127-.068.2-.138.373-.307.531-.49.4-.46.721-.973.975-1.529a3.59 3.59 0 00.325-1.72c-.024-.424-.097-.834-.3-1.213-.013-.027-.015-.06-.03-.121.05.035.082.048.101.072.107.13.22.258.315.398.33.494.46 1.052.486 1.64a3.75 3.75 0 01-.47 1.97c-.36.655-.887 1.14-1.526 1.506-.193.111-.394.21-.595.308-.157.078-.248.211-.318.365a.522.522 0 00-.033.406.359.359 0 01.013.139c-.005.077-.077.155-.14.162-.054.006-.125-.043-.15-.116a1.206 1.206 0 01-.06-.233c-.04-.314-.155-.6-.308-.87a3.906 3.906 0 00-.73-.91 2.129 2.129 0 00-.897-.524 4.093 4.093 0 00-.692-.131c-.075-.008-.15-.04-.22.01.18.06.363.11.538.18.434.173.82.43 1.18.728.308.255.58.543.794.884.098.155.186.315.227.496.027.123.042.25.067.375.013.062-.002.109-.053.144-.047.033-.122.034-.163-.01a.455.455 0 01-.08-.14c-.03-.073-.038-.159-.078-.225a7.314 7.314 0 00-1.423-1.664c-.16-.137-.329-.26-.537-.323-.376-.114-.753-.203-1.15-.154-.213.025-.427.032-.64.053a1.6 1.6 0 00-.736.278 5.14 5.14 0 00-.834.72c-.329.342-.642.699-.955 1.055-.136.155-.264.319-.314.531a5.227 5.227 0 00-.012.051.096.096 0 01-.09.076h-.31c-.046 0-.082-.048-.072-.094.023-.108.045-.216.07-.324.075-.325.19-.635.368-.917.024-.039.04-.088.104-.08l.01.049.027.077c.28-.435.571-.834.996-1.135.283-.204.584-.378.89-.55a.196.196 0 00-.098-.002c-.162.043-.325.084-.485.134-.402.124-.764.33-1.11.566-.147.1-.298.193-.414.333a7.314 7.314 0 00-1.07 1.767.845.845 0 00-.04.12.075.075 0 01-.072.056h-.494c-.04 0-.062-.051-.036-.082.123-.14.246-.282.377-.415.275-.281.58-.532.777-.884.027-.048.063-.09.095-.135.238-.333.54-.607.818-.902.082-.086.175-.16.26-.24.029-.027.053-.057.079-.085l-.018-.025-.135.041c-.034.017-.07.031-.102.05-.248.144-.494.292-.743.433-.408.23-.825.439-1.209.711-.281.2-.591.358-.889.533-.02.012-.044.015-.08.028-.015-.135.143-.201.108-.336-.033.014-.064.02-.085.038-.111.096-.227.19-.328.296-.148.157-.284.325-.425.488-.125.143-.25.286-.373.431A.153.153 0 019.89 24H8.762a.316.316 0 00.016-.042c.028-.09.085-.172.083-.28-.091-.018-.162.001-.212.077a4.45 4.45 0 00-.136.215c-.01.016-.024.03-.042.03h-.093c-.019 0-.029-.022-.017-.037.071-.088.14-.178.209-.268.001-.002-.006-.012-.012-.024-.014.004-.03.006-.045.013-.176.09-.352.181-.527.274a.363.363 0 01-.168.042H5.202c-.026 0-.039-.036-.019-.053.21-.178.402-.374.558-.605.335-.496.538-1.047.667-1.629.004-.02-.003-.043-.006-.091-.037.048-.059.072-.076.1a1.943 1.943 0 01-.334.415c-.28.258-.59.448-.983.464-.297.012-.588 0-.865-.127-.46-.21-.722-.57-.794-1.072-.025-.17-.017-.171-.182-.219A3.513 3.513 0 011.97 20.6a2.286 2.286 0 01-.808-1.13 3.569 3.569 0 01-.16-1.245c.002-.034.016-.067.024-.1.032.023.046.043.05.066.033.153.059.308.096.46.086.355.257.664.516.92.258.256.571.419.91.532.358.118.717.138 1.07-.016a1.89 1.89 0 00.621-.452c.328-.348.533-.76.648-1.223.009-.034.005-.071.007-.11-.015.006-.026.006-.03.011-.031.05-.064.1-.093.152-.284.502-.679.887-1.196 1.135-.351.17-.718.255-1.11.159a1.607 1.607 0 01-.971-.64 2.006 2.006 0 01-.368-.924 2.903 2.903 0 01.02-.886c.05-.439.466-1.17.742-1.271-.02.063-.035.112-.053.16-.043.116-.097.227-.13.345a1.901 1.901 0 00-.05.82c.033.212.09.416.204.6.147.236.346.407.62.465.11.023.225.014.338.018a.576.576 0 00.386-.131c.164-.128.282-.292.366-.481.168-.375.24-.777.309-1.179.05-.296.093-.594.133-.893.039-.281.071-.563.104-.845.026-.232.048-.464.074-.696.024-.228.052-.455.076-.683.024-.227.047-.455.069-.683.013-.14.022-.28.034-.42l.037-.417c.022-.25.041-.5.065-.748.008-.082-.02-.132-.09-.177a2.46 2.46 0 01-.492-.418c-.1-.109-.188-.228-.282-.342-.035-.042-.056-.097-.116-.118a2.084 2.084 0 00.275.597c.06.092.131.176.196.265.063.086.182.115.234.226-.028.003-.046.01-.06.006a4.74 4.74 0 01-.22-.057 2.71 2.71 0 01-1.287-.819c-.435-.487-.656-1.076-.71-1.723a5.206 5.206 0 01.014-1.06c.072-.602.22-1.186.45-1.745.155-.376.338-.741.526-1.102.205-.393.466-.75.765-1.076.512-.559 1.104-1.024 1.726-1.448.717-.49 1.478-.898 2.277-1.233C8.244.828 8.767.632 9.31.494c.655-.166 1.31-.33 1.982-.415.229-.03.458-.058.688-.07zm-1.847 22.82c-.07.06-.147.111-.207.18-.238.27-.464.549-.668.869l-.044.108a.177.177 0 00.093-.057c.174-.19.351-.378.519-.574.104-.122.195-.255.288-.386.024-.034.03-.08.046-.12l-.027-.02zm1.65-3.695a5.51 5.51 0 00-.653.593l-.37.386a.963.963 0 01-.377.25 1.372 1.372 0 01-.467.09c-.044 0-.087.006-.151.012.028.058.043.097.064.131.15.242.301.482.45.724.136.22.276.438.399.666.068.125.105.267.156.404.077.027.14-.018.202-.048.29-.135.579-.274.867-.412.213-.101.437-.186.636-.31.347-.215.68-.455 1.018-.685.015-.01.026-.028.042-.046-.023-.019-.038-.037-.056-.044-.287-.111-.527-.3-.77-.482a5.319 5.319 0 01-.506-.42 1.757 1.757 0 01-.41-.653c-.019-.049-.045-.095-.075-.156zm-5.847.264c-.06.096-.097.194-.132.293a3.38 3.38 0 01-.555 1.01c-.2.25-.455.412-.762.493-.23.06-.464.076-.7.07-.048-.002-.097.002-.158.005.016.04.021.066.035.085.1.145.23.246.4.295.157.046.316.034.498.023.181-.037.343-.115.485-.234.238-.199.402-.454.536-.732.175-.363.264-.751.342-1.144.01-.053.008-.11.011-.164zm14.945-4.586c.008.029.016.057.027.107.024.155.051.31.072.464.03.219.067.437.078.657.017.344.027.689-.014 1.033-.037.315-.063.633-.116.946a6.153 6.153 0 01-.46 1.518c-.008.018-.01.039-.02.082.047-.03.077-.042.098-.064.085-.083.17-.167.248-.255.271-.305.458-.66.596-1.043.18-.498.228-1.011.145-1.531-.103-.65-.33-1.263-.597-1.881a9.055 9.055 0 00-.024-.055l-.033.022zM5.797 8.29a.26.26 0 00.018.153c.124.251.25.501.379.75.025.049.066.09.03.163-.284.06-.578.119-.88.255.059.038.097.06.132.087.042.032.112.058.09.12-.01.033-.075.048-.117.072.017.01.043.021.067.036.166.102.33.207.447.368.138.192.229.404.188.644-.079.469-.306.85-.69 1.132-.054.04-.106.083-.161.122a.243.243 0 00-.103.245.77.77 0 00.055.195c.083.196.22.35.375.492.083.076.159.164.222.257a.37.37 0 01.025.377c-.023.05-.05.099-.076.148-.03.06-.028.111.022.162.041.042.08.089.112.138.038.058.078.079.147.05a.486.486 0 01.333-.006c.16.046.302.126.444.21.13.077.264.149.4.219.067.035.14.05.219.026.071-.022.124.01.145.076.02.064-.003.108-.074.139-.07.03-.137.063-.209.088-.1.035-.201.073-.314.077-.013-.107.11-.088.127-.159-.206-.126-.643-.145-.801-.034.063.112.035.21-.096.313-.13-.1-.025-.202.002-.3a.209.209 0 00-.249.17c-.015.101.067.216.178.224.108.007.218-.005.326-.012.06-.005.12-.027.199 0-.103.123-.248.127-.357.19.002.05.07.086.019.131-.053.048-.095-.001-.132-.03-.08-.063-.16-.126-.231-.197a.474.474 0 01-.157-.311.52.52 0 00-.043-.172c-.032-.074-.032-.137.033-.19-.018-.03-.028-.053-.045-.072a1.222 1.222 0 01-.196-.369c-.053-.137-.046-.264.048-.381.024-.03.05-.06.064-.095a.664.664 0 00.047-.168c.017-.165-.064-.287-.182-.387-.186-.156-.36-.322-.46-.551-.005-.011-.024-.017-.037-.026-.011.017-.024.027-.025.038-.019.185-.045.37-.052.557-.014.377.058.743.162 1.104.118.41.289.798.488 1.173.267.502.537 1.002.812 1.5.055.098.13.189.208.27.198.202.452.272.724.273.202 0 .404-.006.605-.026.295-.03.59-.073.884-.113.183-.025.365-.057.548-.08.21-.026.38.073.522.21.16.156.305.327.447.5.22.265.397.56.554.867.05.098.07.1.147.03.13-.121.26-.242.394-.36.067-.059.088-.12.067-.213a3.535 3.535 0 01-.085-.796c.002-.157.006-.314.018-.471.015-.224.03-.45.06-.672a59.114 59.114 0 01.362-2.298c.087-.493.182-.984.268-1.477.06-.347.118-.694.162-1.043.034-.273.055-.55.063-.825.011-.332.003-.665.002-.998 0-.077.004-.155-.01-.23-.028-.142-.01-.155-.162-.19a5.826 5.826 0 00-.607-.107c-.146-.018-.207-.053-.221-.19-.006-.049-.025-.098-.041-.146-.009-.025-.024-.048-.046-.09l-.025.264c-.009.096-.029.116-.127.115-.055 0-.11-.008-.164-.008-.476 0-.952-.008-1.426.032-.095.008-.173-.015-.226-.103-.04-.066-.088-.126-.134-.186-.063-.084-.086-.093-.182-.06-.195.068-.388.138-.582.21a2.71 2.71 0 00-.675.394.986.986 0 01-.323.168c-.033.01-.07.008-.127.013.02-.066.024-.114.047-.15.064-.105.135-.205.205-.306.023-.033.049-.063.073-.095l-.015-.023-.201.037c-.146.04-.296.07-.437.122-.148.053-.266.023-.386-.072a3.623 3.623 0 01-.733-.786l-.093-.132zm8.592 8.963l-.147.09c-.22.134-.44.266-.659.402-.093.058-.184.12-.27.188-.085.07-.124.161-.072.272.047.1.093.2.147.294.047.08.124.138.213.147.11.01.228.012.336-.012.217-.05.372-.205.528-.357a.291.291 0 00.087-.308c-.046-.18-.079-.365-.118-.547-.011-.052-.027-.103-.045-.169zm-.257-2.409c-.12.291-.205.597-.325.91-.151.433-.294.87-.435 1.323.036-.01.054-.01.067-.018.261-.16.522-.324.785-.484.054-.033.071-.078.065-.138-.012-.13-.024-.262-.034-.393l-.068-.886c-.008-.103-.02-.206-.029-.31-.009 0-.017-.002-.026-.004zm3.081-8.13l.099.285c.08.231.159.463.24.714l.58 1.952c.187.63.372 1.262.558 1.893.114.382.235.762.343 1.146.072.257.126.519.186.799.044.206.087.413.127.64.034.106.023.226.077.325l.025-.006-.068-.362c-.038-.206-.077-.412-.113-.638-.015-.07-.029-.141-.046-.211-.095-.396-.177-.796-.29-1.187-.196-.685-.413-1.364-.618-2.046-.165-.549-.322-1.1-.488-1.648-.069-.227-.15-.45-.226-.695l-.117-.336c-.037-.107-.075-.216-.115-.322-.04-.106-.084-.21-.127-.314a7.558 7.558 0 01-.027.01zM6.225 14.304c-.063-.001-.115.014-.134.083a.35.35 0 00.41.012 4.533 4.533 0 00-.276-.095zM5.23 11.98c-.026-.027-.057-.048-.075.002-.012.032-.007.07-.01.113.082-.037.082-.037.085-.115zm.062-1.189a.135.135 0 00-.088.056.197.197 0 00-.025.11c.005.152.01.306.026.457a.751.751 0 00.066.218c.061.136.157.167.288.101.055-.027.06-.054.025-.11a4.52 4.52 0 01-.129-.211c-.015-.068-.066-.131-.033-.207.04-.09-.076-.116-.074-.19V10.874c-.003-.038-.006-.087-.056-.083zm-.017-.968a.867.867 0 00-.467.127c-.076.045-.084.07-.05.158.034.087.07.173.115.254.064.117.09.125.21.077a.657.657 0 01.336-.053c.202.022.357.136.504.264l.092.077c.007-.006.014-.013.022-.018-.019-.105-.035-.226-.149-.264-.157-.053-.324-.075-.508-.117l-.24-.005c.24-.169.452-.044.687.009-.063-.115-.153-.147-.23-.193-.082-.05-.17-.092-.25-.144-.06-.037-.12-.08-.072-.172zm10.233.325c-.23-.01-.427.08-.608.211-.034.026-.06.065-.105.117.087.026.15.046.232.065.044-.015.088-.03.13-.046.306-.114.61-.115.904.031.126.063.237.04.366-.005-.02-.031-.03-.054-.045-.071a.986.986 0 00-.448-.273c-.14-.044-.284-.024-.426-.03zM7.99 6.483a.308.308 0 00.002.133c.08.321.156.643.242.962.104.387.27.75.456 1.103.02.037.061.08.098.087a.404.404 0 00.253-.051l-.472-.84c-.23-.448-.405-.92-.579-1.394zM10.397.497c-.2-.008-.405.004-.603.034-.236.035-.47.087-.7.152-.287.08-.569.18-.852.273-.04.013-.074.038-.11.058.028.014.05.018.07.014.287-.068.58-.085.873-.09.134-.002.269.009.402.025.19.024.382.048.57.09.456.104.874.3 1.265.556.464.306.888.66 1.257 1.078.205.232.395.475.56.739.17.274.315.561.449.856.273.601.456 1.232.6 1.876.04.173.07.348.1.524.017.104.065.167.17.19.122.028.2.105.22.251-.003.102-.06.174-.129.24a1.065 1.065 0 00-.268.358.164.164 0 00.083-.039c.08-.086.162-.172.235-.265a.56.56 0 00.13-.333c.009-.05.022-.1.024-.15.007-.124-.017-.15-.143-.168-.025-.004-.049-.014-.073-.015-.082-.007-.125-.063-.137-.131-.033-.198-.004-.355.247-.408.086-.018.174-.03.26-.042.158-.023.315-.053.473-.067.14-.012.19.033.226.167.008.029.018.057.021.087.019.179-.008.225-.141.288-.027.013-.055.024-.078.042a.148.148 0 00-.051.067c-.039.144.073.382.206.445l.673.32c.023.011.05.015.075.023l.018-.026c-.015-.008-.032-.013-.044-.024a2.27 2.27 0 00-.544-.32 4.898 4.898 0 00-.173-.075.203.203 0 01-.126-.191c-.003-.085.045-.154.128-.187l.059-.025c.099-.044.118-.076.112-.187a.384.384 0 00-.008-.063c-.067-.294-.123-.59-.205-.88a9.478 9.478 0 00-.826-2.036 7.465 7.465 0 00-1.39-1.805 4.536 4.536 0 00-1.177-.824 3.656 3.656 0 00-1.016-.328 6.155 6.155 0 00-.712-.074zm6.719 5.955c.01.014.018.028.038.034l-.022-.044-.016.01zM4.103 3.917a.062.062 0 01-.03.012.455.455 0 01-.04.039c-.01.01-.02.02-.045.04l-.363.354c-.088.085-.17.178-.266.253-.284.22-.425.53-.544.855a.132.132 0 00-.007.071c.013.055.033.108.052.168l.074.026c-.017.056-.03.105-.047.152-.058.164-.118.327-.175.491-.005.015.008.036.019.077.08-.175.158-.33.225-.489.228-.544.484-1.074.819-1.561.09-.133.182-.266.283-.401.004-.006.007-.013.022-.03.001-.016.003-.032.015-.04l.008-.017zm12.976 2.408a.023.023 0 01.009.019.073.073 0 00-.006.01.188.188 0 00.007.02l.018.022c.002-.007.007-.016.005-.021-.003-.01-.012-.018-.02-.038a1.331 1.331 0 01-.013-.012zM4.199 4.48c-.003.004-.008.008-.027.014-.005.013-.011.025-.031.047a2.085 2.085 0 01-.124.167c-.048.07-.116.055-.181.041-.134-.028-.228.016-.287.143-.089.187-.187.37-.273.56-.049.108-.11.216-.118.36.081.003.154.007.228.008h.228a2.563 2.563 0 01-.079.264c-.01.052-.022.103-.033.155l.02.004c.018-.046.037-.092.067-.153.066-.142.13-.285.2-.426.02-.04.034-.1.116-.092 0 .043.004.084 0 .124-.005.045-.017.09-.028.143.141.043.086.174.115.269.102-.022.104-.195.248-.144v.205l.017.002.439-1.059c-.13 0-.246-.02-.358.033-.024.011-.058-.001-.108-.004.075-.15.139-.278.211-.417a.128.128 0 01.025-.036c0-.015-.001-.03.008-.038l.006-.02c-.005.006-.01.011-.028.017-.004.012-.009.024-.026.045a.085.085 0 01-.032.033c-.123.157-.09.164-.258.106-.079-.027-.078-.028-.047-.144.028-.046.056-.093.098-.15 0-.016-.001-.032.007-.042L4.2 4.48zm2.073-.67c-.003.006-.007.011-.027.016-.094.125-.194.246-.28.377-.155.238-.301.481-.451.723-.14.224-.345.368-.575.481-.017.008-.04.006-.079.011.012-.059.016-.109.033-.153a6.076 6.076 0 01.229-.518l-.007-.02a.138.138 0 01-.035.025c-.028.05-.055.1-.093.164-.26.424-.443.817-.442.95.024.004.048.011.073.013.177.013.188.007.26-.165.03-.07.077-.12.147-.15l.175-.07c.044-.018.085-.057.146-.032.003.05-.01.11.014.145.042.062.044.125.047.193.002.049.017.098.026.147.029-.034.039-.065.05-.097.142-.39.277-.782.428-1.17.1-.256.22-.504.33-.756.013-.03.013-.067.03-.092V3.81zm3.987-.34c0 .045.01.084.021.123.042.16.094.318.124.48.024.133.023.27.028.406 0 .033-.019.067-.032.11-.094-.058-.047-.158-.106-.215h-.125c-.015.072-.01.152-.046.2-.066.085-.155.154-.236.227-.043.038-.078.018-.103-.025l-.046-.087c-.065.035-.117.069-.172.093-.116.051-.235.095-.35.147-.085.038-.09.053-.07.147.014.075.034.148.047.223.013.072.05.109.123.124.233.05.462.115.657.265.058-.102.058-.102.168-.151.03-.014.06-.03.092-.042.08-.03.115-.017.15.06.023.048.041.098.066.158.06-.14-.042-.267.017-.416.157.18.24.39.375.567a.235.235 0 00.022-.098c.002-.124 0-.247.002-.371 0-.034.013-.067.02-.1l.032-.003c.11.155.13.354.226.52a3.036 3.036 0 00-.01-.392c-.004-.045 0-.074.05-.088.08.036.116.14.215.158-.03-.275-.423-1.137-.798-1.635-.114-.127-.2-.28-.34-.386zm-2.667.696c-.019.034-.03.05-.037.067-.061.185-.125.37-.18.556-.031.105-.087.169-.195.19-.09.019-.178.052-.268.073-.038.009-.089.015-.118-.003-.024-.016-.025-.069-.036-.106-.064.076-.082.087-.17.047-.133-.062-.262-.135-.393-.201-.048-.025-.093-.063-.17-.03-.043.12-.091.25-.137.382-.099.28-.087.242.095.453.046.048.102.03.154.023.054-.009.106-.03.16-.036.13-.013.26-.08.367-.015.204-.064.387-.122.571-.178.05-.015.089.005.114.054.022.042.034.093.082.121.038-.056-.013-.128.063-.178l.14.241-.042-1.46zm.278.358c-.096-.01-.107.01-.11.108-.002.038-.003.078.002.115.03.2.099.386.174.57.002.006.012.01.022.015l.078-.05c.052.036.081.088.153.088.205-.002.41.014.616.012.099-.001.158.042.205.12.018.03.024.077.088.066l-.08-.394c-.05-.195-.085-.395-.172-.589-.057.057-.114.068-.18.046a.72.72 0 00-.135-.028c-.22-.028-.44-.059-.66-.08zm10.254-1.727c.089.163.155.316.139.491-.016.168.026.342-.044.516-.047-.033-.088-.082-.112-.075-.117.035-.164-.057-.227-.115a4.772 4.772 0 01-.286-.29l-.104-.113a4.856 4.856 0 01-.023.019c.035.046.07.093.11.156.04.064.084.127.122.193.034.058.065.118.031.205-.082-.01-.164-.019-.246-.032-.06-.01-.101 0-.124.07-.031.098-.037.096-.15.09.02.042.036.08.057.116.041.074.03.138-.03.196-.06.06-.118.122-.178.181a.175.175 0 01-.185.046c-.222-.061-.447-.113-.67-.174-.032-.009-.063-.04-.086-.068-.03-.04-.052-.087-.08-.13-.044-.07-.09-.138-.136-.207a.18.18 0 00-.014.105c.012.127.03.253.035.38.005.1-.024.12-.121.104-.104-.017-.206-.04-.31-.058-.064-.012-.131-.028-.202.03l.081.208c.09 0 .166-.01.237.002a.819.819 0 01.458.251c.078.083.154.168.241.26l.018-.005c-.004-.006-.008-.013-.01-.04.014-.056-.062-.118.018-.178.031.03.064.057.088.09.058.078.111.159.169.257l.089.141.024-.013a2093.819 2093.819 0 01-.427-.934c.055.007.083.007.108.016.193.07.385.142.577.216.074.028.147.06.219.094.062.028.112.018.157-.033.05-.056.102-.112.154-.167.05-.051.095-.046.132.014.016.025.026.053.04.08.071.138.143.277.217.433l.159.308.025-.011c-.044-.106-.07-.218-.138-.334-.057-.182-.168-.346-.206-.545.136.034.362.326.567.732l.057.074.018-.011a1.563 1.563 0 01-.052-.127c-.046-.145-.097-.29-.136-.436-.022-.083-.036-.173.022-.26l.109.058-.026-.207.027-.016c.022.02.05.036.065.06.073.108.143.22.215.33.01.016.029.029.043.043-.036-.217-.2-.38-.229-.626l.155.112c.014-.166.012-.319.042-.465.032-.158-.023-.297-.063-.445.024.004.036.006.055.025.092.124.183.249.277.371.02.027.05.047.069.087l.04.063.019-.015a.293.293 0 01-.053-.082 27.922 27.922 0 01-.332-.49c-.221-.311-.363-.467-.485-.521zm-6.57.327c-.003.161.092.275.069.415l-.368.087c.09.139.032.237-.052.331-.05.057-.092.122-.143.178-.037.04-.046.078-.018.126l.16.275c.029.048.072.066.128.064.076-.003.152 0 .228-.001.116-.003.216.022.275.137.006.014.02.024.044.052.004-.059-.003-.098.01-.13.016-.04.04-.099.072-.108.084-.023.173-.024.26-.03.013-.001.027.018.04.029l.071.065c.019-.11-.082-.198-.024-.31l.126.04c-.026-.123-.07-.245-.071-.366 0-.123.051-.243.115-.36.107.062.16.156.234.253.183.265.36.533.494.834.165-.078.27.068.407.088-.003-.106-.133-.441-.197-.492a.142.142 0 00-.102-.028c-.06.011-.119.039-.191.063-.025-.039-.056-.078-.077-.122a3.936 3.936 0 00-.473-.783c-.076-.094-.16-.182-.228-.26l-.391.285c-.049.035-.094.03-.132-.017l-.169-.207c-.025-.03-.053-.059-.097-.108z" />',
	},
};

// Display names mirror hermes_cli/models.py CANONICAL_PROVIDERS/_PROVIDER_LABELS
// (what the model picker shows). Visual style matches the main model menu:
// neutral theme tokens, no brand colors.
const PROVIDER_META = {
	anthropic: { name: "Anthropic", mono: "A" },
	"openai-codex": { name: "OpenAI Codex", mono: "O" },
	nous: { name: "Nous Portal", mono: "N" },
	openrouter: { name: "OpenRouter", mono: "OR" },
	gemini: { name: "Google Gemini", mono: "G" },
	kimi: { name: "Kimi / Moonshot", mono: "K" },
	grok: { name: "xAI Grok", mono: "X" },
};

function providerMeta(pid) {
	return (
		PROVIDER_META[pid] || {
			name: pid,
			mono: String(pid || "?")
				.slice(0, 2)
				.toUpperCase(),
		}
	);
}

function ProviderBadge({ pid }) {
	const meta = providerMeta(pid);
	const svg = PROVIDER_SVGS[pid];
	if (svg) {
		// dangerouslySetInnerHTML is safe here: the path data is a build-time
		// constant inlined from @lobehub/icons, never user input.
		return jsx("span", {
			className:
				"inline-flex h-5 w-5 shrink-0 items-center justify-center text-(--ui-text-secondary)",
			children: jsx("svg", {
				viewBox: svg.viewBox,
				width: "1em",
				height: "1em",
				fill: "currentColor",
				className: "text-[0.875rem]",
				dangerouslySetInnerHTML: { __html: svg.body },
			}),
		});
	}
	return jsx("span", {
		className:
			"inline-flex h-5 w-5 shrink-0 items-center justify-center rounded bg-(--ui-control-hover-background) text-[0.625rem] font-semibold uppercase tracking-wide text-(--ui-text-secondary)",
		children: meta.mono,
	});
}

// ---- severity helpers -----------------------------------------------------

function toneForRemaining(pct) {
	if (pct == null) return "muted";
	if (pct <= 15) return "bad";
	if (pct <= 40) return "warn";
	return "good";
}

// Theme tokens that actually exist in the Desktop app. --ui-danger/--ui-warning
// do NOT exist as theme variables (the core app only ever uses them with an
// inline fallback), so use the real ones: --ui-red / --ui-yellow / --ui-accent.
function toneColor(tone) {
	if (tone === "bad") return "var(--ui-red)";
	if (tone === "warn") return "var(--ui-yellow)";
	if (tone === "good") return "var(--ui-accent)";
	return "var(--ui-stroke-tertiary)";
}

function remainingPct(w) {
	// Cache stores used_percent; widget shows remaining = 100 - used.
	const used = w && w.used_percent;
	if (used == null) return null;
	const n = Number(used);
	if (!Number.isFinite(n)) return null;
	return Math.max(0, Math.min(100, Math.round(100 - n)));
}

function worstWindow(provider) {
	let worst = null;
	for (const w of provider.windows || []) {
		const r = remainingPct(w);
		if (r == null) continue;
		if (worst == null || r < worst) worst = r;
	}
	return worst;
}

// Short absolute reset: today/tomorrow/time, else "Aug 11, 2:30 PM".
function absoluteReset(resetIso) {
	if (!resetIso) return "";
	try {
		const dt = new Date(resetIso);
		if (isNaN(dt.getTime())) return "";
		const now = new Date();
		const sameDay = dt.toDateString() === now.toDateString();
		const tomorrow = new Date(now);
		tomorrow.setDate(now.getDate() + 1);
		const isTomorrow = dt.toDateString() === tomorrow.toDateString();
		const time = dt.toLocaleTimeString([], {
			hour: "2-digit",
			minute: "2-digit",
		});
		if (sameDay) return `today ${time}`;
		if (isTomorrow) return `tomorrow ${time}`;
		return fmtDayTime.format(dt);
	} catch {
		return "";
	}
}

// Short relative countdown, e.g. "resets in 3h 12m". `nowMs` lets the UI tick.
function relativeCountdown(resetIso, nowMs = Date.now()) {
	if (!resetIso) return "";
	try {
		const dt = new Date(resetIso);
		const target = dt.getTime();
		if (isNaN(target)) return "";
		const diff = target - nowMs;
		if (diff <= 0) return "resetting…";
		const totalMin = Math.floor(diff / 60_000);
		const days = Math.floor(totalMin / 1440);
		const hours = Math.floor((totalMin % 1440) / 60);
		const mins = totalMin % 60;
		if (days > 0) return `${days}d ${hours}h`;
		if (hours > 0) return `${hours}h ${mins}m`;
		if (mins > 0) return `${mins}m`;
		return "<1m";
	} catch {
		return "";
	}
}

// Format a reset timestamp per the persisted format setting.
function formatReset(resetIso, format) {
	if (!resetIso) return "";
	if (format === "absolute") return absoluteReset(resetIso);
	const relative = relativeCountdown(resetIso);
	const absolute = absoluteReset(resetIso);
	return absolute ? `${relative} (${absolute})` : relative;
}

function isConfigured(provider) {
	return provider && !provider.unavailable_reason;
}

// Re-render on an interval so relative countdowns tick live.
function useNow(intervalMs = 30_000) {
	const [now, setNow] = useState(() => Date.now());
	useEffect(() => {
		const id = setInterval(() => setNow(Date.now()), intervalMs);
		return () => clearInterval(id);
	}, [intervalMs]);
	return now;
}

// ---- update self-check (like Hermes's own) ---------------------------------

const REPO_API_LATEST =
	"https://api.github.com/repos/rarf/hermes-quota-plugin/commits/master";

// Compare the installed commit stamp against GitHub master. One cheap
// request per hour, cached in storage; never blocks rendering.
function useUpdateCheck() {
	const [update, setUpdate] = useState(null);
	useEffect(() => {
		let alive = true;
		const CHECK_KEY = "updateCheck";
		const ONE_HOUR = 60 * 60 * 1000;
		const run = async () => {
			let installedSha = null;
			try {
				const stamp = await host.request("fs.read", {
					path: "desktop-plugins/quota/version.json",
				});
				installedSha = stamp ? JSON.parse(stamp).installed_sha : null;
			} catch {
				/* not stamped (older install) — skip check */
			}
			if (!installedSha || installedSha === "unknown") return;
			// Throttle: at most one GitHub call per hour.
			try {
				const prev = CTX.storage.get(CHECK_KEY);
				if (prev) {
					const p = JSON.parse(prev);
					if (
						p.at &&
						Date.now() - p.at < ONE_HOUR &&
						p.latest
					) {
						if (p.latest !== installedSha) {
							setUpdate({ latest: p.latest });
						}
						return;
					}
				}
			} catch {
				/* noop */
			}
			try {
				const res = await fetch(REPO_API_LATEST, {
					headers: { Accept: "application/vnd.github+json" },
				});
				if (!res.ok) return;
				const j = await res.json();
				const latest = j && j.sha;
				if (!latest) return;
				try {
					CTX.storage.set(
						CHECK_KEY,
						JSON.stringify({ at: Date.now(), latest }),
					);
				} catch {
					/* noop */
				}
				if (alive && latest !== installedSha) {
					setUpdate({ latest });
				}
			} catch {
				/* offline / rate-limited — stay quiet */
			}
		};
		run();
		return () => {
			alive = false;
		};
	}, []);
	return update;
}

function UpdateBanner({ update }) {
	if (!update) return null;
	return jsxs("button", {
		type: "button",
		className: cn(
			"flex w-full items-center justify-between gap-2 rounded px-3 py-2 text-left text-[0.6875rem]",
			"bg-(--chrome-action) text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) transition-colors",
		),
		onClick: () =>
			host.openExternal?.("https://github.com/rarf/hermes-quota-plugin"),
		children: [
			jsx("span", {
				className: "font-medium",
				children: "⬆ Update available — run ./install.sh to get it",
			}),
			jsx("span", { className: "text-(--ui-text-tertiary)", children: "↗" }),
		],
	});
}

// ---- data hook (cli.exec instead of REST) ----------------------------------

function useQuota() {
	const intervalMs = useValue(refreshIntervalAtom) * 1000;
	return useQuery({
		queryKey: ["quota", "widget"],
		queryFn: async () => {
			// Call Hermes CLI via the desktop gateway. --max-age makes the
			// backend re-fetch providers when the cache is older than our
			// poll interval, so the configured cadence actually refreshes.
			const maxAge = Math.max(10, Math.floor(intervalMs / 1000));
			const result = await host.request("cli.exec", {
				argv: [
					"quota",
					"status",
					"--json",
					"--max-age",
					String(maxAge),
				],
			});
			if (result?.blocked || result?.code !== 0) {
				throw new Error(
					result?.hint || result?.output || "quota status failed",
				);
			}
			// CLI prints JSON to stdout; parse it
			let data;
			try {
				data = JSON.parse(result.output || "{}");
			} catch {
				data = {};
			}
			return data;
		},
		refetchInterval: intervalMs,
		staleTime: Math.max(10_000, Math.floor(intervalMs / 2)),
		retry: 1,
	});
}

// ---- single worst chip (worst mode) ---------------------------------------

function QuotaChipWithBar() {
	const { data } = useQuota();
	const providers =
		data && data.providers ? Object.entries(data.providers) : [];
	let worst = null;
	let worstLabel = "";
	for (const [pid, p] of providers) {
		if (!isProviderEnabled(pid)) continue;
		const r = worstWindow(p);
		if (r == null) continue;
		if (worst == null || r < worst) {
			worst = r;
			worstLabel = providerMeta(pid).name;
		}
	}
	if (worst == null) return jsx("span", { children: "Q:none" });
	const tone = toneForRemaining(worst);
	const fill = toneColor(tone);
	const tip = makeWorstTip(worstLabel, worst, data && data.providers);
	return jsxs("button", {
		type: "button",
		title: tip,
		className:
			"inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded cursor-pointer hover:bg-(--chrome-action-hover) transition-colors",
		onClick: () => {
			if (typeof host.navigate === "function") host.navigate("/quota");
		},
		children: [
			jsx("span", {
				className: "text-[0.6875rem] text-(--ui-text-secondary)",
				children: worstLabel + " " + worst + "%",
			}),
			jsx("span", {
				className:
					"inline-block h-1.5 w-10 overflow-hidden rounded-full bg-(--ui-stroke-secondary)",
				children: jsx("span", {
					className: "block h-full rounded-full",
					style: { width: worst + "%", background: fill },
				}),
			}),
		],
	});
}

function ProviderChip({ pid, provider }) {
	const r = worstWindow(provider);
	const tone = toneForRemaining(r);
	const dot = toneColor(tone);
	const label = providerMeta(pid).name;
	const tip = makeProviderTip(pid, provider);
	return jsxs(
		"button",
		{
			type: "button",
			title: tip,
			className: cn(
				"inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]",
				"text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors",
			),
			onClick: () => {
				if (typeof host.navigate === "function") host.navigate("/quota");
			},
			children: [
				jsx("span", { className: "inline-block h-1.5 w-1.5 rounded-full", style: { background: dot } }),
				jsx("span", { children: label }),
				jsx("span", { className: "tabular-nums", children: r == null ? "—" : `${r}%` }),
			],
		},
	);
}

// Build a rich multiline tip: every provider with data, listing ALL its
// windows (Session, Spark 5h, Spark Weekly, ...) each with % left + reset.
function makeWorstTip(worstLabel, worst, providersObj) {
	if (!providersObj) return `Quota · lowest ${worst}% (${worstLabel})`;
	const blocks = [];
	for (const [pid, p] of Object.entries(providersObj)) {
		if (!isProviderEnabled(pid) || !isConfigured(p)) continue;
		const lines = providerWindowLines(pid, p);
		if (lines.length) blocks.push(lines.join("\n"));
	}
	if (blocks.length === 0) return `Quota · lowest ${worst}% (${worstLabel})`;
	return `Quota breakdown:\n${blocks.join("\n")}\nClick to open Quota pane`;
}

// Build a rich multiline tip for a single provider: lists ALL its windows
// plus plan and detail lines (credits, banked resets).
function makeProviderTip(pid, provider) {
	const meta = providerMeta(pid);
	const lines = providerWindowLines(pid, provider);
	if (provider.plan) lines.unshift(`Plan: ${provider.plan}`);
	lines.unshift(meta.name);
	const details = provider.details || [];
	if (details.length) lines.push(...details);
	lines.push("Click to open Quota pane");
	return lines.join("\n");
}

// Shared: turn a provider's windows into tooltip lines like
// "  Session · 21% left · resets 5d 19h" / "  5.3 Codex Spark · 5h · 8% left · resets 3h 52m"
function providerWindowLines(pid, provider) {
	const windows = provider.windows || [];
	const out = [];
	for (const w of windows) {
		const r = remainingPct(w);
		const reset = w.reset_at ? formatReset(w.reset_at, "relative") : "";
		out.push(
			`  ${w.label || "window"} · ${r == null ? "—" : `${r}% left`}${reset ? ` · resets ${reset}` : ""}`,
		);
	}
	return out;
}

function StatusBar() {
	const showStatusBar = useValue(showStatusBarAtom);
	const mode = useValue(statusbarModeAtom);
	const { data, isError } = useQuota();
	if (!showStatusBar) return null;
	if (mode === "worst") return jsx(QuotaChipWithBar, {});
	if (isError || !data || !data.providers)
		return jsx(StatusDot, { tone: "muted" });
	// Default: only providers with real data. The cherry-picker can only hide
	// more, never show unavailable ones here (the pane shows those muted).
	const entries = Object.entries(data.providers)
		.filter(([pid]) => isProviderEnabled(pid))
		.filter(([, p]) => isConfigured(p));
	if (entries.length === 0) return jsx(StatusDot, { tone: "muted" });
	return jsx("span", {
		className: "inline-flex h-full items-center",
		children: entries.map(([pid, p]) =>
			jsx(ProviderChip, { pid, provider: p, key: pid }),
		),
	});
}

// ---- pane -----------------------------------------------------------------

function QuotaBar({ value, tone }) {
	const pct = Math.max(0, Math.min(100, value == null ? 0 : value));
	return jsx("div", {
		className:
			"h-2 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)",
		children: jsx("div", {
			className: "h-full rounded-full transition-all",
			style: { width: `${pct}%`, background: toneColor(tone) },
		}),
	});
}

// One provider card: bordered box with label + plan, a thick tonal bar per
// window, colored "% left", reset line, and provider detail lines (credits,
// banked resets). Unavailable providers render as a muted card.
function ProviderRow({ id, provider }) {
	const t = usePluginI18n(ID);
	const resetFormat = useValue(resetFormatAtom);
	const reason = provider.unavailable_reason;
	const details = provider.details || [];
	const displayName = providerMeta(id).name;

	if (reason) {
		return jsxs("div", {
			className:
				"flex flex-col gap-0.5 rounded-lg border border-(--ui-stroke-secondary) px-3 py-2.5 opacity-60",
			children: [
				jsxs("div", {
					className: "flex items-center gap-2 text-sm",
					children: [
						jsx(ProviderBadge, { pid: id }),
						jsx("span", {
							className: "font-medium text-(--ui-text-primary)",
							children: displayName,
						}),
					],
				}),
				jsx("div", {
					className: "pl-7 text-xs text-(--ui-text-tertiary)",
					children: t("unavailable", reason),
				}),
			],
		});
	}

	const windows = provider.windows || [];
	if (windows.length === 0 && details.length === 0) {
		return jsxs("div", {
			className:
				"flex items-center gap-2 rounded-lg border border-(--ui-stroke-secondary) px-3 py-2.5 text-sm opacity-60",
			children: [
				jsx(ProviderBadge, { pid: id }),
				jsx("span", {
					className: "font-medium text-(--ui-text-primary)",
					children: displayName,
				}),
				jsx("span", {
					className: "text-xs text-(--ui-text-tertiary)",
					children: t("noData"),
				}),
			],
		});
	}

	return jsxs("div", {
		className:
			"flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) px-3 py-2.5",
		children: [
			jsxs("div", {
				className: "flex items-center justify-between gap-2",
				children: [
					jsxs("span", {
						className:
							"flex items-center gap-2 text-sm font-semibold text-(--ui-text-primary)",
						children: [
							jsx(ProviderBadge, { pid: id }),
							displayName,
							jsx(StatusDot, {
								tone:
									worstWindow(provider) == null
										? "muted"
										: toneForRemaining(worstWindow(provider)),
							}),
						],
					}),
					provider.plan
						? jsx("span", {
								className:
									"rounded bg-(--ui-control-hover-background) px-1.5 py-0.5 text-[0.6875rem] font-medium text-(--ui-text-secondary)",
								children: provider.plan,
							})
						: null,
				],
			}),
			...windows.map((w, i) => {
				const r = remainingPct(w);
				const tone = toneForRemaining(r);
				return jsxs(
					"div",
					{
						className: "flex flex-col gap-1",
						children: [
							jsxs("div", {
								className: "flex items-center justify-between gap-2 text-xs",
								children: [
									jsx("span", {
										className: "text-(--ui-text-secondary)",
										children: w.label || "window",
									}),
									jsx("span", {
										className: "tabular-nums text-[0.8125rem] font-medium",
										style: { color: toneColor(tone) },
										children: r == null ? "—" : `${r}% left`,
									}),
								],
							}),
							jsx(QuotaBar, { value: r, tone }),
							w.reset_at
								? jsx("div", {
										className: "text-[0.6875rem] text-(--ui-text-quaternary)",
										children: t("reset", formatReset(w.reset_at, resetFormat)),
									})
								: null,
						],
					},
					`${id}-w-${i}`,
				);
			}),
			...details.map((d, i) =>
				jsx(
					"div",
					{
						className: "text-[0.6875rem] text-(--ui-text-tertiary)",
						children: d,
					},
					`${id}-d-${i}`,
				),
			),
		],
	});
}

function ResetFormatControl() {
	const t = usePluginI18n(ID);
	const resetFormat = useValue(resetFormatAtom);
	return jsxs("div", {
		className: "flex items-center justify-between gap-2",
		children: [
			jsx("span", {
				className: "text-xs text-(--ui-text-secondary)",
				children: t("resetFormatLabel"),
			}),
			jsx(SegmentedControl, {
				value: resetFormat,
				onChange: (v) => setResetFormat(v),
				options: [
					{ id: "relative", label: t("relative") },
					{ id: "absolute", label: t("absolute") },
				],
			}),
		],
	});
}

function SdkIcon({ name, className, fallback = "•" }) {
	const C = icons[name];
	return C
		? jsx(C, { className })
		: jsx("span", { className, children: fallback });
}

function ShowStatusBarControl() {
	const t = usePluginI18n(ID);
	const show = useValue(showStatusBarAtom);
	return jsxs("div", {
		className: "flex flex-col gap-1",
		children: [
			jsxs("div", {
				className: "flex items-center justify-between gap-2",
				children: [
					jsx("span", {
						className: "text-xs text-(--ui-text-secondary)",
						children: t("showStatusBarLabel"),
					}),
					jsx(Switch, {
						checked: show,
						onCheckedChange: (v) => setShowStatusBar(v),
					}),
				],
			}),
			jsx("div", {
				className: "text-[0.625rem] text-(--ui-text-quaternary)",
				children: t("showStatusBarHint"),
			}),
		],
	});
}

function ShowDockedPaneControl() {
	const t = usePluginI18n(ID);
	const show = useValue(showDockedPaneAtom);
	return jsxs("div", {
		className: "flex items-center justify-between gap-2",
		children: [
			jsx("span", {
				className: "text-xs text-(--ui-text-secondary)",
				children: t("showDockedPaneLabel"),
			}),
			jsx(Switch, {
				checked: show,
				onCheckedChange: (v) => setShowDockedPane(v),
			}),
		],
	});
}

function RefreshIntervalControl() {
	const t = usePluginI18n(ID);
	const seconds = useValue(refreshIntervalAtom);
	return jsxs("div", {
		className: "flex flex-col gap-1",
		children: [
			jsxs("div", {
				className: "flex items-center justify-between gap-2",
				children: [
					jsx("span", {
						className: "text-xs text-(--ui-text-secondary)",
						children: t("refreshIntervalLabel"),
					}),
					jsxs("div", {
						className: "flex items-center gap-1",
						children: [
							jsx(Input, {
								type: "number",
								min: REFRESH_INTERVAL_MIN,
								max: REFRESH_INTERVAL_MAX,
								step: 5,
								value: seconds,
								onChange: (e) =>
									setRefreshInterval(e && e.target ? e.target.value : e),
								className: "h-6 w-16 text-right text-xs",
							}),
							jsx("span", {
								className: "text-[0.6875rem] text-(--ui-text-quaternary)",
								children: t("seconds"),
							}),
						],
					}),
				],
			}),
			jsx("div", {
				className: "text-[0.625rem] text-(--ui-text-quaternary)",
				children: t(
					"refreshIntervalHint",
					REFRESH_INTERVAL_MIN,
					REFRESH_INTERVAL_MAX,
				),
			}),
		],
	});
}

function StatusbarModeControl() {
	const t = usePluginI18n(ID);
	const mode = useValue(statusbarModeAtom);
	return jsxs("div", {
		className: "flex items-center justify-between gap-2",
		children: [
			jsx("span", {
				className: "text-xs text-(--ui-text-secondary)",
				children: t("statusbarModeLabel"),
			}),
			jsx(SegmentedControl, {
				value: mode,
				onChange: (v) => setStatusbarMode(v),
				options: [
					{ id: "all", label: t("statusbarModeAll") },
					{ id: "worst", label: t("statusbarModeWorst") },
				],
			}),
		],
	});
}

// Cherry-picker: toggle list of providers
function DisabledProvidersControl() {
	const t = usePluginI18n(ID);
	const { data } = useQuota();
	const disabled = useValue(disabledProvidersAtom);
	const allProviders =
		data && data.providers ? Object.entries(data.providers) : [];
	if (allProviders.length === 0) return null;
	return jsxs("div", {
		className: "flex flex-col gap-2",
		children: [
			jsx("span", {
				className: "text-xs text-(--ui-text-secondary)",
				children: t("disabledProvidersLabel"),
			}),
			jsx("div", {
				className: "flex flex-wrap gap-1.5",
				children: allProviders.map(([pid, p]) => {
					const enabled = !disabled.includes(pid);
					return jsx(
						"button",
						{
							type: "button",
							className: cn(
								"inline-flex h-6 items-center justify-center gap-1 rounded px-2 text-xs",
								enabled
									? "bg-(--chrome-action) text-(--ui-text-primary) hover:bg-(--chrome-action-hover)"
									: "bg-(--ui-stroke-secondary) text-(--ui-text-tertiary) hover:bg-(--ui-stroke-tertiary)",
							),
							onClick: () => {
								const next = enabled
									? [...disabled, pid]
									: disabled.filter((x) => x !== pid);
								setDisabledProviders(next);
							},
							title: enabled ? t("disableTooltip") : t("enableTooltip"),
							children: jsxs("span", {
								className: "inline-flex items-center gap-1.5",
								children: [
									jsx("span", {
										className: enabled
											? "text-(--ui-accent)"
											: "text-(--ui-text-quaternary)",
										children: enabled ? "●" : "○",
									}),
									jsx(ProviderBadge, { pid }),
									jsx("span", { children: providerMeta(pid).name }),
								],
							}),
						},
						pid,
					);
				}),
			}),
		],
	});
}

function QuotaSettings() {
	const t = usePluginI18n(ID);
	return jsxs("div", {
		className: "flex flex-col gap-3 overflow-y-auto py-1",
		children: [
			jsx(ShowStatusBarControl, {}),
			jsx(StatusbarModeControl, {}),
			jsx(ShowDockedPaneControl, {}),
			jsx(ResetFormatControl, {}),
			jsx(RefreshIntervalControl, {}),
			jsx(DisabledProvidersControl, {}),
		],
	});
}

function QuotaPane() {
	const t = usePluginI18n(ID);
	const qc = useQueryClient();
	const [view, setView] = useState("list"); // 'list' | 'settings'
	const { data, isError, isLoading, refetch } = useQuota();
	const update = useUpdateCheck();
	const refresh = useMutation({
		mutationFn: async () => {
			const result = await host.request("cli.exec", {
				argv: ["quota", "refresh"],
			});
			if (result?.blocked || result?.code !== 0) {
				throw new Error(
					result?.hint || result?.output || "quota refresh failed",
				);
			}
		},
		onSuccess: () => qc.invalidateQueries({ queryKey: ["quota", "widget"] }),
	});

	const headerTitle = view === "settings" ? t("settingsTitle") : t("paneTitle");

	let body;
	if (view === "settings") {
		body = jsx(QuotaSettings, {});
	} else if (isLoading) {
		body = jsx("div", {
			className: "text-xs text-(--ui-text-tertiary)",
			children: t("loading"),
		});
	} else if (isError) {
		body = jsx("div", {
			className: "text-xs text-(--ui-text-tertiary)",
			children: t("error"),
		});
	} else if (
		!data ||
		!data.providers ||
		Object.keys(data.providers).length === 0
	) {
		body = jsxs("div", {
			className: "flex flex-col gap-1 text-xs text-(--ui-text-tertiary)",
			children: [
				jsx("div", { children: t("empty") }),
				jsx("div", { children: t("emptyHint") }),
			],
		});
	} else {
		const disabled = disabledProvidersAtom.get();
		// Default view: only providers with real data. Providers without data
		// (unconfigured / opt-in off) collapse into a muted "no data" section.
		const all = Object.entries(data.providers).filter(
			([pid]) => !disabled.includes(pid),
		);
		const withData = all.filter(([, p]) => isConfigured(p));
		const withoutData = all.filter(([, p]) => !isConfigured(p));
		body = jsxs("div", {
			className: "flex flex-col gap-2",
			children: [
				...withData.map(([id, p]) =>
					jsx(ProviderRow, { id, provider: p, key: id }),
				),
				withoutData.length > 0
					? jsxs("details", {
							className: "mt-1",
							children: [
								jsx("summary", {
									className:
										"cursor-pointer select-none text-[0.6875rem] text-(--ui-text-quaternary) hover:text-(--ui-text-tertiary)",
									children: t("noDataSection", withoutData.length),
								}),
								jsx("div", {
									className: "mt-1.5 flex flex-col gap-2",
									children: withoutData.map(([id, p]) =>
										jsx(ProviderRow, { id, provider: p, key: id }),
									),
								}),
							],
						})
					: null,
			],
		});
	}

	return jsxs("div", {
		className: "flex h-full flex-col gap-2 p-3",
		children: [
			jsxs("div", {
				className: "flex items-center justify-between",
				children: [
					jsx("div", {
						className: "text-sm font-medium",
						children: headerTitle,
					}),
					jsxs("div", {
						className: "flex items-center gap-1",
						children: [
							view === "settings"
								? jsx("button", {
										type: "button",
										className: cn(
											"inline-flex h-6 w-6 items-center justify-center rounded",
											"text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors",
										),
										title: t("backTip"),
										onClick: () => setView("list"),
										children: jsx(SdkIcon, {
											name: "ArrowLeft",
											className: "h-3.5 w-3.5",
											fallback: "‹",
										}),
									})
								: jsxs("div", {
										className: "flex items-center",
										children: [
											jsx("button", {
												type: "button",
												className: cn(
													"inline-flex h-6 w-6 items-center justify-center rounded",
													"text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors",
												),
												title: t("refreshTip"),
												disabled: refresh.isPending,
												onClick: () => refresh.mutate(),
												children: jsx(icons.RefreshCw, {
													className: cn(
														"h-3.5 w-3.5",
														refresh.isPending && "animate-spin",
													),
												}),
											}),
											jsx("button", {
												type: "button",
												className: cn(
													"inline-flex h-6 items-center justify-center gap-1 rounded px-1.5",
													"text-xs text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors",
												),
												title: t("settingsTip"),
												onClick: () => setView("settings"),
												children: jsxs("span", {
													className: "inline-flex items-center gap-1",
													children: [
														jsx(SdkIcon, {
															name: "Settings",
															className: "h-3.5 w-3.5",
															fallback: "⚙",
														}),
														jsx("span", { children: t("settingsButton") }),
													],
												}),
											}),
										],
									}),
						],
					}),
				],
			}),
			jsx("div", { className: "min-h-0 flex-1", children: body }),
			jsx(UpdateBanner, { update }),
			data && data.fetched_at
				? jsx("div", {
						className: "pt-2 text-[0.6875rem] text-(--ui-text-quaternary)",
						children: t(
							"fetched",
							absoluteReset(data.fetched_at) || data.fetched_at,
						),
					})
				: null,
		],
	});
}

// ---- registration ---------------------------------------------------------

let _paneDisposer = null;

function clearDockedPane() {
	if (!_paneDisposer) return;
	try {
		_paneDisposer();
	} catch {
		/* noop */
	}
	_paneDisposer = null;
}

function registerDockedPane() {
	clearDockedPane();
	if (!CTX || !showDockedPaneAtom.get()) return;
	try {
		_paneDisposer = CTX.register({
			id: "pane",
			area: PANES_AREA,
			title: "quota",
			data: {
				placement: "right",
				dock: { pane: "workspace", pos: "right" },
				width: "300px",
			},
			render: () => jsx(QuotaPane, {}),
		});
	} catch {
		_paneDisposer = null;
	}
}

export default {
	id: ID,
	name: "Quota Widget",
	defaultEnabled: true,
	register(ctx) {
		CTX = ctx;
		applyStoredAll();
		registerDockedPane();
		ctx.i18n.register({
			en: {
				paneTitle: "Quota",
				settingsTitle: "Quota · Settings",
				chipTip: (pct, label, reset) =>
					`Quota · lowest ${pct}% (${label})${reset ? ` · resets in ${reset}` : ""}`,
				providerTip: (label, pct, reset) =>
					`${label} · ${pct}%${reset ? ` · resets in ${reset}` : ""}`,
				popoverReset: (reset) => `resets in ${reset}`,
				refreshTip: "Refresh quota",
				settingsTip: "Quota settings",
				backTip: "Back to quota",
				loading: "Loading…",
				error: "Quota backend unavailable",
				empty: "No quota data yet.",
				emptyHint: "Quota data is being initialized automatically…",
				unavailable: (reason) => `unavailable (${reason})`,
				noData: "no window data",
				noDataSection: (n) => `No data (${n}) — click to expand`,
				reset: (when) => `reset ${when}`,
				fetched: (when) => `fetched ${when}`,
				resetFormatLabel: "Reset format",
				relative: "Relative",
				absolute: "Absolute",
				statusbarModeLabel: "Status bar mode",
				statusbarModeAll: "All providers",
				statusbarModeWorst: "Worst only",
				showStatusBarLabel: "Show status bar indicator",
				showStatusBarHint:
					"The Hermes status bar must also be visible (⌘K → Toggle status bar).",
				showDockedPaneLabel: "Show docked quota pane",
				settingsButton: "Settings",
				refreshIntervalLabel: "Refresh interval",
				refreshIntervalHint: (min, max) =>
					`Polls every ${min}–${max}s. Bar and pane update live.`,
				seconds: "s",
				disabledProvidersLabel: "Enabled providers",
				disableTooltip: "Disable this provider",
				enableTooltip: "Enable this provider",
			},
		});

		// Single, static statusbar item. The StatusBar component re-renders itself
		// via hooks (useQuota / useValue) — it never re-registers, so no feedback
		// loop. Order 125 keeps it left of the default right-side items.
		ctx.register({
			id: "statusbar",
			area: STATUSBAR_AREAS.right,
			order: 125,
			render: () => jsx(StatusBar, {}),
		});

		// Route page (/quota).
		ctx.register({
			id: "page",
			area: ROUTES_AREA,
			data: { path: "/quota" },
			render: () => jsx(QuotaPane, {}),
		});

		// Sidebar nav row.
		ctx.register({
			id: "nav",
			area: SIDEBAR_NAV_AREA,
			order: 80,
			data: { codicon: "pulse", label: "Quota", path: "/quota" },
		});
	},
};
