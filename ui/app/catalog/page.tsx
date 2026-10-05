"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

// The catalog is now the home page. This keeps old links and bookmarks working.
export default function Catalog() {
  const router = useRouter();
  useEffect(() => { router.replace("/"); }, [router]);
  return null;
}
