/**
 * The shell: choose a server, then the screens.
 *
 * Net worth, spending and income are separate tabs rather than one long
 * dashboard. They are answers to three different questions, and stacking them
 * meant scrolling past two to reach the third.
 *
 * **The server is the user's.** Their own install or a hosted one, entered here
 * and stored locally, exactly as Bitwarden does it — architecture §5.2. Nothing
 * is baked in, and the version handshake happens before anything else so a
 * client update cannot silently break a self-hoster who has not upgraded.
 */
import { useEffect, useState } from "react";
import {
  AppBar, Alert, Box, Button, Card, CardContent, Container, MenuItem, Select,
  Stack, Tab, Tabs, TextField, Toolbar, Typography,
} from "@mui/material";
import Dashboard from "./Dashboard";
import NetWorthTab from "./NetWorthTab";
import Recurring from "./Recurring";
import Review from "./Review";
import Hidden from "./Hidden";
import { REQUIRED_API_VERSION, api, forgetServer, profile, serverUrl, setServer } from "./api";

function ServerSetup({ onReady }: { onReady: () => void }) {
  const [url, setUrl] = useState(serverUrl() ?? "http://localhost:8000");
  const [which, setWhich] = useState(profile());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function connect() {
    setBusy(true); setError(null);
    setServer(url, which);
    try {
      const health = await api.health();
      if (health.api_version !== REQUIRED_API_VERSION) {
        // Refused rather than attempted. A mismatched server is the failure
        // §5.2 exists to prevent, and guessing would corrupt what it shows.
        setError(
          `That server speaks ${health.api_version}; this client needs ` +
          `${REQUIRED_API_VERSION}. Upgrade one of them.`,
        );
        forgetServer();
      } else onReady();
    } catch {
      setError("Could not reach that server.");
      forgetServer();
    } finally { setBusy(false); }
  }

  return (
    <Container maxWidth="sm" sx={{ py: 6 }}>
      <Card variant="outlined">
        <CardContent>
          <Typography variant="h5" gutterBottom>Connect to your server</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Your own install or a hosted one. This client works the same against
            either, and stores nothing but the address.
          </Typography>
          <Stack spacing={2}>
            <TextField label="Server address" value={url} fullWidth
              onChange={(e) => setUrl(e.target.value)} placeholder="https://finstone.example" />
            <Select size="small" value={which} onChange={(e) => setWhich(e.target.value)}>
              <MenuItem value="prod">Production ledger</MenuItem>
              <MenuItem value="dummy">Sample data</MenuItem>
            </Select>
            {error && <Alert severity="error">{error}</Alert>}
            <Button variant="contained" disabled={busy} onClick={connect}>
              {busy ? "Connecting…" : "Connect"}
            </Button>
          </Stack>
        </CardContent>
      </Card>
    </Container>
  );
}

export default function App() {
  const [connected, setConnected] = useState(false);
  const [tab, setTab] = useState(0);

  useEffect(() => {
    if (!serverUrl()) return;
    api.health()
      .then((h) => setConnected(h.api_version === REQUIRED_API_VERSION))
      .catch(() => setConnected(false));
  }, []);

  if (!connected) return <ServerSetup onReady={() => setConnected(true)} />;

  return (
    <Box>
      <AppBar position="sticky" color="default" elevation={0}
        sx={{ borderBottom: 1, borderColor: "divider" }}>
        <Toolbar sx={{ gap: 2 }}>
          <Typography variant="h6" sx={{ flexGrow: 1 }}>Finstone</Typography>
          <Typography variant="caption" color="text.secondary">
            {serverUrl()} · {profile()}
          </Typography>
          <Button size="small" onClick={() => { forgetServer(); setConnected(false); }}>
            Change
          </Button>
        </Toolbar>
        <Tabs value={tab} onChange={(_, v) => setTab(v)} variant="scrollable"
          allowScrollButtonsMobile>
          <Tab label="Net worth" />
          <Tab label="Spending" />
          <Tab label="Income" />
          <Tab label="Recurring" />
          <Tab label="Review" />
          <Tab label="Hidden" />
        </Tabs>
      </AppBar>
      <Container maxWidth="lg" sx={{ py: 2 }}>
        {/* Remounted per tab rather than hidden: each dashboard owns its own
            filters, and carrying a spending filter silently into income would
            show a total that does not match its own controls. */}
        {tab === 0 && <NetWorthTab />}
        {tab === 1 && <Dashboard mode="spending" />}
        {tab === 2 && <Dashboard mode="income" />}
        {tab === 3 && <Recurring />}
        {tab === 4 && <Review />}
        {tab === 5 && <Hidden />}
      </Container>
    </Box>
  );
}
