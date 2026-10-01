import type { NextConfig } from "next";

const apiProxyTarget = (process.env.AGENTHUB_API_PROXY_TARGET ?? "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);

const nextConfig: NextConfig = {
  output: "standalone",
  // The API streams SSE (agent runs, thread turns). Next's built-in response
  // compression buffers text/event-stream, which turns first-token latency
  // into a wall; gzip belongs to the reverse proxy in front of production
  // instead, where streams can be excluded by content type.
  compress: false,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
