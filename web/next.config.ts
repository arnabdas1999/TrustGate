import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // The 3D scene's labels (drei <Html>) each own a React root; Strict Mode's dev-only double
  // mount tears that root down mid-render and drops labels. Production never double-mounts.
  reactStrictMode: false,
};

export default nextConfig;
