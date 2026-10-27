import type { NavigateFunction } from "react-router-dom";
import { api } from "./api";
import type { Investigation } from "./types";

export interface StartArgs { kind: "incident" | "question"; prompt: string; scope?: Record<string, unknown> }

export async function startInvestigation(navigate: NavigateFunction, args: StartArgs) {
  const inv = await api<Investigation>("/investigations", { body: { ...args, scope: args.scope ?? {} } });
  navigate(`/investigations/${inv.id}`);
}
