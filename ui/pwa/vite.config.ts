import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// No proxy and no baked-in API host: the server address is entered by the user
// at sign-in and stored locally. A build that knows where its server lives
// cannot be self-hosted, which is the whole promise of §5.2.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
