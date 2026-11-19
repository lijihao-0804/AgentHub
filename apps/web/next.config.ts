import type { NextConfig } from "next";

const apiProxyTarget = (process.env.AGENTHUB_API_PROXY_TARGET ?? "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);

const nextConfig: NextConfig = {
  // A verification build can use its own output without replacing a live dev server's files.
  distDir: process.env.AGENTHUB_NEXT_DIST_DIR ?? ".next",
  output: "standalone",
  // Keep the framework's dev indicator (build status, route info, errors)
  // but park it bottom-right: the default bottom-left spot lands on the
  // sidebar footnote.
  devIndicators: { position: "bottom-right" },
  // The API streams SSE (agent runs, thread turns). Next's built-in response
  // compression buffers text/event-stream, which turns first-token latency
  // into a wall; gzip belongs to the reverse proxy in front of production
  // instead, where streams can be excluded by content type.
  compress: false,
  async rewrites() {
    return [
      { source: "/favicon.ico", destination: "/icon.svg" },
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
