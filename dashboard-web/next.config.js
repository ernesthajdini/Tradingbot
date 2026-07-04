/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Run revalidation on every request so dashboards always feel fresh
  experimental: {
    serverActions: { allowedOrigins: ['*'] },
  },
};

module.exports = nextConfig;
