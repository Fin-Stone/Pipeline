import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { CssBaseline, ThemeProvider, createTheme, useMediaQuery } from "@mui/material";
import App from "./App";

function Root() {
  // Follows the device. The TV route in §5 wants a dark, large-type surface and
  // a phone at night wants the same thing for different reasons.
  const prefersDark = useMediaQuery("(prefers-color-scheme: dark)");
  const theme = createTheme({
    palette: { mode: prefersDark ? "dark" : "light" },
    typography: {
      // Tabular figures everywhere money is shown, so columns of amounts line
      // up and a changing number does not shift the ones beside it.
      fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
    },
    shape: { borderRadius: 10 },
  });
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <App />
    </ThemeProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode><Root /></StrictMode>,
);
