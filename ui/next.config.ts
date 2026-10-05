import type { NextConfig } from "next";

// The console is a static export served by the FastAPI app under /ui, so it is one process on one port in production.
// In development, `next dev` proxies /v1 to the API so the same relative URLs work.
const dev = process.env.NODE_ENV === "development";

const config: NextConfig = {
  output: "export",
  basePath: "/ui",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
  ...(dev
    ? {
        async rewrites() {
          const api = process.env.API_ORIGIN ?? "http://127.0.0.1:8020";
          return [{ source: "/v1/:path*", destination: `${api}/v1/:path*`, basePath: false }];
        },
      }
    : {}),
};

export default config;
