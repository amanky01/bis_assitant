import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        bis: {
          blue: "#1e3a5f",
          "blue-light": "#2d4a6f",
          "blue-dark": "#0f2744",
          red: "#c41e3a",
          "red-light": "#e63950",
          "red-dark": "#9a1830",
          black: "#1a1a1a",
          // Assistant bubble: muted teal/slate so it’s distinct from user (blue)
          "chat-assistant": "#1e3d4a",
          "chat-assistant-border": "#2a4a5a",
        },
      },
      animation: {
        "typing-dot": "typing-dot 1.2s ease-in-out infinite both",
        "fade-in-up": "fade-in-up 0.4s ease-out forwards",
        "cursor-blink": "cursor-blink 1s ease-in-out infinite",
      },
      keyframes: {
        "typing-dot": {
          "0%, 70%, 100%": { opacity: "0.4", transform: "scale(0.85)" },
          "35%": { opacity: "1", transform: "scale(1.05)" },
        },
        "fade-in-up": {
          "0%": { opacity: "0", transform: "translateY(10px) scale(0.98)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        "cursor-blink": {
          "0%, 50%": { opacity: "1" },
          "51%, 100%": { opacity: "0.35" },
        },
      },
      backdropBlur: {
        xs: "2px",
      },
    },
  },
  plugins: [],
};

export default config;
