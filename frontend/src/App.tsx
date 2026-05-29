import { useEffect, useMemo, useState } from "react";
import { AuthUser, AuthResponse, DocumentRecord, DocumentsResponse, UploadResponse } from "./types";
import { readJson } from "./utils/helpers";
import { Auth } from "./components/Auth";
import { Sidebar } from "./components/Sidebar";
import { Viewer } from "./components/Viewer";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const TOKEN_KEY = "bulkUploaderAccessToken";

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) ?? "");
  const [user, setUser] = useState<AuthUser | null>(null);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [selectedDocumentId, setSelectedDocumentId] = useState<number | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [message, setMessage] = useState("");
  const [uploadResult, setUploadResult] = useState<UploadResponse | null>(null);

  const selectedDocument = useMemo(() => {
    return documents.find((doc) => doc.id === selectedDocumentId) ?? documents[0] ?? null;
  }, [documents, selectedDocumentId]);

  function persistSession(auth: AuthResponse) {
    localStorage.setItem(TOKEN_KEY, auth.access_token);
    setToken(auth.access_token);
    setUser(auth.user);
  }

  async function loadDocuments(authToken = token) {
    if (!authToken) return;

    try {
      const data = await readJson<DocumentsResponse>(
        await fetch(`${API_BASE}/api/v1/questions`, {
          headers: { Authorization: `Bearer ${authToken}` }
        })
      );
      setDocuments(data.documents);
      setSelectedDocumentId((current) => current ?? data.documents[0]?.id ?? null);
    } catch (error) {
      console.error("Failed to load documents:", error);
    }
  }

  useEffect(() => {
    if (!token) return;

    let isMounted = true;
    async function restoreSession() {
      try {
        const resp = await fetch(`${API_BASE}/api/v1/auth/me`, {
          headers: { Authorization: `Bearer ${token}` }
        });

        if (!resp.ok) {
          if (resp.status === 401) {
            localStorage.removeItem(TOKEN_KEY);
            if (isMounted) {
              setToken("");
              setUser(null);
              setMessage("Session expired");
            }
          } else {
            if (isMounted) setMessage(`Server error: ${resp.status}`);
          }
          return;
        }

        const currentUser = (await resp.json()) as AuthUser;
        if (!isMounted) return;
        setUser(currentUser);
        await loadDocuments(token);
      } catch (error) {
        if (isMounted) setMessage("Network error: unable to contact server");
      }
    }

    restoreSession();
    return () => { isMounted = false; };
  }, [token]);

  async function logout() {
    await fetch(`${API_BASE}/api/v1/auth/logout`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : undefined
    }).catch(() => undefined);
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setUser(null);
    setDocuments([]);
    setSelectedDocumentId(null);
    setUploadResult(null);
  }

  if (!token || !user) {
    return (
      <Auth 
        onAuthSuccess={persistSession}
        isBusy={isBusy}
        setIsBusy={setIsBusy}
        message={message}
        setMessage={setMessage}
      />
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">DOCX Vault</p>
          <h1>Document Intake</h1>
        </div>
        <div className="account">
          <span>{user.email}</span>
          <button type="button" onClick={logout}>Logout</button>
        </div>
      </header>

      <section className="workspace">
        <Sidebar 
          token={token}
          isBusy={isBusy}
          setIsBusy={setIsBusy}
          isDeleting={isDeleting}
          setIsDeleting={setIsDeleting}
          message={message}
          setMessage={setMessage}
          uploadResult={uploadResult}
          setUploadResult={setUploadResult}
          documents={documents}
          setDocuments={setDocuments}
          selectedDocumentId={selectedDocumentId}
          setSelectedDocumentId={setSelectedDocumentId}
          loadDocuments={loadDocuments}
        />
        <Viewer 
          selectedDocument={selectedDocument}
          documents={documents}
          uploadResult={uploadResult}
        />
      </section>
    </div>
  );
}
