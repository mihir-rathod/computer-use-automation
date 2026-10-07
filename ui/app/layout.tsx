import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { AuthProvider, ToastProvider } from "@/lib/hooks";
import { Gate } from "@/components/Gate";

export const metadata: Metadata = { title: "Capability Console", description: "Run, approve and review recorded capabilities on systems that have no API." };

// Applied before first paint so a dark-theme user does not see a light flash.
const THEME_BOOT = `try{var t=localStorage.getItem("cua.theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: THEME_BOOT }} /></head>
      <body><AuthProvider><ToastProvider><Gate>{children}</Gate></ToastProvider></AuthProvider></body>
    </html>
  );
}
