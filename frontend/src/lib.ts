import type { NavigateFunction } from "react-router-dom";
import { api } from "./api";
import type { Investigation } from "./types";

export interface StartArgs { kind: "incident" | "question"; prompt: string; scope?: Record<string, unknown> }

export async function startInvestigation(navigate: NavigateFunction, args: StartArgs) {
  const inv = await api<Investigation>("/investigations", { body: { ...args, scope: args.scope ?? {} } });
  navigate(`/investigations/${inv.id}`);
}

// Mirror of backend app/services/intent.py - only used for the hint under the input box; the server decides.
const CAUSE = /\b(why (is|are|does|do|did|has|have|was|were)\b|what('?s| is| was| are) (causing|wrong|behind|broken)|root[- ]cause|caus(e|es|ing) (of|the)|(is|are|went|goes) down\b|not (working|responding|starting|loading)|stopped working|outage|incident|can'?t (connect|reach|pay|log ?in|access)|unable to (connect|reach|access))/i;
const SURVEY = /^\s*(is anything|is there anything|are (any|there)|which|list|how many|show( me)?|count|give me|what (was|is|were|got) (deployed|changed|running)|what changed|when (was|did))\b/i;
const SYMPTOM = /\b(slow(ness|er)?|latency|lagg?(ing|y)?|down|fail(s|ed|ing|ures?)?|broken|crash\w*|errors?|5\d\d|5xx|time ?outs?|timing out|unavailable|unreachable|degraded|oom\w*|evicted|restarting|hang(s|ing)?|stuck|pending|backoff|refused)\b/i;

export function classifyPrompt(prompt: string): "incident" | "question" {
  const t = prompt.trim();
  if (CAUSE.test(t)) return "incident";
  if (SURVEY.test(t)) return "question";
  if (SYMPTOM.test(t)) return "incident";
  return "question";
}
