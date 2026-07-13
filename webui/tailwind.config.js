/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        card: "hsl(var(--card))",
        "card-foreground": "hsl(var(--card-foreground))",
        popover: "hsl(var(--popover))",
        "popover-foreground": "hsl(var(--popover-foreground))",
        primary: "hsl(var(--primary))",
        "primary-foreground": "hsl(var(--primary-foreground))",
        secondary: "hsl(var(--secondary))",
        "secondary-foreground": "hsl(var(--secondary-foreground))",
        muted: "hsl(var(--muted))",
        "muted-foreground": "hsl(var(--muted-foreground))",
        accent: "hsl(var(--accent))",
        "accent-foreground": "hsl(var(--accent-foreground))",
        destructive: "hsl(var(--destructive))",
        "destructive-foreground": "hsl(var(--destructive-foreground))",
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        sidebar: "hsl(var(--sidebar))",
        "sidebar-foreground": "hsl(var(--sidebar-foreground))",
        "sidebar-accent": "hsl(var(--sidebar-accent))",
        "sidebar-accent-foreground": "hsl(var(--sidebar-accent-foreground))",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      fontFamily: {
        sans: [
          "'Helvetica Neue'", "'Segoe UI Variable'",
          "'Segoe UI'", "Arial", "sans-serif",
        ],
        display: ["'Iowan Old Style'", "'Palatino Linotype'", "'Book Antiqua'", "Palatino", "Georgia", "serif"],
        mono: [
          "'Geist Mono'", "'SF Mono'",
          "'JetBrains Mono'", "monospace",
        ],
      },
      keyframes: {
        "enter-up": {
          from: { opacity: "0", transform: "translateY(10px)" },
          to:   { opacity: "1", transform: "translateY(0)" },
        },
        "enter-scale": {
          from: { opacity: "0", transform: "scale(0.98)" },
          to:   { opacity: "1", transform: "scale(1)" },
        },
      },
      animation: {
        "enter-up":   "enter-up 0.5s cubic-bezier(0.16, 1, 0.3, 1) both",
        "enter-scale":"enter-scale 0.3s cubic-bezier(0.16, 1, 0.3, 1) both",
      },
      transitionTimingFunction: {
        spring: "cubic-bezier(0.32, 0.72, 0, 1)",
        smooth: "cubic-bezier(0.16, 1, 0.3, 1)",
        bounce: "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
    },
  },
  plugins: [require("tailwindcss-animate"), require("@tailwindcss/typography")],
};
