/* tailwind_config.js — theme for the Tailwind play CDN; loaded right after it (the CSP allows no inline scripts). */
tailwind.config = {
  darkMode: "class",
  theme: {
    extend: {
      "colors": {
        "on-surface": "#2e2f2d",
        "tertiary-fixed": "#fec330",
        "on-secondary-fixed": "#00375b",
        "on-background": "#2e2f2d",
        "background": "#f7f6f3",
        "on-primary": "#d1ffc8",
        "on-tertiary": "#fff1db",
        "tertiary-fixed-dim": "#efb520",
        "surface-container": "#e8e8e5",
        "on-secondary-fixed-variant": "#005488",
        "primary-container": "#b2faa9",
        "on-surface-variant": "#5b5c5a",
        "on-primary-fixed-variant": "#2a6c2c",
        "primary-fixed": "#b2faa9",
        "surface-container-high": "#e2e3df",
        "on-tertiary-fixed": "#402d00",
        "surface-variant": "#ddddda",
        "surface-container-highest": "#ddddda",
        "on-secondary-container": "#004a79",
        "on-error": "#ffefec",
        "tertiary-dim": "#674b00",
        "error": "#b02500",
        "on-primary-fixed": "#054e12",
        "on-tertiary-fixed-variant": "#634900",
        "on-tertiary-container": "#584000",
        "secondary-fixed-dim": "#93c8ff",
        "on-primary-container": "#1f6223",
        "error-dim": "#b92902",
        "on-secondary": "#ecf3ff",
        "on-error-container": "#520c00",
        "secondary-fixed": "#afd5ff",
        "inverse-primary": "#b2faa9",
        "secondary": "#005f99",
        "inverse-surface": "#0d0f0d",
        "error-container": "#f95630",
        "outline-variant": "#adadab",
        "primary": "#266829",
        "surface-container-low": "#f1f1ee",
        "inverse-on-surface": "#9d9d9b",
        "tertiary-container": "#fec330",
        "primary-fixed-dim": "#a4eb9c",
        "secondary-container": "#afd5ff",
        "surface": "#f7f6f3",
        "surface-dim": "#d4d5d1",
        "primary-dim": "#185c1e",
        "outline": "#767775",
        "surface-container-lowest": "#ffffff",
        "secondary-dim": "#005386",
        "tertiary": "#765600",
        "surface-tint": "#266829",
        "surface-bright": "#f7f6f3"
      },
      "borderRadius": {
        "DEFAULT": "0.25rem",
        "lg": "0.5rem",
        "xl": "0.75rem",
        "full": "9999px"
      },
      "fontFamily": {
        "headline": ["Be Vietnam Pro"],
        "body": ["Plus Jakarta Sans"],
        "label": ["Plus Jakarta Sans"]
      }
    }
  }
}
