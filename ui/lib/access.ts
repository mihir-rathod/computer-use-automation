import { atLeast, type PendingApproval, type RepairItemLite, type Role, type RunView } from "./types";

/** What each role sees in the navigation. The role decides visibility only: nothing is removed, and the API enforces every permission itself. */
export interface NavItem { href: string; label: string; key: string; icon: "tasks" | "discover" | "runs" | "inbox" | "chat" | "overview" | "artifacts" | "policy" | "keys" }

export function navFor(role: Role | undefined): { main: NavItem[]; manage: NavItem[] } {
  const main: NavItem[] = [{ href: "/", label: "Tasks", key: "h", icon: "tasks" }];
  main.push({ href: "/runs/", label: role === "operator" || role === "supervisor" ? "My runs" : "Runs", key: "r", icon: "runs" });
  if (atLeast(role, "operator")) { main.push({ href: "/inbox/", label: "Inbox", key: "i", icon: "inbox" }); main.push({ href: "/chat/", label: "Chat", key: "c", icon: "chat" }); }
  if (atLeast(role, "supervisor")) main.push({ href: "/discover/", label: "Discover", key: "t", icon: "discover" });
  const manage: NavItem[] = atLeast(role, "admin") ? [
    { href: "/overview/", label: "Overview", key: "o", icon: "overview" },
    { href: "/artifacts/", label: "Artifacts", key: "a", icon: "artifacts" },
    { href: "/policy/", label: "Policy", key: "p", icon: "policy" },
    { href: "/keys/", label: "API keys", key: "k", icon: "keys" },
  ] : [];
  return { main, manage };
}

export function mayDecideApproval(role: Role | undefined, name: string | undefined, tier: string, requestedBy: string): boolean {
  if (name === requestedBy) return false;
  return atLeast(role, tier === "supervisor" ? "supervisor" : "operator");
}

/** How many inbox items this person can act on right now, so the badge never counts things they cannot do anything about. */
export function actionableCount(inbox: { approvals: PendingApproval[]; needs_review: RunView[]; repairs: RepairItemLite[]; discovery_commits?: unknown[]; paused?: unknown[] } | null | undefined, me: { name: string; role: Role } | null): number {
  if (!inbox || !me) return 0;
  const approvals = inbox.approvals.filter((a) => mayDecideApproval(me.role, me.name, a.tier, a.requested_by)).length;
  const review = atLeast(me.role, "operator") ? inbox.needs_review.length : 0;
  const repairs = inbox.repairs.filter((r) => r.confident && atLeast(me.role, r.touches_irreversible_step ? "supervisor" : "operator")).length;
  const discovery = atLeast(me.role, "supervisor") ? (inbox.discovery_commits?.length ?? 0) : 0;
  const waiting = atLeast(me.role, "operator") ? (inbox.paused?.length ?? 0) : 0;
  return approvals + review + repairs + discovery + waiting;
}
