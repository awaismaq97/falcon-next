import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emit a minimal self-contained server (.next/standalone) for a lean Docker image.
  output: "standalone",
  // Allow remote images (e.g. NASA APOD) rendered via markdown <img>.
  images: { unoptimized: true },
  eslint: {
    // Don't fail the production build on lint warnings — CI can lint separately.
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
