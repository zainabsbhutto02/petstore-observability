import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";


export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/health": "http://127.0.0.1:5000",
      "/products": "http://127.0.0.1:5000",
      "/orders": "http://127.0.0.1:5000"
    }
  }
});
