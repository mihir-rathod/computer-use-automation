"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { actionableCount, navFor, type NavItem } from "@/lib/access";
import { useApi, useAuth, useTheme } from "@/lib/hooks";
import { Dialog } from "./ui";
import { IconAuto, IconChart, IconChat, IconFile, IconGrid, IconInbox, IconKey, IconMoon, IconPlay, IconShield, IconSun } from "./icons";

const ICONS: Record<NavItem["icon"], ReactNode> = {
  tasks: <IconGrid />, runs: <IconPlay />, inbox: <IconInbox />, chat: <IconChat />, overview: <IconChart />, artifacts: <IconFile />, policy: <IconShield />, keys: <IconKey />,
};

export function Shell({ children }: { children: ReactNode }) {
  const { me, signOut } = useAuth();
  const path = usePathname();
  const router = useRouter();
  const [theme, cycleTheme] = useTheme();
  const [help, setHelp] = useState(false);
  const nav = useMemo(() => navFor(me?.role), [me?.role]);
  const canInbox = nav.main.some((n) => n.icon === "inbox");
  const inbox = useApi<Parameters<typeof actionableCount>[0] & object>(canInbox ? "/v1/inbox" : null, { every: 10000 });
  const count = actionableCount(inbox.data, me);
  const all = [...nav.main, ...nav.manage];

  useEffect(() => {
    let armed = false, timer: ReturnType<typeof setTimeout> | undefined;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "?") { setHelp(true); return; }
      if (e.key === "/") { const s = document.querySelector<HTMLInputElement>("[data-search]"); if (s) { e.preventDefault(); s.focus(); } return; }
      if (armed) {
        armed = false; clearTimeout(timer);
        const hit = all.find((n) => n.key === e.key.toLowerCase());
        if (hit) { e.preventDefault(); router.push(hit.href); }
        return;
      }
      if (e.key === "g") { armed = true; timer = setTimeout(() => (armed = false), 1200); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router, all]); // eslint-disable-line react-hooks/exhaustive-deps

  const active = (href: string) => (href === "/" ? path === "/" || path === "" || path.startsWith("/task") : path.startsWith(href.replace(/\/$/, "")));
  const item = (n: NavItem) => (
    <Link key={n.href} href={n.href} aria-current={active(n.href) ? "page" : undefined}>
      {ICONS[n.icon]}<span>{n.label}</span>
      {n.icon === "inbox" && count > 0 && <span className="badge" aria-label={`${count} waiting for you`}>{count}</span>}
    </Link>
  );
  return (
    <div className="shell">
      <a className="skip" href="#main">Skip to content</a>
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark" aria-hidden>CU</span><span>Capability Console</span></div>
        <nav className="nav" aria-label="Primary">
          {nav.main.map(item)}
          {nav.manage.length > 0 && <><div className="nav-label">Manage</div>{nav.manage.map(item)}</>}
        </nav>
        <div className="side-foot">
          <div className="who"><span className="avatar" aria-hidden>{me?.name.slice(0, 2)}</span><div><strong>{me?.name}</strong><small>{me?.role}</small></div></div>
          <div className="row">
            <button className="btn sm ghost" onClick={cycleTheme} aria-label={`Theme: ${theme}. Click to change`} title={`Theme: ${theme}`}>
              {theme === "light" ? <IconSun width={16} height={16} /> : theme === "dark" ? <IconMoon width={16} height={16} /> : <IconAuto width={16} height={16} />}
            </button>
            <button className="btn sm ghost" onClick={() => setHelp(true)} aria-label="Keyboard shortcuts"><kbd>?</kbd></button>
            <button className="btn sm ghost" onClick={signOut}>Sign out</button>
          </div>
        </div>
      </aside>
      <main id="main" className="main" tabIndex={-1}>{children}</main>
      <Dialog open={help} onClose={() => setHelp(false)} title="Keyboard shortcuts" footer={<button className="btn" onClick={() => setHelp(false)}>Close</button>}>
        <dl className="kv">
          {all.map((n) => <div key={n.href} style={{ display: "contents" }}><dt><kbd>g {n.key}</kbd></dt><dd>{n.label}</dd></div>)}
          <div style={{ display: "contents" }}><dt><kbd>/</kbd></dt><dd>Focus the search box</dd></div>
          <div style={{ display: "contents" }}><dt><kbd>?</kbd></dt><dd>This help</dd></div>
          <div style={{ display: "contents" }}><dt><kbd>Esc</kbd></dt><dd>Close a dialog</dd></div>
        </dl>
      </Dialog>
    </div>
  );
}
