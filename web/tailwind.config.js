/* Built by scripts/build_css.py into static/tailwind.css. Colors are the CSS variables of static/style.css (:root). */
const token = name => `color-mix(in srgb, var(--${name}) calc(<alpha-value> * 100%), transparent)`;

module.exports = {
  content: ["./templates/**/*.html", "./static/**/*.js"],
  theme: {
    extend: {
      colors: {
        "primary":                   token("primary"),
        "primary-dim":               token("primary-dim"),
        "on-primary":                token("on-primary"),
        "secondary":                 token("secondary"),
        "surface":                   token("surface"),
        "surface-container-lowest":  token("surface-lowest"),
        "surface-container-low":     token("surface-low"),
        "surface-container-high":    token("surface-high"),
        "surface-container-highest": token("surface-highest"),
        "surface-variant":           token("surface-highest"),
        "on-surface":                token("ink"),
        "on-surface-variant":        token("ink-muted"),
        "outline-variant":           token("outline-variant"),
      },
      borderRadius: { DEFAULT: "0.25rem", lg: "0.5rem", xl: "0.75rem", full: "9999px" },
      fontFamily: { sans: ["Heebo", "sans-serif"] },
    },
  },
  plugins: [require("@tailwindcss/forms"), require("@tailwindcss/container-queries")],
};
