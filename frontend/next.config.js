/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  turbopack: { root: __dirname },
  async rewrites() {
    const backend = (process.env.BIS_BACKEND_URL || process.env.NEXT_PUBLIC_API_URL ||
      "http://localhost:8000").replace(/\/$/, "");
    return [{ source: "/api/v1/:path*", destination: `${backend}/api/v1/:path*` }];
  },
};

module.exports = nextConfig;
