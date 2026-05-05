/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: '#0f1117',
        panel: '#1a1f2e',
        row: '#1e2d42',
        selected: '#2a3f5f',
        accent: '#e85b2a',
      },
    },
  },
  plugins: [],
}
