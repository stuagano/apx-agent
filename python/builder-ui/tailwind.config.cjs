/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        background: '#0a0a0a',
        surface: '#111111',
        panel: '#161616',
        border: '#2a2a2a',
        text: '#e5e7eb',
        textMuted: '#888888',
        primary: '#FF3621',
        dbTeal: '#1B3139',
        dbTealMid: '#243f49',
        dbTealLight: '#2e5060',
        dbTealBorder: '#34606f',
        dbGreen: '#00A972',
        dbBlue: '#60a5fa',
        dbYellow: '#F7A600',
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', 'monospace'],
        sans: ['"Sora"', 'sans-serif'],
      },
      boxShadow: {
        modern: '0 4px 6px -1px rgb(0 0 0 / 0.4), 0 2px 4px -2px rgb(0 0 0 / 0.4)',
        floating: '0 10px 15px -3px rgb(0 0 0 / 0.5), 0 4px 6px -4px rgb(0 0 0 / 0.5)',
      },
    },
  },
  plugins: [],
}
