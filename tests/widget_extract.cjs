// Extracts named top-level functions from desktop/plugin.js and evaluates them
// in isolation, so a pure helper can be tested without booting the bundle.
const fs = require("node:fs");
const path = require("node:path");

const repo = process.argv[2];
const fixture = process.argv[3];
const src = fs.readFileSync(path.join(repo, "desktop/plugin.js"), "utf8");

// Brace matching must ignore braces inside strings, template literals and
// comments, or it runs past the function into module code.
function extract(name) {
	const start = src.indexOf("function " + name + "(");
	if (start < 0) throw new Error("not found: " + name);
	let depth = 0;
	let seen = false;
	let i = start;
	while (i < src.length) {
		const ch = src[i];
		if (ch === "/" && src[i + 1] === "/") {
			i = src.indexOf("\n", i);
			if (i < 0) break;
			continue;
		}
		if (ch === "/" && src[i + 1] === "*") {
			const end = src.indexOf("*/", i + 2);
			if (end < 0) break;
			i = end + 2;
			continue;
		}
		if (ch === '"' || ch === "'" || ch === "`") {
			const quote = ch;
			i += 1;
			while (i < src.length && src[i] !== quote) {
				if (src[i] === "\\") i += 1;
				i += 1;
			}
			i += 1;
			continue;
		}
		if (ch === "{") {
			depth += 1;
			seen = true;
		} else if (ch === "}") {
			depth -= 1;
			if (seen && depth === 0) return src.slice(start, i + 1);
		}
		i += 1;
	}
	throw new Error("could not extract " + name);
}

const input = JSON.parse(fs.readFileSync(fixture, "utf8"));
// Helpers pulled in for the requested ones, since the extracted functions call
// each other (balanceText -> balanceFractionDigits, worstWindow -> asList).
const SUPPORT = [
	"asList",
	"asProvider",
	"remainingPct",
	"balanceFractionDigits",
];
const names = Array.from(new Set([...SUPPORT, ...Object.keys(input)]))
	.filter((n) => src.includes("function " + n + "("));
const parts = names.map(extract);
const factory = new Function(
	"Intl",
	"JSON",
	"Math",
	"Date",
	"Number",
	parts.join("\n\n") + "\nreturn { " + names.join(", ") + " };",
);
const api = factory(Intl, JSON, Math, Date, Number);

const results = {};
for (const name of Object.keys(input)) {
	const args = input[name];
	try {
		results[name] = api[name](...args);
	} catch (e) {
		results[name] = { __error: e.constructor.name + ": " + e.message };
	}
}
process.stdout.write(JSON.stringify(results));
