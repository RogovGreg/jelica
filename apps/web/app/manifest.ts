import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "JELICA Web",
    short_name: "JELICA",
    description: "Comparative genomics analysis.",
    start_url: "/",
    display: "standalone",
    background_color: "#ffffff",
    theme_color: "#25856a",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  };
}
