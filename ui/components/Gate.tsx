"use client";
import type { ReactNode } from "react";
import { useAuth } from "@/lib/hooks";
import { Shell } from "./Shell";
import { SignIn } from "./SignIn";
import { Skeleton } from "./ui";

export function Gate({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  if (status === "loading") return <div style={{ padding: 40, maxWidth: 520 }}><Skeleton lines={4} /></div>;
  if (status === "anon") return <SignIn />;
  return <Shell>{children}</Shell>;
}
