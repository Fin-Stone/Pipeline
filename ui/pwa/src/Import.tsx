/**
 * Putting statements into the ledger from the browser.
 *
 * The alternative was `scp` and a shell — a fine answer for the person who
 * built this and a poor one for the person living with it.
 *
 * **Every file is reported on its own.** A batch of twelve with one
 * unparseable statement in it must not read as twelve failures, and a duplicate
 * is a normal outcome rather than an error: re-uploading what is already
 * imported is what somebody does when they are not sure whether they did.
 *
 * The list of what is already in the ledger sits underneath, with a way to take
 * one back out. An upload moves every figure on the dashboard, and contract
 * rule 2a says a thing that does that needs both an inverse and somewhere the
 * inverse can be found later.
 */
import { useEffect, useRef, useState } from "react";
import {
  Alert, AlertTitle, Box, Button, Card, CardContent, Chip,
  Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle,
  IconButton, LinearProgress, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Tooltip, Typography,
} from "@mui/material";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import UploadFileIcon from "@mui/icons-material/UploadFile";
import { ApiError, DocumentRow, UploadResult, api } from "./api";

/** What each pipeline outcome means, in the words a person would use. */
const STATUS: Record<string, { label: string; colour: "success" | "warning" | "info" | "error" }> = {
  imported: { label: "imported", colour: "success" },
  imported_unverified: { label: "imported, balances unchecked", colour: "warning" },
  duplicate: { label: "already had it", colour: "info" },
  quarantined: { label: "set aside", colour: "warning" },
  pending: { label: "not reached", colour: "error" },
};

export default function Import() {
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [q, setQ] = useState("");
  const [confirming, setConfirming] = useState<DocumentRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  const report = (e: unknown) => setError(String(e instanceof ApiError ? e.detail : e));
  const load = () => api.documents().then((d) => setDocs(d.documents)).catch(report);

  useEffect(() => { load(); }, []);

  async function send(files: File[]) {
    if (!files.length) return;
    setBusy(true); setError(null); setResult(null);
    try {
      setResult(await api.upload(files));
      await load();
    } catch (e) { report(e); } finally { setBusy(false); }
  }

  async function remove(doc: DocumentRow) {
    setConfirming(null); setBusy(true);
    try { await api.deleteDocument(doc.sha256); await load(); }
    catch (e) { report(e); } finally { setBusy(false); }
  }

  const shown = q
    ? docs.filter((d) =>
        `${d.source_relpath} ${d.institution}`.toLowerCase().includes(q.toLowerCase()))
    : docs;

  return (
    <Stack spacing={2}>
      {error && <Alert severity="error" onClose={() => setError(null)}>{error}</Alert>}

      <Card
        variant="outlined"
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault(); setDragging(false);
          send(Array.from(e.dataTransfer.files));
        }}
        sx={{
          borderStyle: "dashed", borderWidth: 2,
          borderColor: dragging ? "primary.main" : "divider",
          bgcolor: dragging ? "action.hover" : undefined,
          transition: "border-color .15s, background-color .15s",
        }}
      >
        <CardContent sx={{ textAlign: "center", py: 5 }}>
          <UploadFileIcon sx={{ fontSize: 40, color: "text.secondary", mb: 1 }} />
          <Typography variant="h6">Drop statements here</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            PDF, OFX, CSV or MT940. Several at once is fine — each is reported
            separately, and sending one you already have is harmless.
          </Typography>
          <input
            ref={picker} type="file" multiple hidden
            accept=".pdf,.ofx,.qfx,.csv,.qif,.sta,.mt940,.xml"
            onChange={(e) => {
              send(Array.from(e.target.files ?? []));
              // Cleared, or choosing the same file twice in a row fires no
              // change event and looks like the button stopped working.
              e.target.value = "";
            }}
          />
          <Button variant="contained" disabled={busy} onClick={() => picker.current?.click()}>
            {busy ? "Working…" : "Choose files"}
          </Button>
          {busy && <LinearProgress sx={{ mt: 2 }} />}
        </CardContent>
      </Card>

      {result && <Outcome result={result} />}

      <Card variant="outlined">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="flex-start"
            spacing={2} sx={{ mb: 1 }}>
            <Box>
              <Typography variant="h6">In the ledger</Typography>
              <Typography variant="caption" color="text.secondary" component="p">
                {docs.length} document{docs.length === 1 ? "" : "s"}. Removing one takes
                its transactions with it — the original file is kept, so
                uploading it again brings it back.
              </Typography>
            </Box>
            <TextField size="small" label="Find one" value={q}
              onChange={(e) => setQ(e.target.value)} sx={{ minWidth: 180 }} />
          </Stack>

          {docs.length === 0 ? (
            <Typography color="text.secondary" variant="body2">
              Nothing imported yet.
            </Typography>
          ) : (
            <Box sx={{ overflowX: "auto" }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>File</TableCell>
                    <TableCell>Institution</TableCell>
                    <TableCell>Type</TableCell>
                    <TableCell>Status</TableCell>
                    <TableCell />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {shown.map((doc) => (
                    <TableRow key={doc.sha256} hover>
                      <TableCell sx={{ wordBreak: "break-all", maxWidth: 320 }}>
                        {doc.source_relpath}
                      </TableCell>
                      <TableCell>{doc.institution}</TableCell>
                      <TableCell>{doc.doc_type}</TableCell>
                      <TableCell>
                        <Chip size="small" variant="outlined"
                          color={doc.parse_status === "imported" ? "success" : "warning"}
                          label={doc.parse_status} />
                      </TableCell>
                      <TableCell align="right">
                        <Tooltip title="Remove this document and its transactions">
                          <IconButton size="small" disabled={busy}
                            onClick={() => setConfirming(doc)}>
                            <DeleteOutlineIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}
        </CardContent>
      </Card>

      {/* Asked rather than undone. Every other destructive action here has a
          snackbar undo, but this one deletes transactions and any decision
          attached to them — a five-second window is not enough time to notice,
          so the question comes first instead. */}
      <Dialog open={!!confirming} onClose={() => setConfirming(null)}>
        <DialogTitle>Remove this document?</DialogTitle>
        <DialogContent>
          <DialogContentText component="div">
            <Box sx={{ wordBreak: "break-all", mb: 1.5 }}>
              <strong>{confirming?.source_relpath}</strong>
            </Box>
            Its transactions go with it, along with anything you decided about
            them — hidden rows, hand-set categories, paybacks and transfer
            links pointing at those rows.
            <Box sx={{ mt: 1.5 }}>
              The original file is <strong>kept</strong>. Uploading it again
              re-imports it, though the decisions do not come back.
            </Box>
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirming(null)}>Cancel</Button>
          <Button color="error" onClick={() => confirming && remove(confirming)}>
            Remove
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

function Outcome({ result }: { result: UploadResult }) {
  const nothing = result.imported === 0 && result.accepted > 0;
  return (
    <Card variant="outlined">
      <CardContent>
        <Typography variant="h6" gutterBottom>
          {result.imported} imported
          {result.duplicates > 0 && `, ${result.duplicates} already had`}
          {result.quarantined > 0 && `, ${result.quarantined} set aside`}
        </Typography>
        {result.transactions > 0 && (
          <Typography variant="body2" color="text.secondary">
            {result.transactions.toLocaleString()} new transactions.
          </Typography>
        )}

        {/* An import changes spending in two ways and only one of them is the
            new rows: a statement arriving can complete a transfer pair that has
            been waiting for its other half. */}
        {result.transfers && result.transfers.added > 0 && (
          <Alert severity="success" sx={{ mt: 1.5 }}>
            <AlertTitle>{result.transfers.added} transfer
              {result.transfers.added === 1 ? "" : "s"} paired</AlertTitle>
            Statements you just added completed pairs that were waiting for
            them — a card payment whose other leg was not in the ledger yet.
            Those rows stop counting as spending.
          </Alert>
        )}

        {result.rejected.length > 0 && (
          <Alert severity="error" sx={{ mt: 1.5 }}>
            <AlertTitle>Not taken</AlertTitle>
            {result.rejected.map((r) => (
              <Box key={r.filename}>{r.filename} — {r.reason}</Box>
            ))}
          </Alert>
        )}

        {result.documents.length > 0 && (
          <Table size="small" sx={{ mt: 1.5 }}>
            <TableBody>
              {result.documents.map((doc) => {
                const status = STATUS[doc.status] ?? { label: doc.status, colour: "info" as const };
                return (
                  <TableRow key={doc.filename + doc.status}>
                    <TableCell sx={{ wordBreak: "break-all" }}>{doc.filename}</TableCell>
                    <TableCell>
                      <Chip size="small" color={status.colour} variant="outlined"
                        label={status.label} />
                    </TableCell>
                    <TableCell align="right">
                      {doc.transactions > 0 ? `${doc.transactions} rows` : ""}
                    </TableCell>
                    <TableCell sx={{ color: "text.secondary", fontSize: 13 }}>
                      {doc.reason}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}

        {nothing && (
          <Alert severity="info" sx={{ mt: 1.5 }}>
            Nothing new. Either these are already in the ledger, or their layout
            has no adapter yet — a statement nobody can parse is set aside with
            a reason rather than guessed at.
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
